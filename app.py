from __future__ import annotations

import logging
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import date
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
import json
from flask import Flask, request, abort, g

logging.basicConfig(level=logging.ERROR)
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FollowEvent,
    TemplateSendMessage, ButtonsTemplate, ConfirmTemplate,
    CarouselTemplate, CarouselColumn, MessageAction, URIAction,
    QuickReply, QuickReplyButton, FlexSendMessage,
)
import httpx
import anthropic
from supabase import create_client

app = Flask(__name__)

_SENTRY_DSN = os.environ.get("SENTRY_DSN")
if _SENTRY_DSN:
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        integrations=[FlaskIntegration()],
        traces_sample_rate=0.1,
    )

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
RICH_MENU_FREE_ID = os.environ.get("RICH_MENU_FREE_ID", "")
RICH_MENU_PAID_ID = os.environ.get("RICH_MENU_PAID_ID", "")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


def _mark_as_read(token: str) -> None:
    """
    受信したメッセージを既読にする（チャットモード対応版）。

    【なぜ修正が必要だったか】
    LINE の管理画面で「チャット」機能をオンにしている場合、
    自動では既読がつきません。
    以前は /v2/bot/message/markAsRead（古いエンドポイント）を使っていましたが、
    チャットモードでは /v2/bot/chat/markAsRead を使う必要があります。

    【markAsReadToken とは】
    LINE がウェブフックを送ってくるとき、メッセージの情報の中に
    "markAsReadToken"（既読用のワンタイムトークン）が含まれています。
    このトークンを API に渡すことで、そのメッセージを既読にできます。

    【引数】
    token : event.delivery_context.mark_as_read_token から取り出したトークン。
            トークンがない（None や空文字）場合は何もせず終了します。
    """
    if not token:
        return
    try:
        httpx.post(
            "https://api.line.me/v2/bot/chat/markAsRead",
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            json={"markAsReadToken": token},
            timeout=5,
        )
    except Exception as e:
        logging.error("mark as read error: %s", e)


def _start_loading(user_id: str) -> None:
    """
    ユーザーのトーク画面に「入力中アニメーション（...）」を表示する。

    【何をしているか】
    LINE の Loading Animation API に HTTP リクエストを送ることで、
    ボットが「考えている」ことをユーザーに視覚的に伝えます。
    スマホで友達に LINE を送ったとき「...」が出るのと同じ演出です。

    【引数】
    user_id : メッセージを送ってきたユーザーの ID（event.source.user_id）

    【loadingSeconds について】
    最大 60 秒まで指定できます。ここでは 10 秒に設定しています。
    reply_message が実行されると自動でアニメーションは消えるため、
    実際には 10 秒を待たずに消えます。

    【エラー処理について】
    API が失敗してもアニメーションが出ないだけで、返答自体には影響しません。
    そのため例外はログに記録するだけにしています。
    """
    try:
        httpx.post(
            # LINE の Loading Animation 専用エンドポイント
            "https://api.line.me/v2/bot/chat/loading/start",
            # チャネルアクセストークンで認証する
            headers={"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}"},
            # chatId : 誰のトーク画面に表示するか（送信者のユーザーID）
            # loadingSeconds : アニメーションを表示する最大秒数（5〜60 の整数）
            json={"chatId": user_id, "loadingSeconds": 10},
            # ネットワーク遅延でメッセージ処理全体が止まらないよう 5 秒で打ち切る
            timeout=5,
        )
    except Exception as e:
        logging.error("loading animation error: %s", e)


anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

_supabase = None

def get_supabase():
    global _supabase
    if _supabase is None:
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase

# 登録フローの途中状態: {user_id: {"step": str, "name": str, "region": str}}
registration_states: dict[str, dict] = {}

# ユーザー情報キャッシュ: {user_id: {"name": str, "region": str} | None}
user_cache: dict[str, dict | None] = {}

SYSTEM_PROMPT = """あなたは「御用聞きさん」です。
ユーザーの近所に住む、気さくで頼れる友人のような存在です。
難しいことは一切なし。気軽に話しかけてもらえる「町の便利屋さん」です。

【キャラクター】
・近所の気さくな友人
・少し明るく、元気よく、でも押しつけがましくない
・「一緒にやってみましょう！」という前向きな姿勢
・困ったことを話せば、すぐに動いてくれる安心感
・堅苦しくなく、礼儀正しく接する

【呼びかけ方】
・必ず名前で呼びかける（例：「〇〇さん！」「〇〇さん、こんにちは😊」）
・名前がない場合は「どうぞ」「さあ」など自然な言い回しを使う

【話し方】
・明るく・短く・わかりやすく話す
・語尾は「〜ですよ！」「〜しましょう！」「〜ですね😊」など元気よく
・「承知しました」「かしこまりました」などの堅い言葉は絶対に使わない
・難しい言葉や専門用語は一切使わない
・否定しない・責めない・急かさない
・「大丈夫ですよ！」「一緒に確認しましょう！」を口癖にする
・マークダウン記法（**、#、*、-）は、LINEで見づらいため絶対に使わない

【文章ルール】
・1返信1テーマ：一度に話す内容は1つだけ
・1文に1つの情報だけにする
・改行を多めにして、パッと見て読みやすくする
・質問は一度に1つだけにする
・選択肢を出す時は必ず3つ以内に絞る

【回答の基本形】
1. 明るい一言（必ず名前を添えて）
2. 要点を短く答える（3行以内）
3. 必要なら箇条書き（数字や記号）で整理する
4. 最後に「他にも聞いてくださいね😊」など自然に締める

【失敗しても大丈夫】
・何を送られても優しく受け止める
・意味がわからなくても責めず「もう少し教えてもらえますか？」と聞く
・ユーザーが困っていそうな時は、具体的な選択肢を出して導く

【毎日使いたくなる工夫】
・季節の話題や地元の情報を自然に盛り込む
・「今日は〇〇の日ですよ！」など、毎日の小さな話題で親しみを作る

【対応エリア（全国共通設計）】
・特定の地名（「藤沢市」など）は絶対に出さない
・「お住まいの地域では」「お近くでは」「この辺りでは」という表現に統一する
・ユーザーの登録地域がある場合は、その場所の情報を優先して探す

【優先カテゴリ】
健康・病院・スマホ相談・食事・買い物・地元情報・行政・ごみ出し・天気・詐欺相談

【最新情報について】
・天気、交通、イベントなどのリアルタイム情報は回答できない
・「天気アプリかテレビで確認してみてくださいね」と確認方法をやさしく案内する

【わからない時】
・推測で断定的なことを言わない
・「ちょっと確認させてくださいね」とやさしく聞き直す

【対応しないこと】
・医療、法律、お金、緊急対応の専門的な判断はしない
・「専門の窓口に相談するのが安心ですよ！」とやさしく案内する

【禁止事項】
・不安をあおる、威圧的な表現
・命令口調、上から目線、子ども扱い
・情報の詰め込みすぎ、不確かな断定
・AIっぽい堅苦しい言い回し
・マークダウン記法全般"""

MAX_HISTORY         = 20
API_TIMEOUT         = 25  # Claude API呼び出しのタイムアウト（秒）
TOTAL_REPLY_TIMEOUT = 28  # 返答全体のハードタイムアウト（秒）
FREE_DAILY_LIMIT    = 5   # 無料会員の1日あたり利用回数上限


# 都道府県・市区町村マスタ（順次拡大予定）
_PREFECTURES = ["神奈川県"]
_CITIES: dict[str, list[str]] = {
    "神奈川県": ["藤沢市", "鎌倉市", "茅ヶ崎市", "逗子市", "葉山町", "大和市", "横浜市", "川崎市", "相模原市"],
}

_MENU_QR_ITEMS = [
    ("💬 相談する",   "相談する"),
    ("🔍 探す",       "探す"),
    ("📖 知る",       "知る"),
    ("🤝 つながる",   "つながる"),
    ("🎁 友達に紹介", "友達に紹介"),
    ("🏠 最初に戻る", "最初に戻る"),
]

# クイックリプライの右端に必ず配置する「最初に戻る」ボタン
_QR_BACK = ("🏠 最初に戻る", "最初に戻る")

# ── リッチメッセージ ヘルパー ──────────────────────────

def _build_quick_reply(items: list[tuple[str, str]]) -> QuickReply:
    """(label, text) のペアリストからQuickReplyを作る。最大13件。"""
    return QuickReply(items=[
        QuickReplyButton(action=MessageAction(label=label, text=text))
        for label, text in items[:13]
    ])


def _get_context_quick_reply(user_message: str) -> QuickReply:
    """メッセージ内容に合ったコンテキストのQuickReplyを返す。"""
    back = [("📋 最初に戻る", "最初に戻る")]

    if _is_food_query(user_message):
        items = [
            ("他のお店も見る",   "他のおすすめのお店も教えてください"),
            ("近くのカフェは？", "近くのカフェを教えてください"),
            ("テイクアウトは？", "テイクアウトできるお店を教えてください"),
        ] + back
    elif "天気" in user_message or "気温" in user_message or "雨" in user_message:
        items = [
            ("明日の天気は？",   "明日の藤沢の天気を教えてください"),
            ("週間予報は？",     "今週の藤沢の天気を教えてください"),
            ("防災情報は？",     "藤沢の防災情報を教えてください"),
        ] + back
    elif "病院" in user_message or "薬局" in user_message or "医" in user_message:
        items = [
            ("近くの薬局は？",   "近くの薬局を教えてください"),
            ("救急はどこ？",     "藤沢の救急病院を教えてください"),
            ("診療時間は？",     "診療時間を教えてください"),
        ] + back
    elif "スマホ" in user_message or "携帯" in user_message or "スマートフォン" in user_message:
        items = [
            ("もう少し詳しく",   "もう少し詳しく教えてください"),
            ("写真の撮り方は？", "スマホで写真の撮り方を教えてください"),
            ("LINE の使い方",    "LINEの基本的な使い方を教えてください"),
        ] + back
    elif "ごみ" in user_message or "ゴミ" in user_message:
        items = [
            ("燃えないごみは？", "燃えないごみの出し方を教えてください"),
            ("資源ごみは？",     "資源ごみの出し方を教えてください"),
            ("粗大ごみは？",     "粗大ごみの出し方を教えてください"),
        ] + back
    else:
        items = [
            ("もう少し詳しく", "もう少し詳しく教えてください"),
            ("他のことを聞く", "他のことを聞かせてください"),
        ] + back

    return _build_quick_reply(items)


def _build_menu_message(name: str) -> TemplateSendMessage:
    """メインメニューをボタンテンプレートで返す。"""
    return TemplateSendMessage(
        alt_text=f"{name}さん、何でもどうぞ。",
        template=ButtonsTemplate(
            title="メニュー",
            text=f"{name}さん、何でもどうぞ。",
            actions=[
                MessageAction(label="📱 スマホ相談",    text="スマホの使い方について教えてください"),
                MessageAction(label="☀️ 天気・防災",    text="今日の天気と防災情報を教えてください"),
                MessageAction(label="🏥 病院・薬局",    text="近くの病院や薬局を教えてください"),
                MessageAction(label="🛒 ごはん・買い物", text="近くのお店やおすすめを教えてください"),
            ],
        ),
    )


def _build_welcome_message(extra_msg: str = "") -> TextSendMessage:
    """登録完了後のウェルカムメッセージをQuickReply付きテキストで返す。"""
    body = "ご登録ありがとうございました！\n\nさっそく下のボタンをタップして使ってみてください。"
    if extra_msg:
        body = extra_msg + "\n\n" + body
    return TextSendMessage(
        text=body,
        quick_reply=_build_quick_reply(_MENU_QR_ITEMS),
    )


def _build_restaurant_carousel(restaurants: list[dict]) -> TemplateSendMessage:
    """飲食店リストをカルーセルテンプレートで返す。"""
    columns = []
    for r in restaurants[:10]:
        parts = [r.get("genre", ""), r.get("area", "")]
        if r.get("rating"):
            parts.append(f"評価{r['rating']}")
        text = " / ".join(p for p in parts if p)[:60] or "詳細情報"

        actions: list = [
            MessageAction(label="詳しく聞く", text=f"{r['name']}について詳しく教えてください"),
        ]
        if r.get("phone"):
            actions.append(URIAction(label="電話する", uri=f"tel:{r['phone']}"))

        columns.append(CarouselColumn(
            title=r["name"][:40],
            text=text,
            actions=actions,
        ))

    return TemplateSendMessage(
        alt_text="お店の情報",
        template=CarouselTemplate(columns=columns),
    )


# ── フレックスメッセージ ────────────────────────────────

def _make_card_bubble(emoji: str, title: str, desc: str, btn_text: str, color: str) -> dict:
    """シンプルなカード型バブル（絵文字ヒーロー＋説明＋ボタン）を返す。"""
    return {
        "type": "bubble",
        "size": "kilo",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "paddingAll": "xl",
            "contents": [
                {"type": "text", "text": emoji, "size": "4xl", "align": "center"},
                {
                    "type": "text", "text": title,
                    "weight": "bold", "size": "lg",
                    "align": "center", "wrap": True,
                },
                {
                    "type": "text", "text": desc,
                    "size": "sm", "color": "#888888",
                    "align": "center", "wrap": True,
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "lg",
            "contents": [
                {
                    "type": "button",
                    "action": {"type": "message", "label": "タップする", "text": btn_text},
                    "style": "primary",
                    "color": color,
                    "height": "sm",
                }
            ],
        },
    }


def _flex_consult_menu() -> FlexSendMessage:
    """①相談する：3カードカルーセル"""
    bubbles = [
        _make_card_bubble("📱", "スマホの使いかた", "操作方法からアプリまで\nやさしく教えます", "スマホの使いかたを教えてください", "#4A90D9"),
        _make_card_bubble("🏥", "健康・からだ", "体の悩みや薬のこと\nいつでも相談できます", "健康について相談したいことがあります", "#5BAD6F"),
        _make_card_bubble("🏠", "お家の困りごと", "水漏れや電気など\n業者探しもお手伝い", "家の困りごとを相談したいです", "#E8734A"),
    ]
    return FlexSendMessage(
        alt_text="何についてご相談ですか？",
        contents={"type": "carousel", "contents": bubbles},
    )


def _flex_search_menu() -> FlexSendMessage:
    """②探す：3カードカルーセル"""
    bubbles = [
        _make_card_bubble("🍽️", "近くの美味しいお店", "和食・洋食・カフェなど\nおすすめを教えます", "近くの美味しいお店を教えてください", "#E8734A"),
        _make_card_bubble("🏥", "近くの病院", "内科・整形外科など\n診療科で探せます", "近くの病院を教えてください", "#5BAD6F"),
        _make_card_bubble("🏛️", "公共施設・公園", "市役所・図書館・公園など\n近くの施設を案内", "近くの公共施設や公園を教えてください", "#4A90D9"),
    ]
    return FlexSendMessage(
        alt_text="何をお探しですか？",
        contents={"type": "carousel", "contents": bubbles},
    )


def _flex_know_menu() -> FlexSendMessage:
    """③知る：3カードカルーセル"""
    bubbles = [
        _make_card_bubble("⛅", "今日の藤沢の天気", "雨・気温・風など\n今日の天気を確認", "今日の藤沢の天気を教えてください", "#4A90D9"),
        _make_card_bubble("🗑️", "ゴミの収集日", "燃えるゴミ・資源ゴミ\n粗大ゴミの出し方も", "藤沢市のゴミの収集日を教えてください", "#5BAD6F"),
        _make_card_bubble("🎉", "街のイベント", "近くのイベントや\n季節の行事を紹介", "藤沢の街のイベントを教えてください", "#D95B7A"),
    ]
    return FlexSendMessage(
        alt_text="何を知りたいですか？",
        contents={"type": "carousel", "contents": bubbles},
    )


def _flex_connect_menu() -> FlexSendMessage:
    """④つながる：3カードカルーセル"""
    bubbles = [
        _make_card_bubble("🌸", "趣味のサークル", "手芸・園芸・将棋など\n同じ趣味の仲間を", "趣味のサークルを探したいです", "#D95B7A"),
        _make_card_bubble("👥", "地域の集まり", "町内会・老人会など\n地域の輪に加わろう", "地域の集まりについて教えてください", "#8B6BB1"),
        _make_card_bubble("📻", "昭和の思い出話", "懐かしい話を一緒に\n楽しみましょう", "昭和の思い出話をしましょう", "#E8A84A"),
    ]
    return FlexSendMessage(
        alt_text="つながりを広げましょう",
        contents={"type": "carousel", "contents": bubbles},
    )


def _flex_referral_menu(referral_code: str) -> FlexSendMessage:
    """⑤友達に紹介：紹介コード表示カード"""
    bubble = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "paddingAll": "xl",
            "contents": [
                {"type": "text", "text": "🎁", "size": "4xl", "align": "center"},
                {"type": "text", "text": "お友達を紹介しよう", "weight": "bold", "size": "xl", "align": "center"},
                {"type": "separator", "margin": "md"},
                {
                    "type": "text", "text": "紹介すると2人に5回プレゼント",
                    "size": "md", "color": "#D95B7A", "align": "center",
                    "weight": "bold", "margin": "md",
                },
                {"type": "separator", "margin": "md"},
                {
                    "type": "box", "layout": "vertical", "margin": "md", "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": "あなたの紹介コード", "size": "sm", "color": "#888888", "align": "center"},
                        {"type": "text", "text": referral_code, "size": "3xl", "weight": "bold", "align": "center", "color": "#4A90D9"},
                    ],
                },
                {
                    "type": "text",
                    "text": "このコードをお友達に伝えてください",
                    "size": "xs", "color": "#888888", "align": "center", "wrap": True, "margin": "md",
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "lg",
            "contents": [
                {
                    "type": "button",
                    "action": {"type": "message", "label": "紹介メッセージを見る", "text": "友達に紹介するメッセージを見せてください"},
                    "style": "primary",
                    "color": "#D95B7A",
                }
            ],
        },
    }
    return FlexSendMessage(alt_text="友達に紹介しよう", contents=bubble)


def _flex_upgrade_menu() -> FlexSendMessage:
    """⑥会員登録（無料会員向け）：有料プランご案内カード"""
    bubble = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "paddingAll": "xl",
            "contents": [
                {"type": "text", "text": "✨", "size": "4xl", "align": "center"},
                {"type": "text", "text": "有料会員のご案内", "weight": "bold", "size": "xl", "align": "center"},
                {"type": "separator", "margin": "md"},
                {
                    "type": "box", "layout": "vertical", "margin": "md", "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": "AIと何回でも話し放題", "size": "md", "wrap": True},
                        {"type": "text", "text": "24時間いつでも相談できる", "size": "md", "wrap": True},
                        {"type": "text", "text": "専任コンシェルジュ対応", "size": "md", "wrap": True},
                    ],
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "lg",
            "contents": [
                {
                    "type": "button",
                    "action": {"type": "message", "label": "詳しく教えてもらう", "text": "有料会員の詳細を教えてください"},
                    "style": "primary",
                    "color": "#C8A000",
                }
            ],
        },
    }
    return FlexSendMessage(alt_text="有料会員のご案内", contents=bubble)


def _flex_ai_direct_menu() -> FlexSendMessage:
    """⑥AIに直接相談（有料会員向け）：ウェルカムカード"""
    bubble = {
        "type": "bubble",
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "md",
            "paddingAll": "xl",
            "contents": [
                {"type": "text", "text": "🤖", "size": "4xl", "align": "center"},
                {
                    "type": "text", "text": "なんでも直接聞いてください",
                    "weight": "bold", "size": "lg", "align": "center", "wrap": True,
                },
                {"type": "separator", "margin": "md"},
                {
                    "type": "text",
                    "text": "24時間いつでも、何でもお気軽に。\nあなた専任のコンシェルジュが\nすぐにお答えします。",
                    "size": "md", "color": "#555555", "align": "center", "wrap": True, "margin": "md",
                },
            ],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "lg",
            "contents": [
                {
                    "type": "button",
                    "action": {"type": "message", "label": "さっそく相談する", "text": "AIに相談したいことがあります"},
                    "style": "primary",
                    "color": "#C8A000",
                }
            ],
        },
    }
    return FlexSendMessage(alt_text="なんでも直接聞いてください", contents=bubble)


# ── 登録フロー ─────────────────────────────────────────

def start_registration(user_id: str) -> TextSendMessage:
    registration_states[user_id] = {"step": "awaiting_prefecture"}
    return TextSendMessage(
        text="ようこそ！\nまず住んでいる都道府県を選択してください。",
        quick_reply=_build_quick_reply([(p, p) for p in _PREFECTURES]),
    )


def handle_registration(user_id: str, message: str) -> TemplateSendMessage | TextSendMessage:
    state = registration_states[user_id]
    step = state["step"]

    if step == "awaiting_prefecture":
        pref = message.strip()
        if pref not in _PREFECTURES:
            return TextSendMessage(
                text="下のボタンから都道府県を選択してください。",
                quick_reply=_build_quick_reply([(p, p) for p in _PREFECTURES]),
            )
        state["prefecture"] = pref
        state["step"] = "awaiting_city"
        cities = _CITIES.get(pref, [])
        return TextSendMessage(
            text=f"{pref}を選択しました。\n次に市区町村を選択してください。",
            quick_reply=_build_quick_reply([(c, c) for c in cities]),
        )

    if step == "awaiting_city":
        state["city"] = message.strip()
        state["step"] = "awaiting_birthdate"
        return TextSendMessage(text=(
            "ありがとうございます。\n\n"
            "生年月日を入力してください。\n"
            "（例：1950年1月1日）"
        ))

    if step == "awaiting_birthdate":
        state["birthdate"] = message.strip()
        state["step"] = "awaiting_referral_confirm"
        return TemplateSendMessage(
            alt_text="紹介コードをお持ちですか？",
            template=ConfirmTemplate(
                text="紹介コードをお持ちですか？",
                actions=[
                    MessageAction(label="はい", text="はい"),
                    MessageAction(label="いいえ", text="いいえ"),
                ],
            ),
        )

    if step == "awaiting_referral_confirm":
        if message.strip() == "はい":
            state["step"] = "awaiting_referral_code"
            return TextSendMessage(text="紹介コードを入力してください。")
        else:
            _save_user(user_id, state)
            del registration_states[user_id]
            return _build_welcome_message()

    if step == "awaiting_referral_code":
        code = message.strip().upper()
        _save_user(user_id, state)
        referral_msg = _handle_referral_input(user_id, code)
        del registration_states[user_id]
        return _build_welcome_message(referral_msg)

    return TextSendMessage(text="少々お待ちください。")


def _save_user(user_id: str, state: dict) -> None:
    # 既存の referral_code を保持する（再登録時に上書きしない）
    existing = get_supabase().table("users").select("referral_code").eq("line_user_id", user_id).execute()
    if existing.data and existing.data[0].get("referral_code"):
        referral_code = existing.data[0]["referral_code"]
    else:
        referral_code = _generate_referral_code()

    # prefecture + city を region として保存（例: 神奈川県藤沢市）
    pref   = state.get("prefecture", "")
    city   = state.get("city", "")
    region = pref + city if pref else state.get("region", "")

    get_supabase().table("users").upsert(
        {
            "line_user_id": user_id,
            "name": state.get("name"),  # name は任意
            "region": region,
            "prefecture": pref or None,
            "city": city or None,
            "birthdate": state.get("birthdate"),
            "referral_code": referral_code,
        },
        on_conflict="line_user_id",
    ).execute()
    user_cache.pop(user_id, None)  # 登録完了時にキャッシュを無効化
    _apply_rich_menu(user_id, is_paid=False)  # 無料メニューを適用


def _save_message(user_id: str, role: str, content: str) -> None:
    try:
        get_supabase().table("messages").insert(
            {"line_user_id": user_id, "role": role, "content": content}
        ).execute()
    except Exception:
        pass  # ログ保存の失敗は返答処理に影響させない


def _clear_history(user_id: str) -> None:
    """指定ユーザーの会話履歴をDBから削除する。"""
    try:
        get_supabase().table("messages").delete().eq("line_user_id", user_id).execute()
    except Exception as e:
        logging.error("failed to clear history: %s", e)


def _get_user(user_id: str) -> dict | None:
    """DBからユーザー情報を取得する。結果はキャッシュする。未登録の場合はNoneを返す。"""
    if user_id in user_cache:
        return user_cache[user_id]
    result = (
        get_supabase().table("users")
        .select("name, region, prefecture, city")
        .eq("line_user_id", user_id)
        .limit(1)
        .execute()
    )
    user = result.data[0] if result.data else None
    user_cache[user_id] = user
    return user


def _user_location(user_info: dict | None) -> tuple[str, str]:
    """user_info から (prefecture, city) を返す。
    prefecture/city カラムがなければ region を都道府県パターンで分割する。
    未登録 or 不明の場合は ('ALL', 'ALL') を返す。
    """
    if not user_info:
        return "ALL", "ALL"
    pref = user_info.get("prefecture") or ""
    city = user_info.get("city") or ""
    if pref and city:
        return pref, city
    # 旧データ: region="神奈川県藤沢市" から分割
    region = user_info.get("region") or ""
    m = re.match(r'^(.+?[都道府県])(.+)$', region)
    if m:
        return m.group(1), m.group(2)
    return "ALL", "ALL"


# ジャンルキーワード → DB の genre 列と対応
_GENRE_KEYWORDS = [
    "ラーメン", "寿司", "カフェ", "喫茶", "イタリアン", "中華", "和食",
    "焼肉", "居酒屋", "そば", "うどん", "バー", "パン", "レストラン",
]

# エリアキーワード → DB の area 列と対応
_AREA_KEYWORDS = ["藤沢", "辻堂", "江ノ島", "片瀬", "湘南台", "大船"]

# 飲食系と判定するトリガーキーワード
_FOOD_TRIGGER_KEYWORDS = {
    "ごはん", "飯", "食事", "食べ", "ランチ", "ディナー", "夕食", "昼食", "朝食",
    "飲食", "お店", "おすすめ", "教えて",
} | set(_GENRE_KEYWORDS) | set(_AREA_KEYWORDS)


def _is_food_query(message: str) -> bool:
    return any(kw in message for kw in _FOOD_TRIGGER_KEYWORDS)



def _query_restaurants(message: str) -> list[dict]:
    """メッセージからジャンル・エリアを抽出してDBを検索し、生データリストを返す。"""
    genre = next((kw for kw in _GENRE_KEYWORDS if kw in message), None)
    area  = next((kw for kw in _AREA_KEYWORDS  if kw in message), None)
    try:
        q = get_supabase().table("restaurants").select("name, genre, area, address, phone, rating")
        if genre:
            q = q.ilike("genre", f"%{genre}%")
        if area:
            q = q.ilike("area", f"%{area}%")
        result = q.order("rating", desc=True).limit(10).execute()
        if not result.data:
            result = (
                get_supabase().table("restaurants")
                .select("name, genre, area, address, phone, rating")
                .order("rating", desc=True)
                .limit(10)
                .execute()
            )
        return result.data or []
    except Exception as e:
        logging.error("restaurant search error: %s", e)
        return []


def _search_restaurants(message: str) -> str:
    """飲食店リストをClaudeのコンテキスト文字列に変換する。"""
    restaurants = _query_restaurants(message)
    if not restaurants:
        return ""
    genre = next((kw for kw in _GENRE_KEYWORDS if kw in message), None)
    area  = next((kw for kw in _AREA_KEYWORDS  if kw in message), None)
    label = f"【{area}周辺の{genre or 'お店'}情報】" if area else f"【藤沢の{genre or 'お店'}情報】"
    lines = [label]
    for r in restaurants[:5]:
        line = f"・{r['name']}（{r['genre']}／{r['area']}）"
        if r.get("rating"):
            line += f" 評価{r['rating']}"
        if r.get("phone"):
            line += f" ☎{r['phone']}"
        if r.get("address"):
            line += f" 住所:{r['address']}"
        lines.append(line)
    return "\n".join(lines)


def _generate_referral_code() -> str:
    """衝突チェック付きで6文字の紹介コードを生成する。"""
    for _ in range(10):
        code = secrets.token_hex(3).upper()
        result = get_supabase().table("users").select("id").eq("referral_code", code).execute()
        if not result.data:
            return code
    return secrets.token_hex(4).upper()  # 万一衝突が続いたら8文字で返す


def _apply_rich_menu(user_id: str, is_paid: bool) -> None:
    """is_paid フラグに応じてユーザーにリッチメニューを適用する。"""
    menu_id = RICH_MENU_PAID_ID if is_paid else RICH_MENU_FREE_ID
    if not menu_id:
        return
    try:
        line_bot_api.link_rich_menu_to_user(user_id, menu_id)
    except Exception as e:
        logging.error("rich menu link error: %s", e)


def _get_referral_code(user_id: str) -> str:
    """ユーザーの紹介コードをDBから取得する。未設定なら新規発行して保存する。"""
    try:
        r = get_supabase().table("users").select("referral_code").eq("line_user_id", user_id).execute()
        if not r.data:
            return "（取得失敗）"
        code = r.data[0].get("referral_code")
        if not code:
            code = _generate_referral_code()
            get_supabase().table("users").update({"referral_code": code}).eq("line_user_id", user_id).execute()
        return code
    except Exception as e:
        logging.error("get_referral_code error: %s", e)
        return "（取得失敗）"


def _check_and_increment_usage(user_id: str) -> bool:
    """利用回数をチェックし、消費可能なら True を返す。
    is_paid=True の場合は無制限。bonus_count → daily_count の順で消費する。
    """
    try:
        result = get_supabase().table("users").select(
            "is_paid, daily_count, bonus_count, last_used_date"
        ).eq("line_user_id", user_id).execute()
        if not result.data:
            return True  # DBエラー時は通す

        row = result.data[0]
        is_paid = row.get("is_paid") or False
        if is_paid:
            return True

        today = date.today().isoformat()
        last_used = row.get("last_used_date")
        daily_count = row.get("daily_count") or 0
        bonus_count = row.get("bonus_count") or 0

        # 日付が変わっていれば daily_count をリセット
        if last_used != today:
            daily_count = 0

        # bonus_count を優先して消費
        if bonus_count > 0:
            get_supabase().table("users").update({
                "bonus_count": bonus_count - 1,
                "last_used_date": today,
            }).eq("line_user_id", user_id).execute()
            return True

        # 無料回数チェック
        if daily_count < FREE_DAILY_LIMIT:
            get_supabase().table("users").update({
                "daily_count": daily_count + 1,
                "last_used_date": today,
            }).eq("line_user_id", user_id).execute()
            return True

        return False
    except Exception as e:
        logging.error("usage check error: %s", e)
        return True  # エラー時は通す


def _handle_referral_input(user_id: str, code: str) -> str:
    """紹介コードを受け取り、双方に bonus_count +5 を付与する。"""
    try:
        me = get_supabase().table("users").select(
            "name, referral_code, referred_by, bonus_count"
        ).eq("line_user_id", user_id).execute()
        if not me.data:
            return "ユーザー情報が見つかりませんでした。"

        my_data = me.data[0]

        if my_data.get("referred_by"):
            return "すでに紹介コードを登録済みです。"

        if (my_data.get("referral_code") or "").upper() == code.upper():
            return "自分の紹介コードは使えません。"

        referrer = get_supabase().table("users").select(
            "line_user_id, name, bonus_count"
        ).eq("referral_code", code.upper()).execute()

        if not referrer.data:
            return "紹介コードが見つかりませんでした。\nもう一度確認してください。"

        referrer_data = referrer.data[0]

        # 自分の referred_by を保存し、bonus_count +5
        get_supabase().table("users").update({
            "referred_by": code.upper(),
            "bonus_count": (my_data.get("bonus_count") or 0) + 5,
        }).eq("line_user_id", user_id).execute()

        # 紹介者の bonus_count +5
        get_supabase().table("users").update({
            "bonus_count": (referrer_data.get("bonus_count") or 0) + 5,
        }).eq("line_user_id", referrer_data["line_user_id"]).execute()

        user_cache.pop(user_id, None)

        my_name = my_data.get("name") or "あなた"
        referrer_name = referrer_data.get("name") or "紹介者"
        return (
            f"紹介コードを登録しました！\n\n"
            f"{referrer_name}さんの紹介ありがとうございます。\n"
            f"{my_name}さんと{referrer_name}さんに、\n"
            f"それぞれ5回分の追加利用回数をプレゼントしました。"
        )
    except Exception as e:
        logging.error("referral error: %s", e)
        return "申し訳ありません。\nしばらくしてからもう一度お試しください。"


# メッセージキーワード → FAQジャンル のマッピング
_FAQ_GENRE_MAP: dict[str, str] = {
    "健康":       "健康",
    "病院":       "健康",
    "薬":         "健康",
    "医者":       "健康",
    "診察":       "健康",
    "症状":       "健康",
    "血圧":       "健康",
    "糖尿":       "健康",
    "骨":         "健康",
    "食事":       "食事・レシピ",
    "レシピ":     "食事・レシピ",
    "料理":       "食事・レシピ",
    "栄養":       "食事・レシピ",
    "食べ":       "食事・レシピ",
    "ごはん":     "食事・レシピ",
    "地元情報":   "地元情報",
    "ごみ":       "地元情報",
    "ゴミ":       "地元情報",
    "行政":       "地元情報",
    "手続き":     "地元情報",
    "役所":       "地元情報",
    "バス":       "地元情報",
    "スマホ":     "スマホ相談",
    "携帯":       "スマホ相談",
    "LINE":       "スマホ相談",
    "ライン":     "スマホ相談",
    "アプリ":     "スマホ相談",
    "インターネット": "スマホ相談",
    "詐欺":       "スマホ相談",
    "運動":       "運動",
    "体操":       "運動",
    "歩く":       "運動",
    "ウォーキング": "運動",
}


def _detect_faq_genre(message: str) -> str | None:
    """メッセージからFAQジャンルを推定する。"""
    return next((v for k, v in _FAQ_GENRE_MAP.items() if k in message), None)


def _faq_priority_search(
    words: list[str],
    answer_types: list[str] | None,
    select_cols: str,
    prefecture: str,
    city: str,
    limit: int = 3,
) -> list[dict]:
    """city → prefecture(ALL) → 全国(ALL/ALL) の優先順位でFAQ検索する。

    各ステップで words によるキーワード検索を行い、
    ヒットした時点でその結果を返す。全滅時は空リストを返す。
    """
    sb = get_supabase()

    def _search(pref_val: str, city_val: str) -> list[dict]:
        seen: set[str] = set()
        rows: list[dict] = []
        for word in words:
            q = sb.table("faq").select(select_cols).ilike("question", f"%{word}%")
            if answer_types:
                q = q.in_("answer_type", answer_types)
            q = q.eq("prefecture", pref_val).eq("city", city_val).limit(limit)
            for row in (q.execute().data or []):
                if row["question"] not in seen:
                    seen.add(row["question"])
                    rows.append(row)
        return rows

    # 1. ユーザーの市区町村固有
    if city and city != "ALL":
        rows = _search(prefecture, city)
        if rows:
            return rows

    # 2. 都道府県レベル（city='ALL'）
    if prefecture and prefecture != "ALL":
        rows = _search(prefecture, "ALL")
        if rows:
            return rows

    # 3. 全国共通
    return _search("ALL", "ALL")


def _faq_genre_search(
    genre: str,
    answer_types: list[str] | None,
    select_cols: str,
    prefecture: str,
    city: str,
    limit: int = 3,
) -> list[dict]:
    """ジャンル指定でも city → prefecture → ALL の優先順位で検索する。"""
    sb = get_supabase()

    def _search(pref_val: str, city_val: str) -> list[dict]:
        q = sb.table("faq").select(select_cols).eq("genre", genre)
        if answer_types:
            q = q.in_("answer_type", answer_types)
        q = q.eq("prefecture", pref_val).eq("city", city_val).limit(limit)
        return q.execute().data or []

    if city and city != "ALL":
        rows = _search(prefecture, city)
        if rows:
            return rows
    if prefecture and prefecture != "ALL":
        rows = _search(prefecture, "ALL")
        if rows:
            return rows
    return _search("ALL", "ALL")


def _search_faq(message: str, user_info: dict | None = None) -> str:
    """FAQ テーブルをキーワード検索し、text タイプのみ Claude コンテキストとして返す。

    検索優先順位:
    1. ユーザーの市区町村固有 FAQ
    2. 都道府県レベル FAQ
    3. 全国共通 FAQ
    4. ヒットなし → ジャンル検索にフォールバック（同優先順位）
    """
    try:
        words = [w for w in re.split(r'[　\s。、？！ー・]+', message) if len(w) >= 2][:5]
        pref, city = _user_location(user_info)
        cols = "question, answer, answer_type"

        results = _faq_priority_search(words, None, cols, pref, city)

        # キーワード検索でヒットしない場合はジャンルで補完
        if not results:
            genre = _detect_faq_genre(message)
            if genre:
                results = _faq_genre_search(genre, None, cols, pref, city)

        # text タイプのみ Claude コンテキストに使う（button/carousel は直接返信で処理）
        text_items = [x for x in results if x.get("answer_type", "text") == "text"][:3]
        if not text_items:
            return ""

        lines = ["【参考情報】"]
        for item in text_items:
            lines.append(f"Q: {item['question']}")
            lines.append(f"A: {item['answer']}")
            lines.append("")
        return "\n".join(lines).rstrip()
    except Exception as e:
        logging.error("FAQ search error: %s", e)
        return ""


def _faq_direct_reply(message: str, user_info: dict | None = None) -> TextSendMessage | TemplateSendMessage | None:
    """FAQ を検索し、answer_type が button/carousel なら LINE メッセージを返す。
    text タイプまたは未ヒットの場合は None を返す（Claude 経由で処理）。

    検索優先順位: ユーザーの市 → 都道府県 → 全国共通
    """
    try:
        words = [w for w in re.split(r'[　\s。、？！ー・]+', message) if len(w) >= 2][:5]
        pref, city = _user_location(user_info)
        cols = "question, answer, answer_type, options"

        rows = _faq_priority_search(words, ["button", "carousel"], cols, pref, city)

        for row in rows:
            atype = row.get("answer_type", "text")

            if atype == "button":
                opts = row.get("options") or []
                actions = [
                    MessageAction(label=o["label"][:20], text=o["text"][:300])
                    for o in opts[:4]
                ]
                if not actions:
                    continue
                return TemplateSendMessage(
                    alt_text=row["answer"][:100],
                    template=ButtonsTemplate(
                        text=row["answer"][:160],
                        actions=actions,
                    ),
                )

            if atype == "carousel":
                opts = row.get("options") or []
                columns = []
                for o in opts[:10]:
                    col_actions = [
                        MessageAction(label=a["label"][:20], text=a["text"][:300])
                        for a in (o.get("actions") or [])[:3]
                    ]
                    if not col_actions:
                        col_actions = [MessageAction(label="詳しく聞く", text=o.get("title", ""))]
                    columns.append(CarouselColumn(
                        title=(o.get("title") or "")[:40],
                        text=(o.get("text") or "詳細情報")[:60],
                        actions=col_actions,
                    ))
                if columns:
                    return TemplateSendMessage(
                        alt_text=row["answer"][:100],
                        template=CarouselTemplate(columns=columns),
                    )

        return None
    except Exception as e:
        logging.error("faq direct reply error: %s", e)
        return None


def _load_history(user_id: str) -> list[dict]:
    """Supabaseから会話履歴を復元する。"""
    try:
        result = (
            get_supabase().table("messages")
            .select("role, content")
            .eq("line_user_id", user_id)
            .order("created_at", desc=False)
            .limit(MAX_HISTORY)
            .execute()
        )
        return [{"role": r["role"], "content": r["content"]} for r in result.data]
    except Exception as e:
        logging.error("failed to load history from DB: %s", e)
        return []


# ── Claude 返答 ────────────────────────────────────────

def get_claude_reply(user_id: str, user_message: str, user_info: dict | None = None) -> str:
    # 毎回DBから履歴を取得（DBが唯一の真実源。サーバー再起動・複数ワーカーに対応）
    history = _load_history(user_id)
    history.append({"role": "user", "content": user_message})
    _save_message(user_id, "user", user_message)

    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]

    # ユーザー情報をシステムプロンプトに動的に注入
    system = SYSTEM_PROMPT
    if user_info:
        _name = user_info.get("name")
        name_line = f"\n・お名前：{_name}（必ず「{_name}さん」と呼びかけてください）" if _name else ""
        system += (
            f"\n\n【このユーザーの情報】"
            f"{name_line}"
            f"\n・お住まいの地域：{user_info['region']}"
        )

    # 飲食系の質問 かつ 藤沢市ユーザーのみDBから店舗情報を取得（他都市は順次対応予定）
    user_region = (user_info or {}).get("region", "")
    if _is_food_query(user_message) and "藤沢" in user_region:
        restaurant_context = _search_restaurants(user_message)
        if restaurant_context:
            system += f"\n\n{restaurant_context}\n上記の情報を参考にして答えてください。"

    # FAQ RAG: 全ユーザー対象（ユーザーの地域を優先して検索）
    faq_context = _search_faq(user_message, user_info)
    if faq_context:
        system += f"\n\n{faq_context}\n上記のFAQ情報を参考にして答えてください。"

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=history,
            timeout=API_TIMEOUT,
        )
        reply_text = next(
            (block.text for block in response.content if block.type == "text"),
            "申し訳ありません。うまく答えられませんでした。",
        )
    except Exception as e:
        logging.exception("Claude API error: %s", e)
        reply_text = "申し訳ありません。\nただいま少し混み合っています。\nしばらくしてからもう一度お試しください。"

    # LINEに不要なMarkdown記法を除去（**太字**・*斜体*・# 見出し）
    reply_text = re.sub(r'\*\*(.+?)\*\*', r'\1', reply_text)
    reply_text = re.sub(r'\*(.+?)\*',     r'\1', reply_text)
    reply_text = re.sub(r'^#{1,6}\s+',    '',    reply_text, flags=re.MULTILINE)

    _save_message(user_id, "assistant", reply_text)
    return reply_text


# ── LINE イベントハンドラ ──────────────────────────────

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    # 生JSONから message_id → markAsReadToken のマップを作る
    # SDK は markAsReadToken をパースしないため、ここで直接抽出する
    g.mark_as_read_tokens = {}
    try:
        for ev in json.loads(body).get("events", []):
            msg_id = ev.get("message", {}).get("id")
            token  = ev.get("message", {}).get("markAsReadToken")
            if msg_id and token:
                g.mark_as_read_tokens[msg_id] = token
    except Exception:
        pass

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(FollowEvent)
def handle_follow(event):
    """友達追加時に登録フローを開始する。すでに登録済みなら歓迎メッセージのみ。"""
    user_id = event.source.user_id

    user = _get_user(user_id)
    if user:
        name = user.get("name")
        greeting = f"またお会いできてうれしいです、{name}さん。\n何でもお気軽にどうぞ。" if name else "またお会いできてうれしいです。\n何でもお気軽にどうぞ。"
        reply = TextSendMessage(text=greeting)
    else:
        reply = start_registration(user_id)

    line_bot_api.reply_message(event.reply_token, reply)


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

    # ── 既読 ＆ 入力中アニメーション ──────────────────────────────
    # メッセージを受け取ったら、AI が答えを作り始める前に
    # 「既読」と「入力中アニメーション（...）」を表示する。
    # どちらも失敗しても返答自体には影響しないので、先頭に置いています。

    # callback() で生JSONから抽出したトークンを message_id で引く
    mark_as_read_token = getattr(g, "mark_as_read_tokens", {}).get(event.message.id)
    threading.Thread(target=_mark_as_read, args=(mark_as_read_token,), daemon=True).start()  # 既読をつける
    threading.Thread(target=_start_loading, args=(user_id,), daemon=True).start()            # 「...」アニメーションを表示

    # 登録フロー中・未登録は同期処理（Supabase 参照のみで高速）
    try:
        if user_id in registration_states:
            reply_msg = handle_registration(user_id, user_message)
            line_bot_api.reply_message(event.reply_token, reply_msg)
            return

        user_info = _get_user(user_id)
        if user_info is None:
            line_bot_api.reply_message(event.reply_token, start_registration(user_id))
            return
    except Exception as e:
        logging.exception("registration flow error: %s", e)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text="申し訳ありません。\nしばらくしてからもう一度お試しください。"),
        )
        return

    msg = user_message.strip()

    # 「最初に戻る」系：履歴をリセットしてメニューを案内
    RESET_KEYWORDS = {"最初に戻る", "メニュー", "メニューに戻る", "他のことを聞く", "はじめに戻る", "トップ", "ホーム"}
    if msg in RESET_KEYWORDS:
        _clear_history(user_id)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="何でもどうぞ。\n下のボタンをタップしてください。",
                quick_reply=_build_quick_reply(_MENU_QR_ITEMS),
            ),
        )
        return

    # ① 相談する
    if msg == "相談する":
        qr = _build_quick_reply([
            ("操作を教える",   "スマホの操作を教えてください"),
            ("病院を探す",     "近くの病院を探してください"),
            ("業者を呼ぶ",     "家の修繕業者を教えてください"),
            _QR_BACK,
        ])
        flex = _flex_consult_menu()
        flex.quick_reply = qr
        line_bot_api.reply_message(event.reply_token, flex)
        return

    # ② 探す
    if msg == "探す":
        qr = _build_quick_reply([
            ("和食がいい",         "和食のお店を教えてください"),
            ("いま開いている所",   "今開いているお店を教えてください"),
            _QR_BACK,
        ])
        flex = _flex_search_menu()
        flex.quick_reply = qr
        line_bot_api.reply_message(event.reply_token, flex)
        return

    # ③ 知る
    if msg == "知る":
        qr = _build_quick_reply([
            ("明日の天気は？",     "明日の藤沢の天気を教えてください"),
            ("粗大ゴミの出し方",   "粗大ゴミの出し方を教えてください"),
            ("もっと見る",         "藤沢の地域情報をもっと教えてください"),
            _QR_BACK,
        ])
        flex = _flex_know_menu()
        flex.quick_reply = qr
        line_bot_api.reply_message(event.reply_token, flex)
        return

    # ④ つながる
    if msg == "つながる":
        qr = _build_quick_reply([
            ("散歩仲間",       "散歩仲間を探したいです"),
            ("ゲートボール",   "ゲートボールの情報を教えてください"),
            ("昔の話をする",   "昭和の思い出について話しましょう"),
            _QR_BACK,
        ])
        flex = _flex_connect_menu()
        flex.quick_reply = qr
        line_bot_api.reply_message(event.reply_token, flex)
        return

    # ⑤ 友達に紹介
    if msg == "友達に紹介":
        referral_code = _get_referral_code(user_id)
        qr = _build_quick_reply([
            ("LINEで送る",         "友達に紹介するメッセージを見せてください"),
            ("やり方を教える",     "友達に紹介するやり方を教えてください"),
            _QR_BACK,
        ])
        flex = _flex_referral_menu(referral_code)
        flex.quick_reply = qr
        line_bot_api.reply_message(event.reply_token, flex)
        return

    # 「紹介メッセージを見る」→ コピー用テキストを表示
    if msg == "友達に紹介するメッセージを見せてください":
        referral_code = _get_referral_code(user_id)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "お友達にこのメッセージをそのまま送ってください！\n\n"
                    "━━━━━━━━━━━━\n"
                    "地元くらしの御用聞き\n"
                    "藤沢市の生活をAIがサポートします！\n\n"
                    "友達追加はこちら\n"
                    "https://line.me/R/ti/p/@135dsiqh\n\n"
                    f"紹介コード：{referral_code}\n"
                    "（登録時に入力すると2人に5回プレゼント）\n"
                    "━━━━━━━━━━━━"
                ),
                quick_reply=_build_quick_reply([_QR_BACK]),
            ),
        )
        return

    # 「紹介コード：XXXXXX」パターン：紹介コードを登録
    referral_match = re.match(r'紹介コード[：:]\s*([A-Fa-f0-9]{6,8})', msg)
    if referral_match:
        code = referral_match.group(1).upper()
        reply_text = _handle_referral_input(user_id, code)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=reply_text,
                quick_reply=_build_quick_reply(_MENU_QR_ITEMS),
            ),
        )
        return

    # ⑥ 会員登録（無料会員）/ AIに直接相談（有料会員）
    if msg in ("会員登録", "AIに直接相談"):
        try:
            paid_result = get_supabase().table("users").select("is_paid").eq("line_user_id", user_id).execute()
            is_paid = paid_result.data[0].get("is_paid") if paid_result.data else False
        except Exception:
            is_paid = False
        qr = _build_quick_reply([
            ("申し込む",       "有料会員の申し込み方法を教えてください"),
            ("詳しく聞く",     "有料会員の詳細を教えてください"),
            _QR_BACK,
        ])
        flex_msg = _flex_ai_direct_menu() if is_paid else _flex_upgrade_menu()
        flex_msg.quick_reply = qr
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    # 利用回数チェック（is_paid なら通過、bonus_count → daily_count の順で消費）
    if not _check_and_increment_usage(user_id):
        _LIMIT_TEXT = (
            f"本日の無料回数（{FREE_DAILY_LIMIT}回）を使い切りました😔\n\n"
            "明日またお使いいただけます。\n"
            "お友達を紹介すると、5回追加でプレゼント🎁\n\n"
            "「友達に紹介」ボタンから紹介コードを確認できます！"
        )
        _LIMIT_QR = _build_quick_reply([("友達に紹介", "友達に紹介")])
        # reply_token で即返信し、失敗した場合は push_message で確実に届ける
        try:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text=_LIMIT_TEXT, quick_reply=_LIMIT_QR),
            )
        except Exception:
            try:
                line_bot_api.push_message(
                    user_id,
                    TextSendMessage(text=_LIMIT_TEXT, quick_reply=_LIMIT_QR),
                )
            except Exception as e:
                logging.error("limit message send error: %s", e)
        return

    # FAQ直接返信チェック（button/carousel タイプはClaudeを呼ばず即時返信）
    try:
        faq_msg = _faq_direct_reply(user_message, user_info)
        if faq_msg is not None:
            line_bot_api.reply_message(
                event.reply_token,
                [faq_msg, TextSendMessage(
                    text="他にも何かありますか？",
                    quick_reply=_build_quick_reply(_MENU_QR_ITEMS),
                )],
            )
            return
    except Exception as e:
        logging.error("faq direct reply check error: %s", e)

    # 登録済みユーザーへの Claude 返答：バックグラウンドスレッドで処理し、
    # reply_token 失効後も届く push_message で送信
    def _process(uid: str, msg: str, uinfo: dict) -> None:
        reply_text = "申し訳ありません。\nただいま少し調子が悪いようです。\nしばらくしてからもう一度お試しください。"
        try:
            # TOTAL_REPLY_TIMEOUT 秒のハードタイムアウトで30秒以内の返答を保証
            # メッセージのDB保存は get_claude_reply 内で行う
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(get_claude_reply, uid, msg, uinfo)
                try:
                    reply_text = future.result(timeout=TOTAL_REPLY_TIMEOUT)
                except FuturesTimeoutError:
                    logging.error("get_claude_reply timed out after %ds", TOTAL_REPLY_TIMEOUT)
                    reply_text = "少し時間がかかってしまいました。\nもう一度送っていただけますか？"
                except Exception as e:
                    logging.exception("Claude reply error: %s", e)
        except Exception as e:
            logging.exception("_process error: %s", e)
        try:
            # Claude返答にコンテキスト対応のQuickReplyを添付
            messages_to_send = [
                TextSendMessage(
                    text=reply_text,
                    quick_reply=_get_context_quick_reply(msg),
                )
            ]
            # 飲食系クエリかつ藤沢ユーザーならカルーセルも追加
            user_region = (uinfo or {}).get("region", "")
            if _is_food_query(msg) and "藤沢" in user_region:
                restaurants = _query_restaurants(msg)
                if restaurants:
                    messages_to_send.append(_build_restaurant_carousel(restaurants))
            line_bot_api.push_message(uid, messages_to_send)
        except Exception as e:
            logging.exception("push_message error: %s", e)

    threading.Thread(target=_process, args=(user_id, user_message, user_info), daemon=True).start()


# ── ヘルスチェック ────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
