from __future__ import annotations

import logging
import os
import re
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import date, datetime, timezone, timedelta
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
import json
from flask import Flask, request, abort, g, jsonify, redirect, render_template

logging.basicConfig(level=logging.ERROR)
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FollowEvent,
    MessageAction, URIAction,
    QuickReply, QuickReplyButton, FlexSendMessage,
)
import httpx
import anthropic
import openai
import stripe
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
RICH_MENU_FREE_TAB1_ID  = os.environ.get("RICH_MENU_FREE_TAB1_ID", "")
RICH_MENU_FREE_TAB2_ID  = os.environ.get("RICH_MENU_FREE_TAB2_ID", "")
RICH_MENU_PAID_TAB1_ID  = os.environ.get("RICH_MENU_PAID_TAB1_ID", "")
RICH_MENU_PAID_TAB2_ID  = os.environ.get("RICH_MENU_PAID_TAB2_ID", "")
OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY", "")
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
LIFF_ID          = os.environ.get("LIFF_ID", "")
LIFF_INVITE_ID   = os.environ.get("LIFF_INVITE_ID",  LIFF_ID)
LIFF_FAQ_ID      = os.environ.get("LIFF_FAQ_ID",     LIFF_ID)
LIFF_SEARCH_ID   = os.environ.get("LIFF_SEARCH_ID",  LIFF_ID)
LIFF_MAP_ID      = os.environ.get("LIFF_MAP_ID",      LIFF_ID)
LIFF_SCHEDULE_ID = os.environ.get("LIFF_SCHEDULE_ID", LIFF_ID)
LIFF_MEMO_ID     = os.environ.get("LIFF_MEMO_ID",     LIFF_ID)
GOOGLE_MAPS_API_KEY    = os.environ.get("GOOGLE_MAPS_API_KEY", "")
STRIPE_SECRET_KEY      = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET  = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID        = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_SUCCESS_URL     = os.environ.get("STRIPE_SUCCESS_URL", "https://line-bot-jq43.onrender.com/stripe/success")
STRIPE_CANCEL_URL      = os.environ.get("STRIPE_CANCEL_URL",  "https://line-bot-jq43.onrender.com/stripe/cancel")
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None


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
# 健康相談の途中状態: {user_id: str}
# 値: "awaiting_symptom" | "awaiting_dept" | "awaiting_medicine" | "awaiting_side_effect"
_health_states: dict[str, str] = {}
# 食事レシピの途中状態: {user_id: dict}
# step: "mood" | "ingredients" | "condition" | "step_by_step"
_recipe_states: dict[str, dict] = {}
# 昭和モード：性別未登録ユーザーが「なつかしい昭和」を押して性別入力待ち
_showa_gender_pending: set[str] = set()
# 昭和トーク中のセッション: {user_id: {"era": int, "gender": str|None, "topic": str}}
_showa_sessions: dict[str, dict] = {}
# 趣味・生きがいの途中状態: {user_id: {"step": str, "data": dict}}
_hobby_states: dict[str, dict] = {}
# 動画・音楽の途中状態: {user_id: {"step": str}}
_music_states: dict[str, dict] = {}
# 旅行相談の途中状態: {user_id: {"type": str, "fitness": str, "budget": str}}
_travel_states: dict[str, dict] = {}

# ユーザー情報キャッシュ: {user_id: {"name": str, "region": str} | None}
user_cache: dict[str, dict | None] = {}
# リッチメニューの現在値をメモリで追跡（DB カラム不要・サーバー再起動でリセット）
_applied_menu_cache: dict[str, str] = {}

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

SHOWA_MODEL = "claude-sonnet-4-20250514"

SHOWA_SYSTEM_PROMPT = """あなたは「昭和博士」です。
昭和時代のことなら何でも知っている話し上手で聞き上手な会話の達人です。

【キャラクター】
・昭和のことなら何でも知っている博士
・ユーザーの話を聞くのが大好き
・共感上手・盛り上げ上手
・「そうそう！あの頃は〜」が口癖
・温かく・楽しく・懐かしい雰囲気
・絵文字は😊🌸📻🎵🎶を多用

【話し方】
・ユーザーの話を必ず褒める
  「それは素敵な思い出ですね😊」「懐かしいですよね！」
・昭和の豆知識を自然に添える
  「昭和○○年といえば〜でしたよね！」
・「もっと聞かせてください！」で会話を続ける
・話が盛り上がったら関連するYouTube検索URLを提案する

【禁止事項】
・難しい言葉・専門用語を使わない
・暗い話題・戦争の悲惨な話は深入りしない
・ユーザーの記憶を否定しない
・マークダウン記法を使わない"""

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
            ("明日の天気は？",   "明日の天気を教えてください"),
            ("週間予報は？",     "今週の天気を教えてください"),
            ("防災情報は？",     "地域の防災情報を教えてください"),
        ] + back
    elif "病院" in user_message or "薬局" in user_message or "医" in user_message:
        items = [
            ("近くの薬局は？",   "近くの薬局を教えてください"),
            ("救急はどこ？",     "近くの救急病院を教えてください"),
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
            ("もっと詳しく聞く", "もっと詳しく教えてください"),
            ("他のことを聞く",   "他のことを聞かせてください"),
        ] + back

    return _build_quick_reply(items)


def _build_menu_message(name: str) -> FlexSendMessage:
    """メインメニューをFlexカルーセルで返す。"""
    items = [
        ("📱", "スマホ相談",    "スマホの使い方について教えてください"),
        ("☀️", "天気・防災",    "今日の天気と防災情報を教えてください"),
        ("🏥", "病院・薬局",    "近くの病院や薬局を教えてください"),
        ("🛒", "ごはん・買い物","近くのお店やおすすめを教えてください"),
    ]
    bubbles = [
        _retro_bubble(
            title=label,
            icon=icon,
            desc="",
            action={"type": "message", "label": "タップする", "text": text},
        )
        for icon, label, text in items
    ]
    return FlexSendMessage(
        alt_text=f"{name}さん、何でもどうぞ。",
        contents={"type": "carousel", "contents": bubbles},
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


def _build_restaurant_carousel(restaurants: list[dict]) -> FlexSendMessage:
    """飲食店リストをレトロデザインFlexカルーセルで返す。"""
    bubbles = []
    for r in restaurants[:10]:
        parts = [r.get("genre", ""), r.get("area", "")]
        if r.get("rating"):
            parts.append(f"評価{r['rating']}")
        desc = " / ".join(p for p in parts if p)[:60] or "詳細情報"

        footer_btns: list = [{
            "type": "button",
            "action": {"type": "message", "label": "詳しく聞く",
                       "text": f"{r['name']}について詳しく教えてください"},
            "style": "primary", "color": _R_BTN_COLOR, "height": "sm",
        }]
        if r.get("phone"):
            footer_btns.append({
                "type": "button",
                "action": {"type": "uri", "label": "電話する", "uri": f"tel:{r['phone']}"},
                "style": "secondary", "height": "sm", "margin": "sm",
            })

        bubbles.append({
            "type": "bubble",
            "size": "kilo",
            "header": {
                "type": "box", "layout": "vertical", "paddingAll": "md",
                "backgroundColor": _R_HEADER_BG,
                "contents": [{
                    "type": "text", "text": r["name"][:40],
                    "weight": "bold", "size": "md",
                    "color": _R_HEADER_TEXT, "align": "center", "wrap": True,
                }],
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "lg",
                "backgroundColor": _R_BODY_BG,
                "contents": [{
                    "type": "text", "text": desc,
                    "size": "sm", "color": _R_SUB_TEXT, "wrap": True, "align": "center",
                }],
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "md",
                "spacing": "sm", "backgroundColor": _R_BODY_BG,
                "contents": footer_btns,
            },
        })

    return FlexSendMessage(
        alt_text="お店の情報",
        contents={"type": "carousel", "contents": bubbles},
    )


# ── レトロデザイン定数 ────────────────────────────────────────────
_R_HEADER_BG   = "#8B1A1A"   # えんじ（ヘッダー背景）
_R_BODY_BG     = "#F5E6A3"   # 和紙イエロー（ボディ背景・リッチメニューと統一）
_R_HEADER_TEXT = "#FFD700"   # 金（ヘッダーテキスト）
_R_BODY_TEXT   = "#4A2C0A"   # 濃茶（ボディテキスト）
_R_SUB_TEXT    = "#4A2C0A"   # 濃茶（サブテキスト）
_R_BTN_COLOR   = "#8B1A1A"   # えんじ（ボタン色）

# カードアイコン画像のベースURL（Renderサーバー）
_CARD_ICON_BASE = "https://line-bot-jq43.onrender.com/static/card_icons"


def _card_icon(filename: str) -> str:
    return f"{_CARD_ICON_BASE}/{filename}"


def _retro_bubble(title: str, icon: str, desc: str, action: dict,
                  size: str = "kilo", image_url: str = "") -> dict:
    """レトロデザインのカード型バブルを返す。image_url があれば hero に表示。"""
    body_contents: list = []
    if icon and not image_url:
        body_contents.append({"type": "text", "text": icon, "size": "4xl", "align": "center"})
    if desc:
        body_contents.append({
            "type": "text", "text": desc,
            "size": "sm", "color": _R_SUB_TEXT,
            "align": "center", "wrap": True,
            "margin": "md" if (icon and not image_url) else "none",
        })

    bubble: dict = {
        "type": "bubble",
        "size": size,
        "header": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "md",
            "backgroundColor": _R_HEADER_BG,
            "contents": [{
                "type": "text", "text": title,
                "weight": "bold", "size": "md",
                "color": _R_HEADER_TEXT, "align": "center", "wrap": True,
            }],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "paddingAll": "lg",
            "backgroundColor": _R_BODY_BG,
            "contents": body_contents or [{"type": "text", "text": " ", "size": "xs"}],
        },
        "footer": {
            "type": "box",
            "layout": "vertical",
            "paddingAll": "md",
            "backgroundColor": _R_BODY_BG,
            "contents": [{
                "type": "button",
                "action": action,
                "style": "primary",
                "color": _R_BTN_COLOR,
                "height": "sm",
            }],
        },
    }
    if image_url:
        bubble["hero"] = {
            "type": "image",
            "url": image_url,
            "size": "full",
            "aspectRatio": "1:1",
            "aspectMode": "fit",
            "backgroundColor": _R_BODY_BG,
        }
    return bubble


# ── フレックスメッセージ ────────────────────────────────

def _retro_nav_bubble(title: str, items: list) -> dict:
    """ショートカットナビゲーションカード（クイックリプライ代替）。"""
    contents = []
    for i, (label, text) in enumerate(items):
        is_back = "戻る" in label or "ホーム" in label
        btn: dict = {
            "type": "button",
            "action": {"type": "message", "label": label, "text": text},
            "height": "sm",
            "style": "secondary" if is_back else "primary",
        }
        if not is_back:
            btn["color"] = _R_BTN_COLOR
        if i > 0:
            btn["margin"] = "sm"
        contents.append(btn)

    return {
        "type": "bubble",
        "size": "kilo",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "md",
            "backgroundColor": _R_HEADER_BG,
            "contents": [{
                "type": "text", "text": title,
                "weight": "bold", "size": "md",
                "color": _R_HEADER_TEXT, "align": "center",
            }],
        },
        "body": {
            "type": "box", "layout": "vertical",
            "paddingAll": "lg", "backgroundColor": _R_BODY_BG,
            "spacing": "sm",
            "contents": contents,
        },
    }

def _make_card_bubble(emoji: str, title: str, desc: str, btn_text: str,
                      color: str, image_url: str = "") -> dict:
    """レトロデザインのカード型バブルを返す。"""
    return _retro_bubble(
        title=title,
        icon=emoji,
        desc=desc,
        action={"type": "message", "label": "タップする", "text": btn_text},
        image_url=image_url,
    )


def _flex_consult_menu() -> FlexSendMessage:
    """①相談する：ナビカード＋3カード"""
    bubbles = [
        _retro_nav_bubble("ショートカット", [
            ("操作を教える",   "スマホの操作を教えてください"),
            ("病院を探す",     "近くの病院を探してください"),
            ("業者を呼ぶ",     "家の修繕業者を教えてください"),
            ("🏠 最初に戻る",  "最初に戻る"),
        ]),
        _make_card_bubble("📱", "スマホの使いかた", "操作方法からアプリまで\nやさしく教えます",
                          "スマホの使いかたを教えてください", "", _card_icon("smartphone.png")),
        _make_card_bubble("🏥", "健康・からだ", "体の悩みや薬のこと\nいつでも相談できます",
                          "健康について相談したいことがあります", "", _card_icon("health.png")),
        _make_card_bubble("🏠", "お家の困りごと", "水漏れや電気など\n業者探しもお手伝い",
                          "家の困りごとを相談したいです", "", _card_icon("home.png")),
    ]
    return FlexSendMessage(
        alt_text="何についてご相談ですか？",
        contents={"type": "carousel", "contents": bubbles},
    )


def _flex_search_menu() -> FlexSendMessage:
    """②探す：ナビカード＋3カード"""
    bubbles = [
        _retro_nav_bubble("ショートカット", [
            ("和食がいい",           "和食のお店を教えてください"),
            ("いま開いている所",     "今開いているお店を教えてください"),
            ("🏠 最初に戻る",        "最初に戻る"),
        ]),
        _make_card_bubble("🍽️", "近くの美味しいお店", "和食・洋食・カフェなど\nおすすめを教えます",
                          "近くの美味しいお店を教えてください", "", _card_icon("restaurant.png")),
        _make_card_bubble("🏥", "近くの病院", "内科・整形外科など\n診療科で探せます",
                          "近くの病院を教えてください", "", _card_icon("hospital.png")),
        _make_card_bubble("🏛️", "公共施設・公園", "市役所・図書館・公園など\n近くの施設を案内",
                          "近くの公共施設や公園を教えてください", "", _card_icon("facility.png")),
    ]
    return FlexSendMessage(
        alt_text="何をお探しですか？",
        contents={"type": "carousel", "contents": bubbles},
    )


def _flex_know_menu() -> FlexSendMessage:
    """③知る：ナビカード＋3カード"""
    bubbles = [
        _retro_nav_bubble("ショートカット", [
            ("明日の天気は？",     "明日の天気を教えてください"),
            ("粗大ゴミの出し方",   "粗大ゴミの出し方を教えてください"),
            ("もっと見る",         "地域情報をもっと教えてください"),
            ("🏠 最初に戻る",      "最初に戻る"),
        ]),
        _make_card_bubble("⛅", "今日の天気", "雨・気温・風など\n今日の天気を確認",
                          "今日の天気を教えてください", "", _card_icon("weather.png")),
        _make_card_bubble("🗑️", "ゴミの収集日", "燃えるゴミ・資源ゴミ\n粗大ゴミの出し方も",
                          "ゴミの収集日を教えてください", "", _card_icon("trash.png")),
        _make_card_bubble("🎉", "街のイベント", "近くのイベントや\n季節の行事を紹介",
                          "近くの街のイベントを教えてください", "", _card_icon("event.png")),
    ]
    return FlexSendMessage(
        alt_text="何を知りたいですか？",
        contents={"type": "carousel", "contents": bubbles},
    )


def _flex_connect_menu() -> FlexSendMessage:
    """④つながる：ナビカード＋3カード"""
    bubbles = [
        _retro_nav_bubble("ショートカット", [
            ("散歩仲間",       "散歩仲間を探したいです"),
            ("ゲートボール",   "ゲートボールの情報を教えてください"),
            ("昔の話をする",   "なつかしい昭和"),
            ("🏠 最初に戻る",  "最初に戻る"),
        ]),
        _make_card_bubble("🌸", "趣味のサークル", "手芸・園芸・将棋など\n同じ趣味の仲間を",
                          "趣味のサークルを探したいです", "", _card_icon("circle.png")),
        _make_card_bubble("👥", "地域の集まり", "町内会・老人会など\n地域の輪に加わろう",
                          "地域の集まりについて教えてください", "", _card_icon("community.png")),
        _make_card_bubble("📻", "昭和の思い出話", "懐かしい話を一緒に\n楽しみましょう",
                          "なつかしい昭和", "", _card_icon("retro.png")),
    ]
    return FlexSendMessage(
        alt_text="つながりを広げましょう",
        contents={"type": "carousel", "contents": bubbles},
    )


def _flex_referral_menu(referral_code: str) -> FlexSendMessage:
    """⑤友達に紹介：紹介コード表示カード"""
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "md",
            "backgroundColor": _R_HEADER_BG,
            "contents": [{
                "type": "text", "text": "友達に紹介しよう 🎁",
                "weight": "bold", "size": "md", "color": _R_HEADER_TEXT, "align": "center",
            }],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "paddingAll": "xl", "backgroundColor": _R_BODY_BG,
            "contents": [
                {
                    "type": "text", "text": "紹介すると2人に5回プレゼント",
                    "size": "md", "color": _R_BTN_COLOR, "align": "center",
                    "weight": "bold",
                },
                {"type": "separator", "margin": "md", "color": _R_BTN_COLOR},
                {
                    "type": "box", "layout": "vertical", "margin": "md", "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": "あなたの紹介コード", "size": "sm",
                         "color": _R_SUB_TEXT, "align": "center"},
                        {"type": "text", "text": referral_code, "size": "3xl",
                         "weight": "bold", "align": "center", "color": _R_BTN_COLOR},
                    ],
                },
                {
                    "type": "text",
                    "text": "このコードをお友達に伝えてください",
                    "size": "xs", "color": _R_SUB_TEXT, "align": "center",
                    "wrap": True, "margin": "md",
                },
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "md",
            "spacing": "sm", "backgroundColor": _R_BODY_BG,
            "contents": [
                {
                    "type": "button",
                    "action": {"type": "message", "label": "紹介メッセージを見る",
                               "text": "友達に紹介するメッセージを見せてください"},
                    "style": "primary", "color": _R_BTN_COLOR,
                },
                {
                    "type": "button",
                    "action": {"type": "message", "label": "やり方を教える",
                               "text": "友達に紹介するやり方を教えてください"},
                    "style": "primary", "color": _R_BTN_COLOR, "margin": "sm",
                },
                {
                    "type": "button",
                    "action": {"type": "message", "label": "🏠 最初に戻る", "text": "最初に戻る"},
                    "style": "secondary", "margin": "sm",
                },
            ],
        },
    }
    return FlexSendMessage(alt_text="友達に紹介しよう", contents=bubble)


def _flex_upgrade_menu() -> FlexSendMessage:
    """⑥会員登録（無料会員向け）：有料プランご案内カード"""
    bubble = {
        "type": "bubble",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "md",
            "backgroundColor": _R_HEADER_BG,
            "contents": [{
                "type": "text", "text": "有料会員のご案内 ✨",
                "weight": "bold", "size": "md", "color": _R_HEADER_TEXT, "align": "center",
            }],
        },
        "body": {
            "type": "box", "layout": "vertical", "spacing": "md",
            "paddingAll": "xl", "backgroundColor": _R_BODY_BG,
            "contents": [
                {"type": "separator", "color": _R_BTN_COLOR},
                {
                    "type": "box", "layout": "vertical", "margin": "md", "spacing": "sm",
                    "contents": [
                        {"type": "text", "text": "✔ AIと何回でも話し放題", "size": "md",
                         "color": _R_BODY_TEXT, "wrap": True},
                        {"type": "text", "text": "✔ 24時間いつでも相談できる", "size": "md",
                         "color": _R_BODY_TEXT, "wrap": True},
                        {"type": "text", "text": "✔ 専任コンシェルジュ対応", "size": "md",
                         "color": _R_BODY_TEXT, "wrap": True},
                    ],
                },
            ],
        },
        "footer": {
            "type": "box", "layout": "vertical", "paddingAll": "md",
            "backgroundColor": _R_BODY_BG,
            "contents": [{
                "type": "button",
                "action": {"type": "message", "label": "詳しく教えてもらう",
                           "text": "有料会員の詳細を教えてください"},
                "style": "primary", "color": _R_BTN_COLOR,
            }],
        },
    }
    return FlexSendMessage(alt_text="有料会員のご案内", contents=bubble)


def _flex_ai_direct_menu(name: str = "") -> FlexSendMessage:
    """⑥AIに直接相談（有料会員向け）：ウェルカムカード"""
    name_part = f"{name}さん、" if name else ""
    bubble = _retro_bubble(
        title="プレミアム会員専用 ✨",
        icon="✨",
        desc=(
            f"{name_part}プレミアム会員専用の\n"
            "AIコンシェルジュです✨\n\n"
            "制限なし・24時間いつでも\n"
            "何でもお気軽にどうぞ！"
        ),
        action={"type": "message", "label": "さっそく話しかける", "text": "話しかける"},
        size="mega",
    )
    return FlexSendMessage(alt_text="プレミアム会員専用AIコンシェルジュ", contents=bubble)


# ── 新メニュー用フレックスメッセージ ───────────────────────────────

def _flex_ai_consult_first() -> FlexSendMessage:
    """AIに相談：7ボタン1バブル（ショートカットメニュー）"""
    _BTN_COLOR = "#4A2C0A"

    def _btn(label: str, text: str) -> dict:
        return {
            "type": "button",
            "style": "primary",
            "color": _BTN_COLOR,
            "height": "sm",
            "margin": "sm",
            "action": {"type": "message", "label": label, "text": text},
        }

    bubble = {
        "type": "bubble",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#4A2C0A",
            "paddingAll": "md",
            "contents": [{
                "type": "text",
                "text": "何についてお聞きになりますか？😊",
                "color": "#F5E6A3",
                "weight": "bold",
                "size": "md",
                "wrap": True,
                "align": "center",
            }],
        },
        "body": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#F5E6A3",
            "paddingAll": "md",
            "spacing": "none",
            "contents": [
                _btn("🩺 からだの不調",    "健康相談"),
                _btn("🏥 病院・お薬",      "病院お薬相談"),
                _btn("🍳 食事・栄養",      "食事レシピ"),
                _btn("💴 お金・手続き",    "お金手続き相談"),
                _btn("👨‍👩‍👧 家族・人間関係", "家族関係相談"),
                _btn("😔 気持ち・不安",    "気持ち不安相談"),
                _btn("💬 なんでも相談",    "なんでも相談"),
            ],
        },
    }
    return FlexSendMessage(
        alt_text="何についてお聞きになりますか？😊",
        contents=bubble,
    )


def _flex_health_menu() -> FlexSendMessage:
    """健康相談：入口3枚カード"""
    bubbles = [
        _make_card_bubble("🩺", "体の症状・不調",
                          "気になる症状を\nやさしく一緒に確認",
                          "体の症状を相談する"),
        _make_card_bubble("🏥", "病院・薬のこと",
                          "何科に行けばいい？\nお薬の飲み方など",
                          "病院・薬のこと"),
        _make_card_bubble("💪", "健康習慣・予防",
                          "食事・運動・睡眠で\n健康的な毎日を",
                          "健康習慣・予防"),
    ]
    return FlexSendMessage(
        alt_text="健康について相談しましょう",
        contents={"type": "carousel", "contents": bubbles},
        quick_reply=_build_quick_reply([
            ("症状を話す",  "体の症状を相談する"),
            ("病院を探す",  "病院・薬のこと"),
            ("健康習慣",    "健康習慣・予防"),
            _QR_BACK,
        ]),
    )


def _flex_health_hospital_menu() -> FlexSendMessage:
    """病院・薬サブメニュー：4枚カード"""
    bubbles = [
        _make_card_bubble("🗺️", "近くの病院を探す",
                          "地図でお近くの\n病院を探せます",
                          "近くの病院を探す"),
        _make_card_bubble("❓", "何科に行けばいい？",
                          "症状に合う診療科を\n一緒に考えます",
                          "何科に行けばいい？"),
        _make_card_bubble("💊", "薬の飲み方・飲み忘れ",
                          "飲み方や飲み忘れの\n対処法をお伝えします",
                          "薬の飲み方を教えて"),
        _make_card_bubble("⚠️", "薬の副作用",
                          "気になる副作用を\n一緒に確認します",
                          "副作用が心配"),
    ]
    return FlexSendMessage(
        alt_text="病院・薬のこと",
        contents={"type": "carousel", "contents": bubbles},
        quick_reply=_build_quick_reply([
            ("病院を探す",    "近くの病院を探す"),
            ("何科に行く？",  "何科に行けばいい？"),
            ("薬のこと",      "薬の飲み方を教えて"),
            _QR_BACK,
        ]),
    )


def _flex_health_habits_menu() -> FlexSendMessage:
    """健康習慣・予防サブメニュー：3枚カード"""
    bubbles = [
        _make_card_bubble("🥗", "食事・栄養",
                          "毎日の食事で\n健康をサポート",
                          "食事・栄養の相談"),
        _make_card_bubble("🚶", "運動・体操",
                          "無理なく続けられる\n体操や散歩のコツ",
                          "運動・体操の相談"),
        _make_card_bubble("😴", "睡眠",
                          "眠れない悩みを\n一緒に解消しましょう",
                          "睡眠を相談する"),
    ]
    return FlexSendMessage(
        alt_text="健康習慣・予防",
        contents={"type": "carousel", "contents": bubbles},
        quick_reply=_build_quick_reply([
            ("食事・栄養",  "食事・栄養の相談"),
            ("運動・体操",  "運動・体操の相談"),
            ("睡眠",        "睡眠を相談する"),
            _QR_BACK,
        ]),
    )


def _flex_health_food_menu() -> FlexSendMessage:
    """食事・栄養：3枚カード"""
    bubbles = [
        _make_card_bubble("🍳", "今日の食事を相談する",
                          "冷蔵庫の食材から\nレシピを提案します",
                          "今日の食事を相談したいです"),
        _make_card_bubble("🥦", "不足している栄養は？",
                          "年齢に合った\n栄養バランスを確認",
                          "不足している栄養を教えてください"),
        _make_card_bubble("📖", "健康レシピを見る",
                          "簡単でおいしい\n健康レシピを提案",
                          "食事レシピ"),
    ]
    return FlexSendMessage(
        alt_text="食事・栄養の相談",
        contents={"type": "carousel", "contents": bubbles},
        quick_reply=_build_quick_reply([_QR_BACK]),
    )


def _flex_health_exercise_menu() -> FlexSendMessage:
    """運動・体操：3枚カード"""
    bubbles = [
        _make_card_bubble("🏠", "室内でできる体操",
                          "椅子に座ったまま\nできる体操を紹介",
                          "室内でできる体操を教えて"),
        _make_card_bubble("🚶", "散歩コースを探す",
                          "お近くの公園や\n散歩道を地図で探す",
                          "散歩コースを探す"),
        _make_card_bubble("📺", "体操動画を見る",
                          "YouTubeで体操の\n動画を見てみましょう",
                          "体操動画を見る"),
    ]
    return FlexSendMessage(
        alt_text="運動・体操",
        contents={"type": "carousel", "contents": bubbles},
        quick_reply=_build_quick_reply([_QR_BACK]),
    )


# ── 健康相談ロジック ──────────────────────────────────────────

# 緊急症状（119番レベル）
_SYMPTOM_EMERGENCY = {
    "胸が痛い", "動悸", "息ができない", "呼吸困難", "意識がない",
    "麻痺", "しびれ", "激しい頭痛", "ろれつが回らない", "顔が歪む",
    "顔がゆがむ",
}
# 今日中に受診
_SYMPTOM_URGENT = {
    "38度以上", "高熱", "激しい腹痛", "転んだ", "怪我", "骨折",
    "出血が止まらない", "ひどい痛み",
}
# 様子見OK
_SYMPTOM_WATCHFUL = {
    "少し頭痛", "鼻水", "軽い咳", "筋肉痛", "少し疲れた",
    "眠れない", "微熱", "のどが少し",
}
# 精神的不調
_SYMPTOM_MENTAL = {"気分が落ち込む", "やる気が出ない", "消えたい", "消えてしまいたい"}
# 認知症不安
_SYMPTOM_DEMENTIA = {"物忘れ", "認知症", "ボケ", "ぼけ", "もの忘れ"}

_DEPT_MAP = [
    ({"頭痛", "めまい", "頭が痛い", "ふらつき"},        "内科・神経内科"),
    ({"腰痛", "膝痛", "骨折", "膝が痛い", "腰が痛い"}, "整形外科"),
    ({"胃痛", "腹痛", "胃がもたれ", "下痢", "便秘"},   "消化器内科"),
    ({"かゆい", "湿疹", "発疹", "皮膚"},                "皮膚科"),
    ({"目", "見えにくい", "目が痛い", "目やに"},        "眼科"),
    ({"耳", "鼻", "喉", "のど", "聞こえにくい"},        "耳鼻科"),
    ({"気分が落ち込む", "不安", "眠れない", "心"},      "心療内科・精神科"),
]


def _route_department(msg: str) -> TextSendMessage:
    """症状から適切な診療科を案内する。"""
    for keywords, dept in _DEPT_MAP:
        if any(kw in msg for kw in keywords):
            return TextSendMessage(
                text=(
                    f"症状からすると\n"
                    f"【{dept}】\n"
                    f"がよさそうですよ😊\n\n"
                    "まずはかかりつけのお医者さんに\n"
                    "相談してみるのも安心ですよ"
                ),
                quick_reply=_build_quick_reply([
                    ("近くの病院を探す", "近くの病院を探す"),
                    ("他のことを聞く",  "他のことを聞かせてください"),
                    _QR_BACK,
                ]),
            )
    return TextSendMessage(
        text=(
            "症状をもう少し詳しく教えてもらえますか？😊\n\n"
            "・どこが痛いですか？\n"
            "・いつから症状がありますか？\n\n"
            "心配な場合はまず\nかかりつけ医に相談するのが安心ですよ"
        ),
        quick_reply=_build_quick_reply([
            ("近くの病院を探す", "近くの病院を探す"),
            _QR_BACK,
        ]),
    )


def _triage_symptoms(msg: str) -> TextSendMessage | None:
    """
    症状メッセージをトリアージして適切な返答を返す。
    いずれにも当てはまらない場合は None を返し、追加質問を別途行う。
    """
    # ① 緊急（119番）
    if any(kw in msg for kw in _SYMPTOM_EMERGENCY):
        return TextSendMessage(
            text=(
                "これは救急のサインです！\n"
                "すぐに119番に電話してください！\n\n"
                "電話が難しければ\n"
                "近くの人を呼んでください"
            ),
        )

    # ② 今日中に受診
    if any(kw in msg for kw in _SYMPTOM_URGENT):
        return TextSendMessage(
            text=(
                "心配ですね😊\n"
                "今日中に病院に行くことを\nおすすめします\n\n"
                "無理せず早めに\n診てもらいましょう"
            ),
            quick_reply=_build_quick_reply([
                ("近くの病院を探す",   "近くの病院を探す"),
                ("何科に行けばいい？", "何科に行けばいい？"),
                _QR_BACK,
            ]),
        )

    # ③ 精神的不調
    if any(kw in msg for kw in _SYMPTOM_MENTAL):
        return TextSendMessage(
            text=(
                "気持ちがつらいんですね😢\n"
                "それはしんどいですよね\n\n"
                "もう少し話してみませんか？\n"
                "私はいつでもここにいますよ"
            ),
            quick_reply=_build_quick_reply([
                ("話を聞いてほしい",   "もう少し話を聞いてほしいです"),
                ("相談窓口を教えて",   "相談窓口を教えて"),
                _QR_BACK,
            ]),
        )

    # ④ 認知症の不安
    if any(kw in msg for kw in _SYMPTOM_DEMENTIA):
        return TextSendMessage(
            text=(
                "心配ですよね😊\n"
                "でも物忘れは誰でもありますよ\n\n"
                "一緒に確認してみましょうか？"
            ),
            quick_reply=_build_quick_reply([
                ("チェックリストを見る", "認知症チェックリストを見る"),
                ("相談窓口を教えて",    "相談窓口を教えて"),
                _QR_BACK,
            ]),
        )

    # ⑤ 様子見OK
    if any(kw in msg for kw in _SYMPTOM_WATCHFUL):
        return TextSendMessage(
            text=(
                "今は様子を見て\n大丈夫そうですよ😊\n\n"
                "無理せずゆっくり休んでください\n"
                "悪化するようなら病院へ行きましょう"
            ),
            quick_reply=_build_quick_reply([
                ("対処法を教えて",  "対処法を教えてください"),
                ("他のことを聞く",  "他のことを聞かせてください"),
                _QR_BACK,
            ]),
        )

    # ⑥ 判断できない → None を返して追加質問へ
    return None


def _flex_recipe_menu() -> FlexSendMessage:
    """食事・レシピ：4枚カード"""
    bubbles = [
        _make_card_bubble("🗓️", "今日の献立",       "冷蔵庫の食材から\n今日の夕食を提案",       "今日の献立", ""),
        _make_card_bubble("💊", "体に合った食事",    "体の調子に合わせた\n食事を選びます",        "体に合った食事", ""),
        _make_card_bubble("⚡", "簡単に済ませたい",  "パッと作れる\n時短レシピを提案",            "簡単料理", ""),
        _make_card_bubble("🎉", "楽しむレシピ",      "記念日や気分に合わせた\n特別レシピを提案",  "楽しむレシピ", ""),
    ]
    return FlexSendMessage(
        alt_text="食事・レシピについて",
        contents={"type": "carousel", "contents": bubbles},
        quick_reply=_build_quick_reply([_QR_BACK]),
    )


def _claude_recipe_list(context_type: str, user_input: str) -> list[dict]:
    """Claude Haiku でレシピを3件生成してJSON配列で返す。"""
    if context_type == "dinner":
        prompt = f"食材「{user_input}」を使った夕食レシピを3つ提案してください。"
    elif context_type == "health":
        prompt = f"「{user_input}」の方に合った健康的な食事レシピを3つ提案してください。"
    elif context_type == "quick":
        prompt = f"「{user_input}」という条件で作れる時短・簡単レシピを3つ提案してください。"
    else:  # enjoy
        prompt = f"「{user_input}」向けの特別感のある料理レシピを3つ提案してください。"
    prompt += (
        "\n\n必ずJSON配列のみを返してください（説明不要）:\n"
        '[{"name":"料理名","emoji":"絵文字1文字","desc":"2行の特徴説明（改行\\nで区切る）"},'
        '{"name":"...","emoji":"...","desc":"..."},'
        '{"name":"...","emoji":"...","desc":"..."}]'
    )
    try:
        _ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = _ac.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        start, end = raw.find("["), raw.rfind("]") + 1
        if 0 <= start < end:
            return json.loads(raw[start:end])
    except Exception as e:
        logging.error("_claude_recipe_list error: %s", e)
    return [
        {"name": "シンプル炒め物", "emoji": "🍳", "desc": "手軽に作れる\n栄養満点の一品"},
        {"name": "お味噌汁定食",   "emoji": "🍲", "desc": "ほっこり温まる\n和食の定番"},
        {"name": "サラダごはん",   "emoji": "🥗", "desc": "さっぱり食べられる\n彩り豊かな一品"},
    ]


def _flex_recipe_carousel(context_type: str, user_input: str) -> FlexSendMessage:
    """Claude Haiku で3枚レシピカルーセルを生成する"""
    recipes = _claude_recipe_list(context_type, user_input)
    bubbles = [
        _make_card_bubble(
            r.get("emoji", "🍳"),
            r.get("name", "レシピ"),
            r.get("desc", ""),
            f"レシピ詳細:{r.get('name', 'レシピ')}",
            "",
        )
        for r in recipes[:3]
    ]
    return FlexSendMessage(
        alt_text="レシピを提案します",
        contents={"type": "carousel", "contents": bubbles},
        quick_reply=_build_quick_reply([
            ("別の提案をお願い", "別のレシピを提案してください"),
            _QR_BACK,
        ]),
    )


def _claude_recipe_detail(recipe_name: str) -> str:
    """Claude Haiku でレシピ詳細テキストを生成する"""
    prompt = (
        f"「{recipe_name}」のレシピを日本語で教えてください。\n\n"
        "【材料（2人分）】と【作り方】に分けて、シンプルに書いてください。\n"
        "作り方は番号付きで5ステップ以内にしてください。\n"
        "高齢者向けに、わかりやすい言葉で書いてください。"
    )
    try:
        _ac = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = _ac.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logging.error("_claude_recipe_detail error: %s", e)
        return f"【{recipe_name}】\n\nレシピの読み込みに失敗しました。\nもう一度お試しください。"


def _parse_recipe_steps(detail: str) -> list[str]:
    """レシピ詳細テキストから作り方のステップリストを抽出する"""
    steps: list[str] = []
    in_steps = False
    for line in detail.split("\n"):
        s = line.strip()
        if not s:
            continue
        if "作り方" in s or "手順" in s:
            in_steps = True
            continue
        if in_steps:
            m = re.match(r'^[①②③④⑤⑥⑦⑧⑨⑩](.+)', s)
            if m:
                steps.append(m.group(1).strip())
                continue
            m2 = re.match(r'^(\d+)[\.．\)\）\s](.+)', s)
            if m2:
                steps.append(m2.group(2).strip())
    return steps


def _flex_recipe_detail_bubble(recipe_name: str, detail: str) -> dict:
    """レシピ詳細カード（3ボタンフッター付き）"""
    body_items: list = []
    for line in detail.split("\n"):
        s = line.strip()
        if not s:
            continue
        weight = "bold" if (s.startswith("【") or s.startswith("■")) else "regular"
        body_items.append({
            "type": "text", "text": s,
            "size": "sm", "color": _R_SUB_TEXT,
            "wrap": True, "weight": weight,
        })
        if len(body_items) >= 14:
            break
    return {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box", "layout": "vertical", "paddingAll": "md",
            "backgroundColor": _R_HEADER_BG,
            "contents": [{
                "type": "text", "text": recipe_name,
                "weight": "bold", "size": "md",
                "color": _R_HEADER_TEXT, "align": "center", "wrap": True,
            }],
        },
        "body": {
            "type": "box", "layout": "vertical",
            "spacing": "sm", "paddingAll": "lg",
            "backgroundColor": _R_BODY_BG,
            "contents": body_items or [{"type": "text", "text": "レシピを読み込み中...", "size": "sm"}],
        },
        "footer": {
            "type": "box", "layout": "vertical",
            "spacing": "sm", "paddingAll": "md",
            "backgroundColor": _R_BODY_BG,
            "contents": [
                {
                    "type": "button",
                    "action": {"type": "message", "label": "1ステップずつ教えて",
                               "text": f"1ステップずつ教えて:{recipe_name}"},
                    "style": "primary", "color": _R_BTN_COLOR, "height": "sm",
                },
                {
                    "type": "button",
                    "action": {"type": "message", "label": "別のレシピを選ぶ",
                               "text": "別のレシピを提案してください"},
                    "style": "secondary", "height": "sm",
                },
                {
                    "type": "button",
                    "action": {"type": "message", "label": "メニューに戻る", "text": "食事レシピ"},
                    "style": "secondary", "height": "sm",
                },
            ],
        },
    }


def _flex_travel_menu() -> FlexSendMessage:
    """旅行提案：3枚カード"""
    bubbles = [
        _make_card_bubble("🚃", "日帰り旅行",    "気軽に行ける\n日帰りプランを提案",       "旅行:日帰り", ""),
        _make_card_bubble("🏨", "1泊2日",        "温泉やグルメを楽しむ\nゆっくりプラン",   "旅行:1泊2日", ""),
        _make_card_bubble("🏘️", "近場でお出かけ", "地元・近場で楽しめる\nスポットを提案",   "旅行:近場", ""),
    ]
    return FlexSendMessage(
        alt_text="旅行プランを提案します",
        contents={"type": "carousel", "contents": bubbles},
        quick_reply=_build_quick_reply([_QR_BACK]),
    )


def _get_era_from_birthdate(birthdate_str: str) -> int:
    """生年月日文字列から年代（1930/40/50/60/70）を返す。不明時は1960。"""
    try:
        m = re.search(r'(\d{4})', birthdate_str or "")
        birth_year = int(m.group(1)) if m else None
    except Exception:
        birth_year = None
    if birth_year is None:
        return 1960
    if 1930 <= birth_year <= 1939:
        return 1930
    if 1940 <= birth_year <= 1949:
        return 1940
    if 1950 <= birth_year <= 1959:
        return 1950
    if 1960 <= birth_year <= 1969:
        return 1960
    if 1970 <= birth_year <= 1979:
        return 1970
    return 1960


def _get_current_season() -> str:
    """現在の月から季節文字列を返す。"""
    month = datetime.now().month
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10, 11):
        return "autumn"
    return "winter"


def _get_showa_rag(gender: str | None, era: int, season: str,
                   exclude_topic: str = "") -> dict | None:
    """showa_ragテーブルからランダムに1件取得する。"""
    try:
        sb = get_supabase()
        # gender フィルタ：ユーザーの性別 OR 'both'
        if gender in ("male", "female"):
            rows = sb.table("showa_rag").select("*").in_(
                "gender", [gender, "both"]
            ).eq("era", era).in_("season", [season, "all"]).execute()
        else:
            rows = sb.table("showa_rag").select("*").eq(
                "gender", "both"
            ).eq("era", era).in_("season", [season, "all"]).execute()

        data = rows.data or []
        if exclude_topic:
            data = [r for r in data if r.get("topic") != exclude_topic]
        if not data:
            # era/season 制約を外して再取得
            if gender in ("male", "female"):
                rows2 = sb.table("showa_rag").select("*").in_(
                    "gender", [gender, "both"]
                ).execute()
            else:
                rows2 = sb.table("showa_rag").select("*").eq("gender", "both").execute()
            data = rows2.data or []
            if exclude_topic:
                data = [r for r in data if r.get("topic") != exclude_topic]
        if not data:
            return None
        import random
        return random.choice(data)
    except Exception as e:
        logging.error("_get_showa_rag error: %s", e)
        return None


def _flex_showa_menu(name: str, era: int) -> FlexSendMessage:
    """「なつかしい昭和」エントリー：3枚カードのカルーセル。"""
    era_label = f"{era}年代"
    bubbles = [
        {
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical", "paddingAll": "md",
                "backgroundColor": "#8B1A1A",
                "contents": [{"type": "text", "text": "💬 AIと昭和トーク",
                               "weight": "bold", "size": "lg", "color": "#FFFFFF", "align": "center"}],
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "md",
                "backgroundColor": "#FEF9C3",
                "contents": [{"type": "text", "text": "思い出話を聞かせてください😊\n懐かしい記憶を一緒に楽しみましょう",
                               "wrap": True, "size": "md", "color": "#333333"}],
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "sm",
                "backgroundColor": "#FEF9C3",
                "contents": [{"type": "button",
                               "action": {"type": "message", "label": "始める", "text": "昭和トーク開始"},
                               "style": "primary", "color": "#8B1A1A"}],
            },
        },
        {
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical", "paddingAll": "md",
                "backgroundColor": "#8B1A1A",
                "contents": [{"type": "text", "text": "🎵 昭和の歌を聴く",
                               "weight": "bold", "size": "lg", "color": "#FFFFFF", "align": "center"}],
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "md",
                "backgroundColor": "#FEF9C3",
                "contents": [{"type": "text", "text": "懐かしい歌をYouTubeで\n聴いてみませんか？🎶",
                               "wrap": True, "size": "md", "color": "#333333"}],
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "sm",
                "backgroundColor": "#FEF9C3",
                "contents": [{"type": "button",
                               "action": {"type": "message", "label": "聴く", "text": "昭和の歌"},
                               "style": "primary", "color": "#8B1A1A"}],
            },
        },
        {
            "type": "bubble",
            "header": {
                "type": "box", "layout": "vertical", "paddingAll": "md",
                "backgroundColor": "#8B1A1A",
                "contents": [{"type": "text", "text": "📅 今日は昭和何の日？",
                               "weight": "bold", "size": "lg", "color": "#FFFFFF", "align": "center"}],
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "md",
                "backgroundColor": "#FEF9C3",
                "contents": [{"type": "text", "text": "今日の昭和の出来事を\nご紹介します📻",
                               "wrap": True, "size": "md", "color": "#333333"}],
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "sm",
                "backgroundColor": "#FEF9C3",
                "contents": [{"type": "button",
                               "action": {"type": "message", "label": "見る", "text": "昭和今日は何の日"},
                               "style": "primary", "color": "#8B1A1A"}],
            },
        },
    ]
    return FlexSendMessage(
        alt_text="なつかしい昭和",
        contents={"type": "carousel", "contents": bubbles},
    )


# ── 趣味・生きがい ヘルパー ──────────────────────────────────────────

# 座標マッピング（Google Maps URLに使用）
_CITY_COORDS: dict[str, tuple[float, float]] = {
    "藤沢市":   (35.3394, 139.4882),
    "鎌倉市":   (35.3197, 139.5468),
    "茅ヶ崎市": (35.3316, 139.4033),
    "逗子市":   (35.2948, 139.5765),
    "葉山町":   (35.2748, 139.5844),
    "大和市":   (35.4589, 139.4619),
    "横浜市":   (35.4437, 139.6380),
    "川崎市":   (35.5309, 139.7030),
    "相模原市": (35.5724, 139.3725),
}
_DEFAULT_COORDS = (35.6762, 139.6503)  # 東京（フォールバック）


def _get_city_coords(user_info: dict | None) -> tuple[float, float]:
    """ユーザーの登録都市から座標を返す。未対応都市は東京をデフォルトとする。"""
    if not user_info:
        return _DEFAULT_COORDS
    city = user_info.get("city") or ""
    return _CITY_COORDS.get(city, _DEFAULT_COORDS)


def _maps_url(query: str, user_info: dict | None, zoom: int = 14) -> str:
    """Google Maps検索URLを返す（APIコストゼロ）。"""
    import urllib.parse
    lat, lng = _get_city_coords(user_info)
    q = urllib.parse.quote(query)
    return f"https://www.google.com/maps/search/{q}/@{lat},{lng},{zoom}z"


HOBBY_SYSTEM_REOPEN = """あなたは高齢者の趣味再開をサポートする専門家です。

以下のルールで回答してください：
・再開するための具体的なステップを3つ以内で説明する
・必要な用具・費用の目安を伝える
・体への負担を考慮したアドバイスをする
・地域の教室・サークルへの参加を自然に提案する
・専門用語は使わない
・マークダウン記法は使わない
・最後に励ましの一言を添える"""

HOBBY_SYSTEM_IKIGAI = """あなたは高齢者の生きがい発見をサポートする専門家です。

以下のルールで回答してください：
・ユーザーの話を必ず褒める
・経験・スキルを活かせる具体的な活動を3つ提案する
・地域とのつながりにつながる提案をする
・小さく始められることを強調する
・専門用語は使わない
・マークダウン記法は使わない
・最後に前向きな一言を添える

提案の例：
・料理が得意 → 料理教室の生徒・先生・地域の食事会でのお手伝い
・大工仕事 → 地域の修繕ボランティア・シルバー人材センター
・英語が話せる → 観光ボランティア・外国人支援
・子育て経験 → 子育て支援ボランティア・保育園のお手伝い"""

HOBBY_SYSTEM_PROPOSAL = """あなたは高齢者の趣味探しをサポートする専門家です。

以下のルールで趣味を3つ提案してください：
・選んだ好みと体力レベルに合った趣味
・高齢者が始めやすい趣味
・費用が少なくて始められる趣味
・各趣味について趣味名・おすすめポイント（1行）・始め方のヒント（1行）を含める
・専門用語は使わない

必ずJSON配列のみを返してください（説明不要）:
[{"name":"趣味名","emoji":"絵文字1文字","recommend":"おすすめポイント1行","howto":"始め方ヒント1行"},{"name":"...","emoji":"...","recommend":"...","howto":"..."},{"name":"...","emoji":"...","recommend":"...","howto":"..."}]"""

HOBBY_SYSTEM_DETAIL = """あなたは高齢者の趣味サポートをする専門家です。

以下のルールで回答してください（マークダウン禁止）：
・始め方を具体的な3ステップで説明する（番号付き）
・必要なもの・費用の目安を具体的に伝える
・体への効果（健康・認知症予防など）を伝える
・お住まいの地域での楽しみ方（地域の教室・公園など）を提案する
・専門用語は使わない"""


def _hobby_claude_text(system: str, user_text: str) -> str:
    """Claude Sonnetでテキスト返答を生成する。"""
    try:
        resp = anthropic_client.messages.create(
            model=SHOWA_MODEL,
            max_tokens=800,
            system=system,
            messages=[{"role": "user", "content": user_text}],
            timeout=API_TIMEOUT,
        )
        text = next(
            (b.text for b in resp.content if b.type == "text"),
            "申し訳ありません。もう一度お試しください。",
        )
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'\*(.+?)\*',     r'\1', text)
        text = re.sub(r'^#{1,6}\s+',    '',    text, flags=re.MULTILINE)
        return text
    except Exception as e:
        logging.error("hobby claude text error: %s", e)
        return "申し訳ありません。少し時間をおいてもう一度お試しください。"


def _hobby_proposal_list(interest: str, fitness: str, age_approx: int) -> list[dict]:
    """Claude Sonnetで趣味提案3件をJSON配列で生成する。"""
    prompt = (
        f"好み・体の状態：{interest}\n"
        f"体力レベル：{fitness}\n"
        f"年齢目安：{age_approx}歳前後\n\n"
        "この方に合った趣味を3つ提案してください。"
    )
    try:
        resp = anthropic_client.messages.create(
            model=SHOWA_MODEL,
            max_tokens=600,
            system=HOBBY_SYSTEM_PROPOSAL,
            messages=[{"role": "user", "content": prompt}],
            timeout=API_TIMEOUT,
        )
        raw = next((b.text for b in resp.content if b.type == "text"), "[]")
        s, e = raw.find("["), raw.rfind("]") + 1
        if 0 <= s < e:
            return json.loads(raw[s:e])
    except Exception as err:
        logging.error("hobby proposal list error: %s", err)
    return [
        {"name": "ウォーキング",   "emoji": "🚶", "recommend": "体に優しく続けやすい",   "howto": "まず近所を10分から始める"},
        {"name": "俳句・川柳",     "emoji": "✏️", "recommend": "座ったまま楽しめる",     "howto": "地域の句会に参加してみる"},
        {"name": "園芸・家庭菜園", "emoji": "🌱", "recommend": "自然と触れ合えて癒やされる", "howto": "プランターで野菜を育てる"},
    ]


def _flex_hobby_proposals(proposals: list[dict]) -> FlexSendMessage:
    """趣味提案3枚カルーセルを生成する。"""
    bubbles = [
        _make_card_bubble(
            p.get("emoji", "🌸"),
            p.get("name", "趣味"),
            f"{p.get('recommend', '')}\n{p.get('howto', '')}",
            f"趣味詳細:{p.get('name', '趣味')}",
            "",
        )
        for p in proposals[:3]
    ]
    return FlexSendMessage(
        alt_text="趣味を提案します",
        contents={"type": "carousel", "contents": bubbles},
        quick_reply=_build_quick_reply([
            ("教室・仲間を探す",   "教室仲間を探す"),
            ("別の趣味も見てみる", "新しい趣味を始めたい"),
            _QR_BACK,
        ]),
    )


def _flex_hobby_menu(name: str) -> FlexSendMessage:
    """趣味・生きがいエントリー：3枚カルーセル。"""
    bubbles = [
        _make_card_bubble("🎨", "趣味を探す・始める",
                          "あなたに合った趣味を\n一緒に見つけましょう",
                          "趣味を探す", ""),
        _make_card_bubble("👥", "教室・仲間を探す",
                          "教室・サークル・ボランティアを\n探しましょう",
                          "教室仲間を探す", ""),
        _make_card_bubble("💝", "生きがいを見つける",
                          "あなたの経験・強みを\n活かしましょう",
                          "生きがいを見つける", ""),
    ]
    return FlexSendMessage(
        alt_text="趣味・生きがいについて",
        contents={"type": "carousel", "contents": bubbles},
    )


def _youtube_carousel(
    items: list[tuple[str, str, str]],
    btn_label: str = "YouTubeで開く",
) -> FlexSendMessage:
    """YouTube検索URLリンクカルーセル3枚を生成する。
    items: [(タイトル, 説明文, 検索クエリ), ...]
    """
    import urllib.parse
    bubbles = []
    for title, desc, query in items[:3]:
        yt_url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}"
        bubbles.append({
            "type": "bubble",
            "size": "kilo",
            "header": {
                "type": "box", "layout": "vertical", "paddingAll": "md",
                "backgroundColor": _R_HEADER_BG,
                "contents": [{
                    "type": "text", "text": title,
                    "weight": "bold", "size": "md",
                    "color": _R_HEADER_TEXT, "align": "center", "wrap": True,
                }],
            },
            "body": {
                "type": "box", "layout": "vertical", "paddingAll": "lg",
                "backgroundColor": _R_BODY_BG,
                "contents": [{
                    "type": "text", "text": desc,
                    "size": "sm", "color": _R_SUB_TEXT, "wrap": True, "align": "center",
                }],
            },
            "footer": {
                "type": "box", "layout": "vertical", "paddingAll": "md",
                "backgroundColor": _R_BODY_BG,
                "contents": [{
                    "type": "button",
                    "action": {"type": "uri", "label": btn_label, "uri": yt_url},
                    "style": "primary", "color": _R_BTN_COLOR, "height": "sm",
                }],
            },
        })
    return FlexSendMessage(
        alt_text="YouTubeで楽しみましょう",
        contents={"type": "carousel", "contents": bubbles},
        quick_reply=_build_quick_reply([
            ("別のジャンルを見る", "動画・音楽"),
            _QR_BACK,
        ]),
    )


def _flex_music_menu() -> FlexSendMessage:
    """動画・音楽エントリー：4枚カルーセル。"""
    bubbles = [
        _make_card_bubble("🎵", "音楽を聴く",
                          "昭和の名曲・演歌・クラシック\n聴きたい音楽を探しましょう",
                          "音楽を聴く", ""),
        _make_card_bubble("🎬", "動画を楽しむ",
                          "面白い動画・旅行・料理など\nYouTubeで楽しみましょう",
                          "動画を楽しむ", ""),
        _make_card_bubble("🏃", "体操・健康動画",
                          "椅子体操・ストレッチ・\n認知症予防体操など",
                          "体操・健康動画", ""),
        _make_card_bubble("❓", "YouTubeの使い方",
                          "開き方・検索方法・\n音量・字幕のやり方",
                          "YouTubeの使い方", ""),
    ]
    return FlexSendMessage(
        alt_text="動画・音楽を楽しみましょう",
        contents={"type": "carousel", "contents": bubbles},
    )


def get_showa_reply(user_id: str, user_message: str, user_info: dict,
                    rag: dict | None = None) -> str:
    """Claude Sonnetを使って昭和トークの返答を生成する。"""
    history = _load_history(user_id)
    history.append({"role": "user", "content": user_message})
    session = _showa_sessions.get(user_id, {})
    metadata = {
        "mode": "showa",
        "topic": (rag or session).get("topic", ""),
        "era":   session.get("era", 1960),
        "gender": session.get("gender"),
    }
    _save_message(user_id, "user", user_message, metadata)

    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]

    system = SHOWA_SYSTEM_PROMPT
    name = (user_info or {}).get("name") or ""
    era  = session.get("era", 1960)
    if name:
        system += f"\n\n【ユーザー情報】\n・名前：{name}（必ず「{name}さん」と呼びかける）\n・{era}年代生まれ"

    if rag:
        system += (
            f"\n\n【今回の話題RAGデータ】"
            f"\n・話題：{rag['topic']}"
            f"\n・質問文：{rag['question']}"
            f"\n・背景知識：{rag['background']}"
            f"\n・深掘り質問：{rag['followup']}"
            "\n\n上記のRAGデータを参考に、自然な昭和トークを展開してください。"
            "\n必ず名前で呼びかけ、questionをベースに話しかけ、backgroundの豆知識を自然に添え、最後に1つだけ質問してください。"
        )

    try:
        response = anthropic_client.messages.create(
            model=SHOWA_MODEL,
            max_tokens=800,
            system=system,
            messages=history,
            timeout=API_TIMEOUT,
        )
        reply_text = next(
            (block.text for block in response.content if block.type == "text"),
            "申し訳ありません。もう一度お試しください。",
        )
    except Exception as e:
        logging.exception("Showa Claude API error: %s", e)
        reply_text = "少し調子が悪いようです。もう一度試してみてください😊"

    reply_text = re.sub(r'\*\*(.+?)\*\*', r'\1', reply_text)
    reply_text = re.sub(r'\*(.+?)\*',     r'\1', reply_text)
    reply_text = re.sub(r'^#{1,6}\s+',    '',    reply_text, flags=re.MULTILINE)

    _save_message(user_id, "assistant", reply_text, metadata)
    return reply_text


# ── 登録フロー ─────────────────────────────────────────

def start_registration(user_id: str) -> TextSendMessage:
    registration_states[user_id] = {"step": "awaiting_prefecture"}
    return TextSendMessage(
        text="ようこそ！\nまず住んでいる都道府県を選択してください。",
        quick_reply=_build_quick_reply([(p, p) for p in _PREFECTURES]),
    )


def handle_registration(user_id: str, message: str) -> FlexSendMessage | TextSendMessage:
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
        return FlexSendMessage(
            alt_text="紹介コードをお持ちですか？",
            contents={
                "type": "bubble",
                "header": {
                    "type": "box", "layout": "vertical", "paddingAll": "md",
                    "backgroundColor": _R_HEADER_BG,
                    "contents": [{
                        "type": "text", "text": "紹介コードをお持ちですか？",
                        "weight": "bold", "size": "md",
                        "color": _R_HEADER_TEXT, "align": "center",
                    }],
                },
                "footer": {
                    "type": "box", "layout": "horizontal",
                    "spacing": "sm", "paddingAll": "lg",
                    "backgroundColor": _R_BODY_BG,
                    "contents": [
                        {
                            "type": "button",
                            "action": {"type": "message", "label": "はい", "text": "はい"},
                            "style": "primary", "color": _R_BTN_COLOR,
                        },
                        {
                            "type": "button",
                            "action": {"type": "message", "label": "いいえ", "text": "いいえ"},
                            "style": "secondary",
                        },
                    ],
                },
            },
        )

    if step == "awaiting_referral_confirm":
        if message.strip() == "はい":
            state["step"] = "awaiting_referral_code"
            return TextSendMessage(text="紹介コードを入力してください。")
        else:
            _save_user(user_id, state)
            state["step"] = "awaiting_gender"
            return TextSendMessage(
                text="最後に性別を教えてください😊\n昭和の思い出話を合わせるためです",
                quick_reply=_build_quick_reply([
                    ("男性", "性別:男性"),
                    ("女性", "性別:女性"),
                    ("答えたくない", "性別:答えたくない"),
                ]),
            )

    if step == "awaiting_referral_code":
        code = message.strip().upper()
        _save_user(user_id, state)
        referral_msg = _handle_referral_input(user_id, code)
        state["referral_msg"] = referral_msg
        state["step"] = "awaiting_gender"
        return TextSendMessage(
            text="最後に性別を教えてください😊\n昭和の思い出話を合わせるためです",
            quick_reply=_build_quick_reply([
                ("男性", "性別:男性"),
                ("女性", "性別:女性"),
                ("答えたくない", "性別:答えたくない"),
            ]),
        )

    if step == "awaiting_gender":
        gender_map = {"性別:男性": "male", "性別:女性": "female", "性別:答えたくない": None}
        gender = gender_map.get(message.strip())
        if gender is not None or message.strip() == "性別:答えたくない":
            if gender:
                try:
                    get_supabase().table("users").update({"gender": gender}).eq(
                        "line_user_id", user_id
                    ).execute()
                    user_cache.pop(user_id, None)
                except Exception as e:
                    logging.error("gender update error: %s", e)
        referral_msg = state.get("referral_msg")
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
            "gender": state.get("gender") or None,
        },
        on_conflict="line_user_id",
    ).execute()
    user_cache.pop(user_id, None)  # 登録完了時にキャッシュを無効化
    _apply_rich_menu(user_id, is_paid=False)  # 無料メニューを適用


def _save_message(user_id: str, role: str, content: str,
                  metadata: dict | None = None) -> None:
    try:
        row: dict = {"line_user_id": user_id, "role": role, "content": content}
        if metadata:
            import json as _json
            row["metadata"] = _json.dumps(metadata, ensure_ascii=False)
        get_supabase().table("messages").insert(row).execute()
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
        .select("name, region, prefecture, city, is_paid, birthdate, gender")
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
    label = f"【{area}周辺の{genre or 'お店'}情報】" if area else f"【地元の{genre or 'お店'}情報】"
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
    """is_paid に応じてリッチメニューを適用する。
    メモリキャッシュで前回適用済みのメニューを追跡し、
    同じなら API コールをスキップする。
    """
    # タブ切り替えはLINEのエイリアス機構が自動処理するため、タブ1のIDのみリンクする
    target_menu_id = RICH_MENU_PAID_TAB1_ID if is_paid else RICH_MENU_FREE_TAB1_ID
    if not target_menu_id:
        return
    if _applied_menu_cache.get(user_id) == target_menu_id:
        return  # 既に正しいメニューが適用済み → スキップ
    try:
        line_bot_api.link_rich_menu_to_user(user_id, target_menu_id)
        _applied_menu_cache[user_id] = target_menu_id
        logging.info("rich menu switched: %s → %s (is_paid=%s)", user_id, target_menu_id, is_paid)
        # DB にも記録（current_menu_id カラムが存在すれば保存、なくてもエラーにしない）
        try:
            get_supabase().table("users").update(
                {"current_menu_id": target_menu_id}
            ).eq("line_user_id", user_id).execute()
        except Exception:
            pass  # カラム未作成でも動作に支障なし
    except Exception as e:
        logging.error("rich menu link error: %s", e)


def safe_push_message(user_id: str, messages, user_info: dict | None = None) -> None:
    """有料会員のみ push_message を送る。
    無料会員への push はコスト増を防ぐためブロックし WARNING を出力する。
    reply_token が失効した場合のフォールバックとして使用する。
    """
    is_paid = bool((user_info or {}).get("is_paid"))
    if not is_paid:
        logging.warning("safe_push_message: blocked for free user %s", user_id)
        return
    try:
        line_bot_api.push_message(user_id, messages)
    except Exception as e:
        logging.exception("safe_push_message error: %s", e)


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
    """紹介コードを受け取り、双方に bonus_count +5 を付与し、紹介者に通知する。"""
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

        # 更新後のボーナス回数を先に計算
        my_new_bonus      = (my_data.get("bonus_count")       or 0) + 5
        referrer_new_bonus = (referrer_data.get("bonus_count") or 0) + 5

        # 自分の referred_by を保存し、bonus_count +5
        get_supabase().table("users").update({
            "referred_by": code.upper(),
            "bonus_count": my_new_bonus,
        }).eq("line_user_id", user_id).execute()

        # 紹介者の bonus_count +5
        get_supabase().table("users").update({
            "bonus_count": referrer_new_bonus,
        }).eq("line_user_id", referrer_data["line_user_id"]).execute()

        user_cache.pop(user_id, None)

        # ── 紹介者へ LINE プッシュ通知 ───────────────────────────
        referrer_line_id = referrer_data.get("line_user_id")
        if referrer_line_id:
            try:
                notify_text = (
                    "🎉 おめでとうございます！\n"
                    "お友達があなたの紹介コードで登録しました！\n\n"
                    f"ボーナス5回をプレゼントしました😊\n"
                    f"残り回数：{referrer_new_bonus}回\n\n"
                    "引き続きご利用ください！"
                )
                line_bot_api.push_message(
                    referrer_line_id,
                    TextSendMessage(text=notify_text),
                )
            except Exception as push_err:
                logging.warning("referral push notify failed: %s", push_err)

        # ── 紹介された人（自分）へのウェルカムメッセージ ─────────
        return (
            f"🎁 紹介コードが確認できました！\n\n"
            f"ボーナス5回をプレゼントします😊\n"
            f"これで合計{my_new_bonus}回使えます！\n\n"
            "さっそく使ってみましょう！"
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


def _vector_search_faq(message: str, threshold: float = 0.80, limit: int = 3) -> list[dict]:
    """OpenAI text-embedding-3-small でベクトル検索して FAQ を返す。
    openai_client が未設定またはエラー時は空リストを返す。
    しきい値未満のFAQは返さない（Claudeにフォールバックさせる）。
    """
    if not openai_client:
        return []
    try:
        resp = openai_client.embeddings.create(
            model="text-embedding-3-small",
            input=message,
        )
        embedding = resp.data[0].embedding
        result = get_supabase().rpc("match_faq", {
            "query_embedding": embedding,
            "match_threshold": threshold,
            "match_count": limit,
        }).execute()
        rows = result.data or []
        # ヒット結果をログに出力（どのFAQが選ばれているか確認用）
        for row in rows:
            logging.error(
                "vector_faq_hit | query=%r | question=%r | similarity=%.4f",
                message, row.get("question"), row.get("similarity"),
            )
        if not rows:
            logging.error("vector_faq_miss | query=%r | threshold=%.2f", message, threshold)
        return rows
    except Exception as e:
        logging.error("vector search error: %s", e)
        return []


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


def _faq_direct_reply(message: str, user_info: dict | None = None) -> TextSendMessage | FlexSendMessage | None:
    """ハイブリッド検索（キーワード→ジャンル→ベクトル）でFAQを引き、直接返信メッセージを作る。
    text は TextSendMessage、button/carousel は FlexSendMessage を返す。
    未ヒットは None を返す（Claude 経由で処理）。
    """
    try:
        words = [w for w in re.split(r'[　\s。、？！ー・]+', message) if len(w) >= 2][:5]
        pref, city = _user_location(user_info)
        cols = "question, answer, answer_type, options"

        # 1. キーワード全文検索（固有名詞・地名に強い）
        rows = _faq_priority_search(words, None, cols, pref, city)

        # 2. ジャンル検索にフォールバック
        if not rows:
            genre = _detect_faq_genre(message)
            if genre:
                rows = _faq_genre_search(genre, None, cols, pref, city)

        # 3. ベクトル検索にフォールバック（意味検索・類義語に強い）
        if not rows:
            rows = _vector_search_faq(message)

        for row in rows:
            atype = row.get("answer_type", "text")

            if atype == "text":
                answer = (row.get("answer") or "").strip()
                if answer:
                    return TextSendMessage(text=answer)

            elif atype == "button":
                opts = row.get("options") or []
                btn_contents = [
                    {
                        "type": "button",
                        "action": {"type": "message", "label": o["label"][:20], "text": o["text"][:300]},
                        "style": "primary", "color": _R_BTN_COLOR, "height": "sm",
                        "margin": "sm",
                    }
                    for o in opts[:4]
                ]
                if not btn_contents:
                    continue
                return FlexSendMessage(
                    alt_text=row["answer"][:100],
                    contents={
                        "type": "bubble",
                        "header": {
                            "type": "box", "layout": "vertical", "paddingAll": "md",
                            "backgroundColor": _R_HEADER_BG,
                            "contents": [{
                                "type": "text", "text": row["answer"][:100],
                                "weight": "bold", "size": "sm",
                                "color": _R_HEADER_TEXT, "wrap": True, "align": "center",
                            }],
                        },
                        "body": {
                            "type": "box", "layout": "vertical",
                            "spacing": "sm", "paddingAll": "lg",
                            "backgroundColor": _R_BODY_BG,
                            "contents": btn_contents,
                        },
                    },
                )

            elif atype == "carousel":
                opts = row.get("options") or []
                bubbles = []
                for o in opts[:10]:
                    col_actions = [
                        {"type": "message", "label": a["label"][:20], "text": a["text"][:300]}
                        for a in (o.get("actions") or [])[:3]
                    ]
                    if not col_actions:
                        col_actions = [{"type": "message", "label": "詳しく聞く",
                                        "text": o.get("title", "")}]
                    footer_btns = [
                        {"type": "button", "action": a, "style": "primary",
                         "color": _R_BTN_COLOR, "height": "sm", "margin": "sm"}
                        for a in col_actions
                    ]
                    bubbles.append({
                        "type": "bubble", "size": "kilo",
                        "header": {
                            "type": "box", "layout": "vertical", "paddingAll": "md",
                            "backgroundColor": _R_HEADER_BG,
                            "contents": [{
                                "type": "text", "text": (o.get("title") or "")[:40],
                                "weight": "bold", "size": "md",
                                "color": _R_HEADER_TEXT, "align": "center", "wrap": True,
                            }],
                        },
                        "body": {
                            "type": "box", "layout": "vertical", "paddingAll": "lg",
                            "backgroundColor": _R_BODY_BG,
                            "contents": [{
                                "type": "text", "text": (o.get("text") or "詳細情報")[:60],
                                "size": "sm", "color": _R_SUB_TEXT, "wrap": True, "align": "center",
                            }],
                        },
                        "footer": {
                            "type": "box", "layout": "vertical", "paddingAll": "md",
                            "spacing": "sm", "backgroundColor": _R_BODY_BG,
                            "contents": footer_btns,
                        },
                    })
                if bubbles:
                    return FlexSendMessage(
                        alt_text=row["answer"][:100],
                        contents={"type": "carousel", "contents": bubbles},
                    )

        return None
    except Exception as e:
        logging.error("faq direct reply error: %s", e)
        return None


def _save_missed_faq(question: str, claude_answer: str) -> None:
    """FAQにヒットせずClaudeが回答した質問をmissed_faqsに記録する。
    同一質問が再送されたらuser_countを+1して集計する。
    """
    try:
        sb = get_supabase()
        result = sb.table("missed_faqs").select("id, user_count").eq("question", question).limit(1).execute()
        if result.data:
            row_id = result.data[0]["id"]
            new_count = result.data[0]["user_count"] + 1
            sb.table("missed_faqs").update({
                "user_count": new_count,
                "claude_answer": claude_answer,
            }).eq("id", row_id).execute()
        else:
            sb.table("missed_faqs").insert({
                "question": question,
                "claude_answer": claude_answer,
            }).execute()
    except Exception as e:
        logging.error("save_missed_faq error: %s", e)


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


def _is_conversation_active(user_id: str) -> bool:
    """直前のDBメッセージがAIからの返信かつ30分以内なら会話継続中とみなす。

    会話継続中と判定された場合は FAQ 検索をスキップし、
    過去の会話文脈を維持したまま Claude に直接投げる。
    """
    try:
        result = (
            get_supabase().table("messages")
            .select("role, created_at")
            .eq("line_user_id", user_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not result.data:
            return False
        last = result.data[0]
        if last["role"] != "assistant":
            return False
        last_time = datetime.fromisoformat(last["created_at"].replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - last_time) < timedelta(minutes=30)
    except Exception as e:
        logging.error("conversation active check error: %s", e)
        return False


def _has_message_history(user_id: str) -> bool:
    """messagesテーブルに対象ユーザーの履歴があるか確認する。"""
    try:
        result = (
            get_supabase().table("messages")
            .select("id")
            .eq("line_user_id", user_id)
            .limit(1)
            .execute()
        )
        return bool(result.data)
    except Exception:
        return False


# ── Claude 返答 ────────────────────────────────────────

def get_claude_reply(
    user_id: str,
    user_message: str,
    user_info: dict | None = None,
    skip_faq: bool = False,
    save_missed: bool = False,
) -> str:
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

    # 会話継続中はDBをスキップして文脈を優先する
    # 新規トピック時はFAQをhandle_message側で確認済みなので飲食店情報のみ注入
    if not skip_faq:
        user_region = (user_info or {}).get("region", "")
        if _is_food_query(user_message) and user_region:
            restaurant_context = _search_restaurants(user_message)
            if restaurant_context:
                system += f"\n\n{restaurant_context}\n上記の情報を参考にして答えてください。"

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

    # FAQミス（新規トピックでヒットなし）をバックグラウンドで記録
    if save_missed:
        threading.Thread(
            target=_save_missed_faq, args=(user_message, reply_text), daemon=True
        ).start()

    return reply_text


# ── LINE イベントハンドラ ──────────────────────────────

def _is_duplicate_event(event_id: str) -> bool:
    """processed_events テーブルで重複チェック。
    新規なら insert して False を返す。既存なら True を返す。
    24時間以上古い行は削除してテーブルを小さく保つ。
    """
    try:
        sb = get_supabase()
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        sb.table("processed_events").delete().lt("created_at", cutoff).execute()
        sb.table("processed_events").insert({"event_id": event_id}).execute()
        return False  # 新規
    except Exception as e:
        if any(k in str(e).lower() for k in ("duplicate", "unique", "23505")):
            return True  # 重複
        logging.error("duplicate event check error: %s", e)
        return False  # 不明エラーは処理を続ける


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    # 生JSONから message_id → markAsReadToken と webhookEventId のマップを作る
    # SDK は両フィールドをパースしないため、ここで直接抽出する
    g.mark_as_read_tokens = {}
    g.webhook_event_ids = {}
    try:
        for ev in json.loads(body).get("events", []):
            msg_id = ev.get("message", {}).get("id")
            token  = ev.get("message", {}).get("markAsReadToken")
            wh_id  = ev.get("webhookEventId")
            if msg_id and token:
                g.mark_as_read_tokens[msg_id] = token
            if msg_id and wh_id:
                g.webhook_event_ids[msg_id] = wh_id
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

    # ── webhook 重複チェック ──────────────────────────────────────
    webhook_event_id = getattr(g, "webhook_event_ids", {}).get(event.message.id)
    if webhook_event_id and _is_duplicate_event(webhook_event_id):
        return  # 同一イベントの再配信はスキップ

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

        # リッチメニューをバックグラウンドで同期（変更がある時のみ API コール）
        threading.Thread(
            target=_apply_rich_menu,
            args=(user_id, bool(user_info.get("is_paid"))),
            daemon=True,
        ).start()
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
        _showa_sessions.pop(user_id, None)   # 昭和セッションをクリア
        _showa_gender_pending.discard(user_id)
        _hobby_states.pop(user_id, None)     # 趣味セッションをクリア
        _music_states.pop(user_id, None)     # 動画・音楽セッションをクリア
        _travel_states.pop(user_id, None)    # 旅行セッションをクリア
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
        line_bot_api.reply_message(event.reply_token, _flex_consult_menu())
        return

    # ② 探す
    if msg == "探す":
        line_bot_api.reply_message(event.reply_token, _flex_search_menu())
        return

    # ③ 知る
    if msg == "知る":
        line_bot_api.reply_message(event.reply_token, _flex_know_menu())
        return

    # ④ つながる
    if msg == "つながる":
        line_bot_api.reply_message(event.reply_token, _flex_connect_menu())
        return

    # ⑤ 友達に紹介
    if msg == "友達に紹介":
        referral_code = _get_referral_code(user_id)
        line_bot_api.reply_message(event.reply_token, _flex_referral_menu(referral_code))
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
                    "地元の生活をAIがサポートします！\n\n"
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

    # ── 動画・音楽（Claude不要・YouTube URL案内）──────────────────────

    if msg == "動画・音楽":
        _name = (user_info or {}).get("name") or ""
        name_part = f"{_name}さん、" if _name else ""
        line_bot_api.reply_message(
            event.reply_token,
            [
                TextSendMessage(text=f"{name_part}音楽や動画を楽しみましょう😊"),
                _flex_music_menu(),
            ],
        )
        return

    if msg == "音楽を聴く":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="どんな音楽を聴きますか？😊",
                quick_reply=_build_quick_reply([
                    ("昭和の歌謡曲",     "音楽:昭和の歌謡曲"),
                    ("演歌・民謡",       "音楽:演歌・民謡"),
                    ("クラシック・童謡", "音楽:クラシック・童謡"),
                    ("好きな歌手を検索", "好きな歌手を検索"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    if msg.startswith("音楽:"):
        genre = msg[len("音楽:"):]
        if genre == "昭和の歌謡曲":
            carousel = _youtube_carousel([
                ("美空ひばり", "昭和を代表する歌姫\n名曲がたくさんあります🎵", "美空ひばり 名曲"),
                ("都はるみ",   "演歌の女王\n心にしみる歌声です🎵",             "都はるみ 名曲"),
                ("北島三郎",   "男性演歌の第一人者\n力強い歌声です🎵",         "北島三郎 名曲"),
            ], "YouTubeで聴く")
        elif genre == "演歌・民謡":
            carousel = _youtube_carousel([
                ("演歌のベスト", "懐かしい演歌を\nまとめて楽しめます🎵", "演歌 ベスト ヒット"),
                ("民謡集",       "日本全国の\n民謡を楽しみましょう🎵", "日本民謡 ベスト"),
                ("懐メロ特集",   "あの頃の名曲が\n次々に流れます🎵",   "懐メロ 昭和 ヒット"),
            ], "YouTubeで聴く")
        else:  # クラシック・童謡
            carousel = _youtube_carousel([
                ("日本の童謡",     "懐かしい童謡を\nゆったり聴きましょう🎵", "日本の童謡 ベスト"),
                ("唱歌集",         "学校で歌った\nあの懐かしい唱歌🎵",       "日本唱歌 ベスト"),
                ("クラシック名曲", "心が落ち着く\nクラシックの名曲🎵",       "クラシック 名曲 BGM"),
            ], "YouTubeで聴く")
        line_bot_api.reply_message(event.reply_token, carousel)
        return

    if msg == "好きな歌手を検索":
        _music_states[user_id] = {"step": "awaiting_artist"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="好きな歌手や曲名を\n教えてください😊",
                quick_reply=_build_quick_reply([_QR_BACK]),
            ),
        )
        return

    # 歌手・曲名の入力待ち
    _mus = _music_states.get(user_id)
    if _mus and _mus.get("step") == "awaiting_artist":
        _music_states.pop(user_id, None)
        import urllib.parse as _urlparse
        yt_url = f"https://www.youtube.com/results?search_query={_urlparse.quote(msg)}"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"「{msg}」を\nYouTubeで検索してみましょう！😊",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=URIAction(label="▶️ YouTubeで開く", uri=yt_url)),
                    QuickReplyButton(action=MessageAction(label="別の曲を探す", text="好きな歌手を検索")),
                    QuickReplyButton(action=MessageAction(label="🏠 最初に戻る", text="最初に戻る")),
                ]),
            ),
        )
        return

    if msg == "動画を楽しむ":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="どんな動画を見ますか？😊",
                quick_reply=_build_quick_reply([
                    ("面白い動画",                 "動画:面白い動画"),
                    ("旅行・風景動画",             "動画:旅行・風景動画"),
                    ("料理・趣味の動画",           "動画:料理・趣味の動画"),
                    ("ニュース・ドキュメンタリー", "動画:ニュース・ドキュメンタリー"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    if msg.startswith("動画:"):
        genre = msg[len("動画:"):]
        if genre == "面白い動画":
            carousel = _youtube_carousel([
                ("動物・癒し動画",       "かわいい動物が\n心を癒してくれます😊", "動物 癒し ほっこり動画"),
                ("笑える動画",           "元気が出る\n笑える動画特集😄",         "面白い動画 笑える"),
                ("懐かしいテレビ番組", "あの頃の人気番組を\n懐かしもう📺",       "昭和 懐かしい テレビ番組"),
            ], "YouTubeで見る")
        elif genre == "旅行・風景動画":
            carousel = _youtube_carousel([
                ("日本の絶景",   "美しい日本の\n絶景を旅しよう🗾",     "日本の絶景 4K 風景"),
                ("温泉地の風景", "名湯の風景と\n旅気分を楽しもう♨️", "温泉地 風景 旅行"),
                ("昭和の街並み", "懐かしい昭和の\n街並みを歩こう🏘️", "昭和 街並み 懐かしい"),
            ], "YouTubeで見る")
        elif genre == "料理・趣味の動画":
            carousel = _youtube_carousel([
                ("簡単レシピ動画", "わかりやすい\n料理の作り方🍳",         "簡単 料理 レシピ 動画"),
                ("趣味の動画",     "様々な趣味の\nやり方動画🌸",           "趣味 手芸 作り方 動画"),
                ("手芸・工作",     "素敵な手作り作品の\n作り方を学ぼう✂️", "手芸 工作 作り方 動画"),
            ], "YouTubeで見る")
        else:  # ニュース・ドキュメンタリー
            carousel = _youtube_carousel([
                ("NHKニュース",               "最新のニュースを\nわかりやすく📰",     "NHK ニュース"),
                ("昭和のドキュメンタリー",     "あの時代を\n振り返ろう📻",           "昭和 ドキュメンタリー 懐かしい"),
                ("自然・動物ドキュメンタリー", "大自然の神秘を\n楽しもう🦁",         "自然 動物 ドキュメンタリー"),
            ], "YouTubeで見る")
        line_bot_api.reply_message(event.reply_token, carousel)
        return

    if msg == "体操・健康動画":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "体の状態を教えてください😊\n"
                    "ぴったりの体操動画を\n"
                    "ご紹介しますよ！"
                ),
                quick_reply=_build_quick_reply([
                    ("椅子に座ってできる体操", "体操動画:椅子に座ってできる体操"),
                    ("ストレッチ",             "体操動画:ストレッチ"),
                    ("認知症予防体操",         "体操動画:認知症予防体操"),
                    ("転倒予防体操",           "体操動画:転倒予防体操"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    if msg.startswith("体操動画:"):
        genre = msg[len("体操動画:"):]
        if genre == "椅子に座ってできる体操":
            carousel = _youtube_carousel([
                ("椅子体操入門",         "座ったまま\n簡単にできる体操です",   "椅子体操 高齢者 入門"),
                ("座ってできる足腰体操", "足腰を鍛える\n椅子体操です",         "椅子体操 足腰 高齢者"),
                ("座ってできる上半身体操", "肩・腕・首を\n動かす体操です",     "椅子体操 上半身 高齢者"),
            ], "YouTubeで見る")
        elif genre == "ストレッチ":
            carousel = _youtube_carousel([
                ("朝のストレッチ",       "一日の始まりに\n気持ちよく伸ばそう", "朝のストレッチ 高齢者"),
                ("腰痛予防ストレッチ",   "腰の痛みを\n和らげるストレッチ",     "腰痛予防 ストレッチ 高齢者"),
                ("肩こり解消ストレッチ", "肩のこりを\nほぐすストレッチ",       "肩こり解消 ストレッチ 高齢者"),
            ], "YouTubeで見る")
        elif genre == "認知症予防体操":
            carousel = _youtube_carousel([
                ("コグニサイズ",         "体を動かしながら\n頭も使う体操です", "コグニサイズ 認知症予防"),
                ("指体操・脳トレ体操",   "指を動かして\n脳を活性化します",     "指体操 脳トレ 高齢者"),
                ("歌いながら体操",       "楽しく歌いながら\n体を動かします",   "歌いながら体操 高齢者"),
            ], "YouTubeで見る")
        else:  # 転倒予防体操
            carousel = _youtube_carousel([
                ("バランス体操", "バランス感覚を\n鍛えて転倒予防",   "バランス体操 転倒予防 高齢者"),
                ("足腰強化体操", "足腰を強くして\n安全に歩こう",     "足腰強化 体操 高齢者"),
                ("歩行改善体操", "歩き方を改善する\n体操を学ぼう",   "歩行改善 体操 高齢者"),
            ], "YouTubeで見る")
        line_bot_api.reply_message(event.reply_token, carousel)
        return

    if msg == "YouTubeの使い方":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="何について知りたいですか？😊",
                quick_reply=_build_quick_reply([
                    ("YouTubeの開き方", "YouTube使い方:YouTubeの開き方"),
                    ("動画の検索方法",  "YouTube使い方:動画の検索方法"),
                    ("音量の調整方法",  "YouTube使い方:音量の調整方法"),
                    ("字幕の出し方",    "YouTube使い方:字幕の出し方"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    if msg.startswith("YouTube使い方:"):
        topic = msg[len("YouTube使い方:"):]
        _yt_qr_done = _build_quick_reply([
            ("他の使い方を教えて", "YouTubeの使い方"),
            ("動画を楽しむ",       "動画を楽しむ"),
            _QR_BACK,
        ])
        _yt_qr_retry = lambda t: _build_quick_reply([
            ("もう一度説明して", f"YouTube使い方:{t}"),
            ("他の使い方を教えて", "YouTubeの使い方"),
            _QR_BACK,
        ])
        if topic == "YouTubeの開き方":
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "YouTubeはスマホに\n"
                        "入っているアプリですよ😊\n\n"
                        "赤い□に白い▷が\n"
                        "描いてあるアイコンです\n"
                        "それをタップしてください！"
                    ),
                    quick_reply=_build_quick_reply([
                        ("開けました！",         "動画を楽しむ"),
                        ("アプリが見つからない", "YouTubeアプリが見つからない"),
                        _QR_BACK,
                    ]),
                ),
            )
        elif topic == "動画の検索方法":
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "YouTubeで動画を\n"
                        "検索する方法ですよ😊\n\n"
                        "1. YouTubeを開く\n"
                        "2. 上の検索バーをタップ\n"
                        "3. 見たい動画の名前を入力\n"
                        "4. 虫眼鏡ボタンをタップ"
                    ),
                    quick_reply=_yt_qr_retry("動画の検索方法"),
                ),
            )
        elif topic == "音量の調整方法":
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "動画の音量の\n"
                        "調整方法ですよ😊\n\n"
                        "1. スマホの側面にある\n"
                        "   音量ボタン（＋）を押す\n"
                        "2. または動画を再生中に\n"
                        "   画面をタップして\n"
                        "   スライダーを動かす"
                    ),
                    quick_reply=_yt_qr_retry("音量の調整方法"),
                ),
            )
        else:  # 字幕の出し方
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "YouTubeの字幕の\n"
                        "出し方ですよ😊\n\n"
                        "1. 動画を再生中に\n"
                        "   画面をタップ\n"
                        "2. 右上の「CC」または\n"
                        "   字幕アイコンをタップ\n"
                        "3. 字幕が表示されます"
                    ),
                    quick_reply=_yt_qr_retry("字幕の出し方"),
                ),
            )
        return

    if msg == "YouTubeアプリが見つからない":
        import urllib.parse as _urlparse2
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "アプリが見つからない場合は\n"
                    "こちらから開けますよ😊"
                ),
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=URIAction(
                        label="▶️ YouTubeを開く",
                        uri="https://www.youtube.com",
                    )),
                    QuickReplyButton(action=MessageAction(label="🏠 最初に戻る", text="最初に戻る")),
                ]),
            ),
        )
        return

    # 動画・YouTube キーワード（利用カウント不要・URLを案内）
    if re.search(r'動画|youtube|ユーチューブ', msg, re.IGNORECASE):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "動画を楽しみたいですね😊\n"
                    "YouTubeで楽しい動画がたくさん見られますよ！\n\n"
                    "こちらからどうぞ👇"
                ),
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=URIAction(label="▶️ YouTubeを開く", uri="https://www.youtube.com")),
                ]),
            ),
        )
        return

    # AIに聞くバー（全会員共通）
    if msg == "AIに聞く":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="ご質問をどうぞ。何でもお気軽に聞かせてください！😊"
            ),
        )
        return

    # AIに相談（無料会員向け）
    if msg == "AIに相談":
        import random as _random
        _name = (user_info or {}).get("name") or ""
        _name_call = f"{_name}さん" if _name else "あなた"
        if not _has_message_history(user_id):
            # 初回利用
            _welcome = (
                f"{_name_call}、はじめまして！😊\n\n"
                "何でも気軽に話しかけてください。\n"
                "間違えても大丈夫ですよ！\n\n"
                "例えばこんなことを聞けます👇"
            )
            line_bot_api.reply_message(
                event.reply_token,
                [TextSendMessage(text=_welcome), _flex_ai_consult_first()],
            )
        else:
            # 2回目以降：ランダムウェルカム
            _msgs = [
                f"{_name_call}、今日もお待ちしてました😊 何でもどうぞ！",
                f"{_name_call}、いらっしゃい！😊 今日はどんなことを話しましょうか？",
                f"{_name_call}、こんにちは！😊 何でも気軽に聞いてくださいね",
            ]
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=_random.choice(_msgs),
                    quick_reply=_build_quick_reply([
                        ("健康のこと",   "健康について相談したいです"),
                        ("地元のこと",   "地元の情報を教えてください"),
                        ("なんでも話す", "話しかける"),
                        _QR_BACK,
                    ]),
                ),
            )
        return

    # 健康相談（入口）
    if msg == "健康相談":
        line_bot_api.reply_message(event.reply_token, _flex_health_menu())
        return

    # 病院・お薬（AIに相談フレックスからの入口）
    if msg == "病院お薬相談":
        line_bot_api.reply_message(event.reply_token, _flex_health_hospital_menu())
        return

    # お金・手続き相談
    if msg == "お金手続き相談":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "お金や手続きについてですね😊\n"
                    "どんなことでお困りですか？\n\n"
                    "よくある相談を選んでもいいですし\n"
                    "直接話しかけてもらってもOKですよ！"
                ),
                quick_reply=_build_quick_reply([
                    ("年金について",       "年金について教えてください"),
                    ("医療費・保険",       "医療費や保険について教えてください"),
                    ("給付金・補助金",     "給付金や補助金について教えてください"),
                    ("相続・遺言",         "相続や遺言について教えてください"),
                    ("確定申告・税金",     "確定申告や税金について教えてください"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 家族・人間関係相談
    if msg == "家族関係相談":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "家族や人間関係のことですね😊\n"
                    "どんなことでお困りですか？\n\n"
                    "どんな小さなことでも\n遠慮なく話してください"
                ),
                quick_reply=_build_quick_reply([
                    ("子どもとの関係",     "子どもとの関係について相談したいです"),
                    ("夫婦のこと",         "夫婦のことについて相談したいです"),
                    ("介護のこと",         "介護について相談したいです"),
                    ("近所付き合い",       "近所付き合いについて相談したいです"),
                    ("孤独・ひとりぼっち", "一人で寂しいです"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 気持ち・不安相談（共感優先）
    if msg == "気持ち不安相談":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "気持ちや不安なことを\n話してくださいね😊\n\n"
                    "ここでは何でも話せますよ。\n"
                    "ゆっくり聞かせてください"
                ),
                quick_reply=_build_quick_reply([
                    ("なんとなく不安",     "なんとなく不安な気持ちです"),
                    ("気力がわかない",     "最近気力がわかなくて困っています"),
                    ("眠れない",           "なかなか眠れなくて困っています"),
                    ("話を聞いてほしい",   "ただ話を聞いてほしいです"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # なんでも相談（フリー入力誘導）
    if msg == "なんでも相談":
        _name = (user_info or {}).get("name") or ""
        name_call = f"{_name}さん" if _name else "あなた"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    f"{name_call}、何でも話しかけてくださいね😊\n\n"
                    "困りごと・疑問・雑談\n"
                    "どんなことでもOKですよ！"
                ),
                quick_reply=_build_quick_reply([
                    ("今日の天気",         "今日の天気を教えてください"),
                    ("おすすめレシピ",     "今日のおすすめレシピを教えてください"),
                    ("地元の情報",         "地元の情報を教えてください"),
                    ("雑談したい",         "少し雑談しませんか"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # ── 健康相談サブメニュー（利用カウント不要）────────────────────────

    # 体の症状・不調
    if msg in ("体の症状を相談する", "症状を話す"):
        _health_states[user_id] = "awaiting_symptom"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "どんな症状ですか？\n"
                    "気になることを教えてください😊\n\n"
                    "どんな小さなことでも\n大丈夫ですよ"
                ),
                quick_reply=_build_quick_reply([
                    ("胸・動悸・息苦しい",  "胸が痛い・動悸がする"),
                    ("発熱・腹痛",          "高熱や激しい腹痛がある"),
                    ("頭痛・鼻水・軽い咳",  "少し頭痛・鼻水・軽い咳がある"),
                    ("気分の落ち込み",      "気分が落ち込む・やる気が出ない"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 病院・薬サブメニュー
    if msg == "病院・薬のこと":
        line_bot_api.reply_message(event.reply_token, _flex_health_hospital_menu())
        return

    # 健康習慣サブメニュー
    if msg in ("健康習慣・予防", "健康習慣"):
        line_bot_api.reply_message(event.reply_token, _flex_health_habits_menu())
        return

    # 近くの病院を探す → 地図LIFF誘導
    if msg == "近くの病院を探す":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="地図で近くの病院を\n探してみましょう😊",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=URIAction(
                        label="🗺️ 地図で探す",
                        uri=f"https://liff.line.me/{LIFF_ID}/map",
                    )),
                    QuickReplyButton(action=MessageAction(label="🏠 最初に戻る", text="最初に戻る")),
                ]),
            ),
        )
        return

    # 何科に行けばいい？
    if msg == "何科に行けばいい？":
        _health_states[user_id] = "awaiting_dept"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="どんな症状ですか？\n一緒に考えましょう😊",
                quick_reply=_build_quick_reply([
                    ("頭痛・めまい",  "頭痛やめまいがします"),
                    ("腰痛・膝痛",   "腰や膝が痛いです"),
                    ("胃痛・腹痛",   "胃や腹が痛いです"),
                    ("皮膚のかゆみ", "皮膚がかゆいです"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 薬の飲み方
    if msg == "薬の飲み方を教えて":
        _health_states[user_id] = "awaiting_medicine"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="薬の名前を教えてもらえますか？\n飲み方をお伝えしますよ😊",
                quick_reply=_build_quick_reply([
                    ("飲み忘れた時は？", "薬を飲み忘れた時はどうすればいい？"),
                    ("副作用が心配",      "副作用が心配"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 薬の飲み忘れ
    if msg in ("飲み忘れた時は？", "薬を飲み忘れた時はどうすればいい？"):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "気づいた時にすぐ飲みましょう😊\n\n"
                    "でも次の服用時間が近い場合は\n"
                    "1回飛ばしても大丈夫ですよ\n\n"
                    "心配な場合は薬局に\n電話してください📞"
                ),
                quick_reply=_build_quick_reply([
                    ("副作用が心配",   "副作用が心配"),
                    ("他のことを聞く", "他のことを聞かせてください"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 薬の副作用
    if msg == "副作用が心配":
        _health_states[user_id] = "awaiting_side_effect"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="どんな症状が出ていますか？\n一緒に確認しましょう😊",
                quick_reply=_build_quick_reply([
                    ("呼吸が苦しい",    "呼吸困難・全身に発疹が出た"),
                    ("少しだるい・眠い", "少しだるい・眠気がある"),
                    ("他の症状",        "他の副作用症状がある"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 食事・栄養サブメニュー
    if msg == "食事・栄養の相談":
        line_bot_api.reply_message(event.reply_token, _flex_health_food_menu())
        return

    # 運動・体操サブメニュー
    if msg == "運動・体操の相談":
        line_bot_api.reply_message(event.reply_token, _flex_health_exercise_menu())
        return

    # 散歩コースを探す → 地図LIFF
    if msg == "散歩コースを探す":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="お近くの公園や散歩道を\n地図で探してみましょう😊",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=URIAction(
                        label="🗺️ 地図で探す",
                        uri=f"https://liff.line.me/{LIFF_ID}/map",
                    )),
                    QuickReplyButton(action=MessageAction(label="🏠 最初に戻る", text="最初に戻る")),
                ]),
            ),
        )
        return

    # 体操動画 → 体操・健康動画メニューへ
    if msg == "体操動画を見る":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "体の状態を教えてください😊\n"
                    "ぴったりの体操動画を\n"
                    "ご紹介しますよ！"
                ),
                quick_reply=_build_quick_reply([
                    ("椅子に座ってできる体操", "体操動画:椅子に座ってできる体操"),
                    ("ストレッチ",             "体操動画:ストレッチ"),
                    ("認知症予防体操",         "体操動画:認知症予防体操"),
                    ("転倒予防体操",           "体操動画:転倒予防体操"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 睡眠相談
    if msg == "睡眠を相談する":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "眠れないのはつらいですよね😢\n"
                    "一緒に原因を考えましょう😊"
                ),
                quick_reply=_build_quick_reply([
                    ("眠れない理由を話す",    "眠れない理由を話す"),
                    ("睡眠改善のコツを教えて", "睡眠改善のコツを教えて"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 認知症チェックリスト
    if msg in ("認知症チェックリストを見る", "チェックリストを見る"):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "簡単なチェックをしてみましょう😊\n\n"
                    "最近こんなことはありますか？\n\n"
                    "①同じことを何度も聞く\n"
                    "②財布やカギをよく置き忘れる\n"
                    "③日付や曜日がわからなくなる\n"
                    "④料理の手順がわからなくなる\n"
                    "⑤人の名前がとっさに出ない\n\n"
                    "3つ以上あれば一度\nかかりつけ医に相談すると安心ですよ😊"
                ),
                quick_reply=_build_quick_reply([
                    ("かかりつけ医に相談する", "かかりつけ医への相談方法を教えてください"),
                    ("相談窓口を教えて",       "相談窓口を教えて"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 相談窓口
    if msg == "相談窓口を教えて":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "いつでも話を聞いてくれる\n窓口がありますよ😊\n\n"
                    "📞 よりそいホットライン\n"
                    "0120-279-338（24時間）\n\n"
                    "📞 こころの健康相談\n"
                    "0570-064-556\n\n"
                    "気軽に電話してみてくださいね"
                ),
                quick_reply=_build_quick_reply([
                    ("話を聞いてほしい",   "もう少し話を聞いてほしいです"),
                    ("他のことを聞く",     "他のことを聞かせてください"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 食事レシピ
    if msg == "食事レシピ":
        line_bot_api.reply_message(event.reply_token, _flex_recipe_menu())
        return

    # ── 食事レシピ サブメニュー（利用カウント不要）───────────────────────

    if msg == "今日の献立":
        _recipe_states[user_id] = {"step": "ingredients", "type": "dinner"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "冷蔵庫にある食材を\n教えてください😊\n\n"
                    "例：鶏肉、キャベツ、じゃがいも\n\n"
                    "ある食材を送っていただければ\n今夜の献立を提案しますよ！"
                ),
                quick_reply=_build_quick_reply([
                    ("肉系がある",         "鶏肉、玉ねぎ、にんじん"),
                    ("野菜が多い",         "キャベツ、じゃがいも、卵"),
                    ("魚系がある",         "鮭、大根、豆腐"),
                    ("あり合わせで何とか", "残り野菜、卵、ご飯"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    if msg == "体に合った食事":
        _recipe_states[user_id] = {"step": "condition", "type": "health"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "今日の体の調子を\n教えてください😊\n\n"
                    "体の状態に合った\nおすすめ食事を提案します！"
                ),
                quick_reply=_build_quick_reply([
                    ("血圧が気になる",     "血圧が気になる"),
                    ("血糖値が気になる",   "血糖値・糖尿病気味"),
                    ("胃腸が弱い",         "胃腸が弱い・胃もたれ"),
                    ("疲れが取れない",     "疲れが取れない・だるい"),
                    ("元気をつけたい",     "もっと元気になりたい"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    if msg == "簡単料理":
        _recipe_states[user_id] = {"step": "condition", "type": "quick"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "今日はどんな感じですか？😊\n\n"
                    "状況に合わせた\nラクちんレシピを提案します！"
                ),
                quick_reply=_build_quick_reply([
                    ("5分で済ませたい",        "5分で作れるもの"),
                    ("電子レンジだけで",       "電子レンジだけで作れるもの"),
                    ("コンビニ食材で",         "コンビニの食材だけで"),
                    ("一品だけでいい",         "おかず一品だけでいい"),
                    ("残り物で何とかしたい",   "残り物・ありもので"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    if msg == "楽しむレシピ":
        _recipe_states[user_id] = {"step": "condition", "type": "enjoy"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "どんな場面ですか？😊\n\n"
                    "気分や場面に合わせた\n特別レシピを提案します！"
                ),
                quick_reply=_build_quick_reply([
                    ("記念日や特別な日",   "記念日・特別な日に"),
                    ("家族と一緒に",       "家族みんなで食べたい"),
                    ("友達を招待",         "友達をおもてなし"),
                    ("お酒に合うもの",     "お酒に合うおつまみ"),
                    ("季節の料理を作りたい", "旬の食材を使った料理"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 次のステップ（ステップバイステップ中の進行）
    if msg == "次のステップ":
        _rs = _recipe_states.get(user_id)
        if _rs and _rs.get("step") == "step_by_step":
            steps = _rs.get("steps", [])
            current = _rs.get("current", 0) + 1
            recipe_name = _rs.get("recipe_name", "レシピ")
            if current >= len(steps):
                _recipe_states.pop(user_id, None)
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text=(
                            f"「{recipe_name}」\n全ステップ完了です！\n\n"
                            "おいしく作れましたか？😊\n"
                            "ぜひ召し上がれ！"
                        ),
                        quick_reply=_build_quick_reply([
                            ("もう一度作る",     f"レシピ詳細:{recipe_name}"),
                            ("別のレシピを選ぶ", "別のレシピを提案してください"),
                            _QR_BACK,
                        ]),
                    ),
                )
            else:
                _recipe_states[user_id] = {**_rs, "current": current}
                total = len(steps)
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text=f"【ステップ {current + 1}/{total}】\n\n{steps[current]}",
                        quick_reply=_build_quick_reply([
                            ("次のステップ", "次のステップ"),
                            ("最初から",     f"レシピ詳細:{recipe_name}"),
                            ("途中でやめる", "食事レシピ"),
                        ]),
                    ),
                )
            return

    # ── 昭和モード ──────────────────────────────────────────────────────────

    # 性別入力待ちユーザーからの性別回答
    if user_id in _showa_gender_pending:
        gender_map = {"性別:男性": "male", "性別:女性": "female", "性別:答えたくない": None}
        if msg in gender_map:
            _showa_gender_pending.discard(user_id)
            gender = gender_map[msg]
            if gender:
                try:
                    get_supabase().table("users").update({"gender": gender}).eq(
                        "line_user_id", user_id
                    ).execute()
                    user_cache.pop(user_id, None)
                    user_info = {**(user_info or {}), "gender": gender}
                except Exception as e:
                    logging.error("showa gender update error: %s", e)
            # 性別登録後に昭和メニューを表示
            _name    = (user_info or {}).get("name") or ""
            _birth   = (user_info or {}).get("birthdate", "")
            _era     = _get_era_from_birthdate(_birth)
            line_bot_api.reply_message(
                event.reply_token,
                [
                    TextSendMessage(
                        text=f"ありがとうございます😊\n{_name}さん、昭和の懐かしい話をしましょう！\n今日は昭和{_era}年代のお話ですよ！"
                             if _name else f"ありがとうございます😊\n昭和の懐かしい話をしましょう！\n今日は昭和{_era}年代のお話ですよ！",
                    ),
                    _flex_showa_menu(_name, _era),
                ],
            )
            return

    # なつかしい昭和 エントリーポイント
    if msg == "なつかしい昭和":
        _name   = (user_info or {}).get("name") or ""
        _birth  = (user_info or {}).get("birthdate", "")
        _gender = (user_info or {}).get("gender")
        _era    = _get_era_from_birthdate(_birth)
        # 性別未登録なら先に聞く
        if _gender is None:
            _showa_gender_pending.add(user_id)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text="昭和の話題をより楽しむために\n性別を教えていただけますか？😊",
                    quick_reply=_build_quick_reply([
                        ("男性", "性別:男性"),
                        ("女性", "性別:女性"),
                        ("答えたくない", "性別:答えたくない"),
                    ]),
                ),
            )
            return
        line_bot_api.reply_message(
            event.reply_token,
            [
                TextSendMessage(
                    text=f"{_name}さん、昭和の懐かしい話をしましょう😊\n今日は昭和{_era}年代のお話ですよ！"
                         if _name else f"昭和の懐かしい話をしましょう😊\n今日は昭和{_era}年代のお話ですよ！",
                ),
                _flex_showa_menu(_name, _era),
            ],
        )
        return

    # 昭和トーク開始
    if msg == "昭和トーク開始":
        _birth   = (user_info or {}).get("birthdate", "")
        _gender  = (user_info or {}).get("gender")
        _name    = (user_info or {}).get("name") or ""
        _era     = _get_era_from_birthdate(_birth)
        _season  = _get_current_season()
        rag = _get_showa_rag(_gender, _era, _season)
        _showa_sessions[user_id] = {
            "era": _era, "gender": _gender,
            "topic": rag.get("topic", "") if rag else "",
        }
        _clear_history(user_id)
        prompt = (
            f"以下のRAGデータを参考に{_name}さんに話しかけてください。\n\n"
            if _name else "以下のRAGデータを参考にユーザーに話しかけてください。\n\n"
        )
        if rag:
            prompt += (
                f"ユーザー情報：\n"
                f"・名前：{_name}\n・性別：{_gender or '不明'}\n・年代：{_era}年代生まれ\n\n"
                f"RAGデータ：\n"
                f"・話題：{rag['topic']}\n"
                f"・質問文：{rag['question']}\n"
                f"・背景知識：{rag['background']}\n"
                f"・深掘り質問：{rag['followup']}\n\n"
                "話しかける際のルール：\n"
                "・必ず名前で呼びかける\n"
                "・questionをベースに自然な会話文を生成する\n"
                "・backgroundの豆知識を自然に添える\n"
                "・最後に1つだけ質問する"
            )
        else:
            prompt += f"ユーザーは{_era}年代生まれです。その年代に合った昭和の懐かしい話題で話しかけてください。"

        def _showa_start_process(uid, prpt, uinfo, r_token):
            try:
                reply_text = get_showa_reply(uid, prpt, uinfo, rag)
                qr = _build_quick_reply([
                    ("思い出を話す",   "思い出を話す"),
                    ("別の話題にする", "別の昭和の話題"),
                    ("昭和の歌を聴く", "昭和の歌"),
                    _QR_BACK,
                ])
                try:
                    line_bot_api.reply_message(r_token, TextSendMessage(text=reply_text, quick_reply=qr))
                except Exception:
                    safe_push_message(uid, [TextSendMessage(text=reply_text, quick_reply=qr)], uinfo)
            except Exception as e:
                logging.exception("showa start error: %s", e)

        threading.Thread(
            target=_showa_start_process,
            args=(user_id, prompt, user_info, event.reply_token),
            daemon=True,
        ).start()
        return

    # 別の昭和の話題
    if msg == "別の昭和の話題":
        _birth   = (user_info or {}).get("birthdate", "")
        _gender  = (user_info or {}).get("gender")
        _era     = _get_era_from_birthdate(_birth)
        _season  = _get_current_season()
        _exclude = _showa_sessions.get(user_id, {}).get("topic", "")
        rag = _get_showa_rag(_gender, _era, _season, exclude_topic=_exclude)
        _showa_sessions[user_id] = {
            "era": _era, "gender": _gender,
            "topic": rag.get("topic", "") if rag else "",
        }
        _clear_history(user_id)
        _name = (user_info or {}).get("name") or ""
        prompt = (
            f"ユーザー：{_name}さん（{_era}年代生まれ、性別：{_gender or '不明'}）\n\n"
            if _name else f"ユーザー：{_era}年代生まれ、性別：{_gender or '不明'}\n\n"
        )
        if rag:
            prompt += (
                f"新しい話題でユーザーに話しかけてください。\n"
                f"話題：{rag['topic']}\n質問文：{rag['question']}\n"
                f"背景知識：{rag['background']}\n深掘り質問：{rag['followup']}"
            )
        else:
            prompt += "昭和の懐かしい話題で話しかけてください。"

        def _showa_topic_process(uid, prpt, uinfo, r_token):
            try:
                reply_text = get_showa_reply(uid, prpt, uinfo, rag)
                qr = _build_quick_reply([
                    ("思い出を話す",   "思い出を話す"),
                    ("別の話題にする", "別の昭和の話題"),
                    ("昭和の歌を聴く", "昭和の歌"),
                    _QR_BACK,
                ])
                try:
                    line_bot_api.reply_message(r_token, TextSendMessage(text=reply_text, quick_reply=qr))
                except Exception:
                    safe_push_message(uid, [TextSendMessage(text=reply_text, quick_reply=qr)], uinfo)
            except Exception as e:
                logging.exception("showa topic error: %s", e)

        threading.Thread(
            target=_showa_topic_process,
            args=(user_id, prompt, user_info, event.reply_token),
            daemon=True,
        ).start()
        return

    # 昭和の歌
    if msg in ("昭和の歌", "別の歌を教えて"):
        _birth  = (user_info or {}).get("birthdate", "")
        _gender = (user_info or {}).get("gender")
        _era    = _get_era_from_birthdate(_birth)
        _name   = (user_info or {}).get("name") or ""

        def _showa_song_process(uid, uinfo, r_token, era, gender, name):
            try:
                prompt = (
                    f"ユーザー：{name}さん（{era}年代生まれ、性別：{gender or '不明'}）\n\n"
                    if name else f"ユーザー：{era}年代生まれ、性別：{gender or '不明'}\n\n"
                )
                prompt += (
                    "このユーザーの年代と性別に合った昭和の名曲を1曲だけ選んで紹介してください。\n"
                    "以下の形式で答えてください：\n"
                    "「○○さんの年代といえば\nこの歌はご存知ですか？😊\n\n"
                    "🎵 曲名 / 歌手名\n昭和○○年の名曲ですよ！\n\n"
                    "（曲の思い出や背景を1〜2文で紹介する）」\n"
                    "YouTubeで聴けるよう、曲名と歌手名は正確に書いてください。"
                )
                response = anthropic_client.messages.create(
                    model=SHOWA_MODEL,
                    max_tokens=400,
                    system=SHOWA_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=API_TIMEOUT,
                )
                reply_text = next(
                    (b.text for b in response.content if b.type == "text"),
                    "昭和の名曲をYouTubeで検索してみてください😊",
                )
                reply_text = re.sub(r'\*\*(.+?)\*\*', r'\1', reply_text)
                reply_text = re.sub(r'\*(.+?)\*',     r'\1', reply_text)
                reply_text = re.sub(r'^#{1,6}\s+',    '',    reply_text, flags=re.MULTILINE)
                # YouTubeリンク生成用にタイトルを抽出（フォールバック）
                yt_url = "https://www.youtube.com/results?search_query=昭和+歌謡曲+名曲"
                qr = _build_quick_reply([
                    ("YouTubeで聴く",        yt_url[:20]),
                    ("この歌の思い出を話す", "思い出を話す"),
                    ("別の歌を教えて",       "別の歌を教えて"),
                    _QR_BACK,
                ])
                # YouTubeリンクはURIアクション（quickreplyでは開けないのでテキストに含める）
                full_text = reply_text + f"\n\n▶ YouTubeで聴く\nhttps://www.youtube.com/results?search_query=昭和+歌謡曲+{era}年代"
                try:
                    line_bot_api.reply_message(r_token, TextSendMessage(text=full_text, quick_reply=_build_quick_reply([
                        ("この歌の思い出を話す", "思い出を話す"),
                        ("別の歌を教えて",       "別の歌を教えて"),
                        ("昭和トークに戻る",     "昭和トーク開始"),
                        _QR_BACK,
                    ])))
                except Exception:
                    safe_push_message(uid, [TextSendMessage(text=full_text)], uinfo)
            except Exception as e:
                logging.exception("showa song error: %s", e)

        threading.Thread(
            target=_showa_song_process,
            args=(user_id, user_info, event.reply_token, _era, _gender, _name),
            daemon=True,
        ).start()
        return

    # 昭和今日は何の日
    if msg == "昭和今日は何の日":
        _name  = (user_info or {}).get("name") or ""
        _birth = (user_info or {}).get("birthdate", "")
        _era   = _get_era_from_birthdate(_birth)
        today  = datetime.now()

        def _showa_today_process(uid, uinfo, r_token, name, era, t):
            try:
                prompt = (
                    f"今日は{t.month}月{t.day}日です。\n"
                    "昭和時代（1926〜1989年）に起きた、この日（または近い日）の出来事を1つ選んで紹介してください。\n"
                    "以下の形式で答えてください（マークダウン禁止）：\n"
                    f"「今日（{t.month}月{t.day}日）は昭和の歴史的な日ですよ😊\n\n"
                    "📅 昭和○○年○月○日\n○○がありました！\n\n"
                    f"{name}さんはあの頃何をしていましたか？」"
                    if name else
                    f"「今日（{t.month}月{t.day}日）は昭和の歴史的な日ですよ😊\n\n"
                    "📅 昭和○○年○月○日\n○○がありました！\n\n"
                    "あの頃どんな思い出がありますか？」"
                )
                response = anthropic_client.messages.create(
                    model=SHOWA_MODEL,
                    max_tokens=400,
                    system=SHOWA_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=API_TIMEOUT,
                )
                reply_text = next(
                    (b.text for b in response.content if b.type == "text"),
                    "今日も昭和の記念日かもしれませんよ😊",
                )
                reply_text = re.sub(r'\*\*(.+?)\*\*', r'\1', reply_text)
                reply_text = re.sub(r'\*(.+?)\*',     r'\1', reply_text)
                reply_text = re.sub(r'^#{1,6}\s+',    '',    reply_text, flags=re.MULTILINE)
                qr = _build_quick_reply([
                    ("思い出を話す",     "思い出を話す"),
                    ("別の出来事を教えて", "昭和今日は何の日"),
                    ("昭和の歌を聴く",   "昭和の歌"),
                    _QR_BACK,
                ])
                try:
                    line_bot_api.reply_message(r_token, TextSendMessage(text=reply_text, quick_reply=qr))
                except Exception:
                    safe_push_message(uid, [TextSendMessage(text=reply_text, quick_reply=qr)], uinfo)
            except Exception as e:
                logging.exception("showa today error: %s", e)

        threading.Thread(
            target=_showa_today_process,
            args=(user_id, user_info, event.reply_token, _name, _era, today),
            daemon=True,
        ).start()
        return

    # 旅行提案
    if msg == "旅行提案":
        _travel_states.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, _flex_travel_menu())
        return

    # 旅行: タイプ選択 → 体力確認
    if msg.startswith("旅行:"):
        travel_type = msg[3:].strip()  # 日帰り / 1泊2日 / 近場
        _travel_states[user_id] = {"type": travel_type}
        type_label = {"日帰り": "日帰り旅行", "1泊2日": "1泊2日の旅", "近場": "近場のお出かけ"}.get(travel_type, travel_type)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"{type_label}ですね😊\n体の調子はどうですか？",
                quick_reply=_build_quick_reply([
                    ("バリバリ動ける",     "旅行体力:バリバリ動ける"),
                    ("少し疲れやすい",     "旅行体力:少し疲れやすい"),
                    ("なるべくゆっくり",   "旅行体力:なるべくゆっくり"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 旅行: 体力確認 → 予算確認
    if msg.startswith("旅行体力:"):
        fitness = msg[5:].strip()
        st = _travel_states.get(user_id, {})
        st["fitness"] = fitness
        _travel_states[user_id] = st
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="予算はどのくらいですか？",
                quick_reply=_build_quick_reply([
                    ("3,000円以内",   "旅行予算:3000円以内"),
                    ("5,000円くらい", "旅行予算:5000円くらい"),
                    ("気にしない",     "旅行予算:気にしない"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 旅行: 予算確認 → 目的確認
    if msg.startswith("旅行予算:"):
        budget = msg[5:].strip()
        st = _travel_states.get(user_id, {})
        st["budget"] = budget
        _travel_states[user_id] = st
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="どんな目的ですか？",
                quick_reply=_build_quick_reply([
                    ("自然・景色を楽しむ",   "旅行目的:自然・景色"),
                    ("温泉・グルメを楽しむ", "旅行目的:温泉・グルメ"),
                    ("名所・歴史を巡る",     "旅行目的:名所・歴史"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # ── 趣味・生きがい（メニュー・状態セット、Claude不要）─────────────────

    if msg == "趣味生きがい":
        _name = (user_info or {}).get("name") or ""
        name_part = f"{_name}さん、" if _name else ""
        line_bot_api.reply_message(
            event.reply_token,
            [
                TextSendMessage(text=f"{name_part}趣味や生きがいについて\n一緒に考えましょう😊"),
                _flex_hobby_menu(_name),
            ],
        )
        return

    if msg == "趣味を探す":
        _hobby_states.pop(user_id, None)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="今何か趣味はありますか？😊",
                quick_reply=_build_quick_reply([
                    ("昔の趣味を再開したい",     "昔の趣味を再開したい"),
                    ("新しい趣味を始めたい",     "新しい趣味を始めたい"),
                    ("体に合った趣味を知りたい", "体に合った趣味を知りたい"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    if msg == "昔の趣味を再開したい":
        _hobby_states[user_id] = {"step": "awaiting_hobby_reopen"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "どんな趣味をお持ちでしたか？😊\n"
                    "教えてもらえれば\n"
                    "再開のお手伝いをしますよ！"
                ),
                quick_reply=_build_quick_reply([_QR_BACK]),
            ),
        )
        return

    if msg in ("新しい趣味を始めたい", "別の趣味も見てみる"):
        _hobby_states.pop(user_id, None)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="どんなことが好きですか？😊",
                quick_reply=_build_quick_reply([
                    ("体を動かすのが好き", "趣味興味:体を動かすのが好き"),
                    ("手先を使うのが好き", "趣味興味:手先を使うのが好き"),
                    ("音楽・芸術が好き",   "趣味興味:音楽・芸術が好き"),
                    ("学ぶことが好き",     "趣味興味:学ぶことが好き"),
                    ("自然・植物が好き",   "趣味興味:自然・植物が好き"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    if msg == "体に合った趣味を知りたい":
        _hobby_states.pop(user_id, None)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "体の状態を教えてください😊\n"
                    "無理なく楽しめる趣味を\n"
                    "提案しますよ！"
                ),
                quick_reply=_build_quick_reply([
                    ("膝・腰が痛い",       "趣味体状態:膝・腰が痛い"),
                    ("目が疲れやすい",     "趣味体状態:目が疲れやすい"),
                    ("手先が動かしにくい", "趣味体状態:手先が動かしにくい"),
                    ("特に問題なし",       "趣味体状態:特に問題なし"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    if msg == "教室仲間を探す":
        _hobby_states.pop(user_id, None)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="何をお探しですか？😊",
                quick_reply=_build_quick_reply([
                    ("趣味の教室を探す",       "趣味の教室を探す"),
                    ("サークル・クラブを探す", "サークル・クラブを探す"),
                    ("ボランティアを探す",     "ボランティアを探す"),
                    ("地域活動を探す",         "地域活動を探す"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    if msg == "趣味の教室を探す":
        _hobby_states[user_id] = {"step": "awaiting_classroom_name"}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "どんな教室をお探しですか？😊\n"
                    "趣味の名前を教えてください"
                ),
                quick_reply=_build_quick_reply([_QR_BACK]),
            ),
        )
        return

    if msg == "サークル・クラブを探す":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="どんなサークルをお探しですか？😊",
                quick_reply=_build_quick_reply([
                    ("スポーツ・体操", "サークル選択:スポーツ・体操"),
                    ("文化・芸術",     "サークル選択:文化・芸術"),
                    ("音楽・カラオケ", "サークル選択:音楽・カラオケ"),
                    ("園芸・料理",     "サークル選択:園芸・料理"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    if msg == "ボランティアを探す":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="素晴らしいですね😊\nどんな活動に興味がありますか？",
                quick_reply=_build_quick_reply([
                    ("子どもに関わる活動",     "ボランティア選択:子どもに関わる活動"),
                    ("高齢者支援",             "ボランティア選択:高齢者支援"),
                    ("環境・清掃活動",         "ボランティア選択:環境・清掃活動"),
                    ("地域のお祭り・イベント", "ボランティア選択:地域のお祭り・イベント"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    if msg == "地域活動を探す":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "地域活動への参加は\n"
                    "素晴らしいですね😊\n\n"
                    "以下から探してみましょう！"
                ),
                quick_reply=_build_quick_reply([
                    ("町内会・自治会に参加する",   "地域活動選択:町内会・自治会に参加する"),
                    ("老人クラブに参加する",       "地域活動選択:老人クラブに参加する"),
                    ("シルバー人材センターで働く", "地域活動選択:シルバー人材センターで働く"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    if msg == "生きがいを見つける":
        _hobby_states[user_id] = {"step": "awaiting_ikigai"}
        _name = (user_info or {}).get("name") or ""
        name_part = f"{_name}さんの" if _name else "あなたの"
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    f"{name_part}人生経験は\n"
                    "とても素晴らしいですよ😊\n"
                    "一緒に生きがいを見つけましょう！\n\n"
                    "これまでの人生で\n"
                    "一番楽しかったことや\n"
                    "得意だったことは何ですか？😊\n"
                    "どんな小さなことでも\n"
                    "大丈夫ですよ！"
                ),
                quick_reply=_build_quick_reply([_QR_BACK]),
            ),
        )
        return

    if msg == "スケジュール登録":
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "目標をスケジュールに\n"
                    "入れておきましょう😊\n"
                    "LINEカレンダーで管理できますよ！"
                ),
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=URIAction(
                        label="📅 カレンダーを開く",
                        uri="https://liff.line.me/2009711933-tXV7CqW9/calendar",
                    )),
                    QuickReplyButton(action=MessageAction(label="🏠 最初に戻る", text="最初に戻る")),
                ]),
            ),
        )
        return

    if msg in ("LINEカレンダーの使い方を教えて", "カレンダーの使い方を教えて"):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "LINEカレンダーの使い方を\n"
                    "説明しますね😊\n\n"
                    "STEP1：LINEを開く\n"
                    "・いつものLINEアプリを開いてください\n\n"
                    "STEP2：カレンダーを探す\n"
                    "・画面下のメニューを見てください\n"
                    "・カレンダーのアイコンをタップ\n\n"
                    "STEP3：予定を追加する\n"
                    "・右下の「＋」ボタンをタップ\n"
                    "・日付と予定の内容を入力\n"
                    "・「保存」をタップ\n\n"
                    "STEP4：家族と共有する\n"
                    "・予定を作成する時に\n"
                    "  共有したい家族を選べます\n\n"
                    "これだけで使えますよ！😊\n"
                    "わからないことがあれば\n"
                    "何でも聞いてくださいね"
                ),
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=URIAction(
                        label="📅 カレンダーを開く",
                        uri="https://liff.line.me/2009711933-tXV7CqW9/calendar",
                    )),
                    QuickReplyButton(action=MessageAction(
                        label="もっと詳しく教えて",
                        text="LINEカレンダーについてもっと詳しく教えてください",
                    )),
                    QuickReplyButton(action=MessageAction(label="🏠 最初に戻る", text="最初に戻る")),
                ]),
            ),
        )
        return

    # 趣味興味選択（「新しい趣味を始めたい」STEP2 → STEP3）
    if msg.startswith("趣味興味:"):
        interest = msg[len("趣味興味:"):]
        _hobby_states[user_id] = {"step": "awaiting_fitness", "data": {"interest": interest}}
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "体の状態を教えてください😊\n"
                    "無理なく楽しめる趣味を\n"
                    "提案しますよ！"
                ),
                quick_reply=_build_quick_reply([
                    ("活動的に動ける",        "趣味体力:活動的に動ける"),
                    ("軽めの運動なら大丈夫",  "趣味体力:軽めの運動なら大丈夫"),
                    ("座ってできることがいい", "趣味体力:座ってできることがいい"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # サークル選択（Maps URL表示）
    if msg.startswith("サークル選択:"):
        circle_type = msg[len("サークル選択:"):]
        maps_kominkan = _maps_url("公民館", user_info, 14)
        maps_rojin = _maps_url("老人センター", user_info, 14)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    f"{circle_type}のサークルを探しましょう😊\n"
                    "お住まいの地域の\n"
                    "公民館や老人センターに\n"
                    "問い合わせるのがおすすめですよ！"
                ),
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=URIAction(label="🗺️ 地図で探す", uri=maps_kominkan)),
                    QuickReplyButton(action=URIAction(label="🏠 老人センターを探す", uri=maps_rojin)),
                    QuickReplyButton(action=MessageAction(label="🏠 最初に戻る", text="最初に戻る")),
                ]),
            ),
        )
        return

    # ボランティア選択（Maps URL表示）
    if msg.startswith("ボランティア選択:"):
        vol_type = msg[len("ボランティア選択:"):]
        maps_shakyo = _maps_url("社会福祉協議会", user_info, 13)
        maps_vol = _maps_url("ボランティアセンター", user_info, 13)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    f"{vol_type}のボランティアを探しましょう😊\n"
                    "まず地域の社会福祉協議会に\n"
                    "相談するのがおすすめですよ！"
                ),
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=URIAction(label="🏛️ 社会福祉協議会を探す", uri=maps_shakyo)),
                    QuickReplyButton(action=URIAction(label="🤝 ボランティアセンターを探す", uri=maps_vol)),
                    QuickReplyButton(action=MessageAction(label="🏠 最初に戻る", text="最初に戻る")),
                ]),
            ),
        )
        return

    # 地域活動選択（Maps URL表示）
    if msg.startswith("地域活動選択:"):
        activity = msg[len("地域活動選択:"):]
        if "町内会" in activity or "自治会" in activity:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "お住まいの地域の\n"
                        "町内会・自治会に\n"
                        "直接問い合わせてみてください😊\n"
                        "市役所でも紹介してもらえますよ"
                    ),
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action=URIAction(
                            label="🏛️ 市役所を探す",
                            uri=_maps_url("市役所", user_info, 14),
                        )),
                        QuickReplyButton(action=MessageAction(label="🏠 最初に戻る", text="最初に戻る")),
                    ]),
                ),
            )
        elif "老人クラブ" in activity:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "地域の老人クラブは\n"
                        "60歳以上の方が参加できますよ😊\n"
                        "旅行・スポーツ・文化活動など\n"
                        "楽しいイベントがたくさんあります！"
                    ),
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action=URIAction(
                            label="🗺️ 老人センターを探す",
                            uri=_maps_url("老人センター", user_info, 14),
                        )),
                        QuickReplyButton(action=MessageAction(label="🏠 最初に戻る", text="最初に戻る")),
                    ]),
                ),
            )
        else:  # シルバー人材センター
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "シルバー人材センターでは\n"
                        "自分のペースで働けますよ😊\n"
                        "庭の手入れ・家事・清掃など\n"
                        "様々なお仕事があります！"
                    ),
                    quick_reply=QuickReply(items=[
                        QuickReplyButton(action=URIAction(
                            label="🗺️ シルバー人材センターを探す",
                            uri=_maps_url("シルバー人材センター", user_info, 13),
                        )),
                        QuickReplyButton(action=MessageAction(label="🏠 最初に戻る", text="最初に戻る")),
                    ]),
                ),
            )
        return

    # 教室名入力待ち（Maps URL表示、Claude不要）
    _hob_pre = _hobby_states.get(user_id)
    if _hob_pre and _hob_pre.get("step") == "awaiting_classroom_name":
        _hobby_states.pop(user_id, None)
        class_type = msg
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"{class_type}の教室を探しましょう😊",
                quick_reply=QuickReply(items=[
                    QuickReplyButton(action=URIAction(
                        label="🗺️ 地図で近くの教室を探す",
                        uri=_maps_url(f"{class_type}教室", user_info, 14),
                    )),
                    QuickReplyButton(action=URIAction(
                        label="🏛️ 公民館・市民センターで探す",
                        uri=_maps_url("公民館", user_info, 14),
                    )),
                    QuickReplyButton(action=URIAction(
                        label="🏠 老人センターで探す",
                        uri=_maps_url("老人センター", user_info, 14),
                    )),
                    QuickReplyButton(action=MessageAction(label="🏠 最初に戻る", text="最初に戻る")),
                ]),
            ),
        )
        return

    # ⑥ 会員登録（無料会員）/ AIに直接相談（有料会員）
    if msg in ("会員登録", "AIに直接相談"):
        is_paid = bool((user_info or {}).get("is_paid"))
        if is_paid:
            _name = (user_info or {}).get("name") or ""
            flex_msg = _flex_ai_direct_menu(_name)
            flex_msg.quick_reply = _build_quick_reply([
                ("健康のこと",   "健康について相談したいです"),
                ("地元のこと",   "地元の情報を教えてください"),
                ("なんでも話す", "話しかける"),
                _QR_BACK,
            ])
        else:
            flex_msg = _flex_upgrade_menu()
            flex_msg.quick_reply = _build_quick_reply([
                ("申し込む",   "有料会員の申し込み方法を教えてください"),
                ("詳しく聞く", "有料会員の詳細を教えてください"),
                _QR_BACK,
            ])
        line_bot_api.reply_message(event.reply_token, flex_msg)
        return

    # ── 緊急・危険キーワード（利用カウントを消費しない） ─────────────
    _EMERGENCY_KEYWORDS = {"助けて", "倒れた", "救急", "死にたい", "死にそう", "意識がない", "119", "110"}
    if any(kw in msg for kw in _EMERGENCY_KEYWORDS):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "すぐに119番か110番に\n"
                    "電話してください！\n\n"
                    "もし電話が難しければ\n"
                    "近くの人を呼んでください"
                )
            ),
        )
        return

    # ── 孤独・悲しいキーワード（利用カウントを消費しない） ───────────
    _LONELINESS_KEYWORDS = {"寂しい", "つらい", "悲しい", "一人ぼっち", "孤独", "誰もいない"}
    if any(kw in msg for kw in _LONELINESS_KEYWORDS):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "そうですか、つらいですね😢\n"
                    "もう少し話してみませんか？\n"
                    "私はいつでもここにいますよ"
                ),
                quick_reply=_build_quick_reply([
                    ("話を聞いてほしい",   "もう少し話を聞いてほしいです"),
                    ("元気が出る話をして", "元気が出る話をしてください"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # 利用回数チェック（is_paid なら通過、bonus_count → daily_count の順で消費）
    if not _check_and_increment_usage(user_id):
        _LIMIT_TEXT = (
            f"今日はたくさん話せましたね😊\n"
            "また明日も待っています！\n\n"
            "お友達を紹介すると\n"
            "5回追加されますよ🎁"
        )
        _LIMIT_QR = _build_quick_reply([
            ("友達に紹介する", "友達に紹介"),
            _QR_BACK,
        ])
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

    # 会話継続中かどうかを判定（直前がAIの返信かつ30分以内）
    in_conversation = _is_conversation_active(user_id)

    # ── 旅行: 目的確認 → Claude Sonnet でプラン提案 ─────────────────────
    if msg.startswith("旅行目的:"):
        purpose = msg[5:].strip()
        st = _travel_states.pop(user_id, {})
        travel_type = st.get("type", "日帰り")
        fitness     = st.get("fitness", "普通")
        budget      = st.get("budget", "気にしない")
        _name = (user_info or {}).get("name") or ""
        name_call = f"{_name}さん" if _name else "あなた"
        region = (user_info or {}).get("region") or (user_info or {}).get("prefecture") or "神奈川県"

        def _travel_process(uid, r_token, ttype, fit, bgt, purp, nm, reg):
            try:
                system = (
                    "あなたは旅行アドバイザーです。高齢者（60〜80代）向けに、"
                    "やさしい言葉で具体的な旅行プランを提案してください。\n"
                    "・マークダウン記法を使わない\n"
                    "・300字以内でまとめる\n"
                    "・昭和ゆかりのスポットや歴史的な場所があれば必ず1つ触れる"
                )
                prompt = (
                    f"{nm}さんへのおすすめ旅行プランを提案してください。\n"
                    f"・旅行タイプ：{ttype}\n"
                    f"・体力：{fit}\n"
                    f"・予算：{bgt}\n"
                    f"・目的：{purp}\n"
                    f"・出発地域：{reg}"
                )
                resp = anthropic_client.messages.create(
                    model=SHOWA_MODEL,
                    max_tokens=600,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    timeout=API_TIMEOUT,
                )
                reply_text = resp.content[0].text.strip()
                qr = _build_quick_reply([
                    ("昭和ゆかりの場所も見る", "なつかしい昭和"),
                    ("別のプランを見る",       "旅行提案"),
                    _QR_BACK,
                ])
                safe_reply_message(r_token, TextSendMessage(text=reply_text, quick_reply=qr), uid)
            except Exception as e:
                logging.exception("travel_process error: %s", e)
                safe_reply_message(r_token, TextSendMessage(text="プランの作成中にエラーが発生しました。もう一度お試しください。"), uid)

        t = threading.Thread(
            target=_travel_process,
            args=(user_id, event.reply_token, travel_type, fitness, budget, purpose, _name, region),
            daemon=True,
        )
        t.start()
        return

    # ── 昭和モード継続 ────────────────────────────────────────────────
    # 「思い出を話す」または昭和セッション中の自由発話は Claude Sonnet で処理
    if msg == "思い出を話す" or user_id in _showa_sessions:
        if user_id not in _showa_sessions:
            # セッション外から「思い出を話す」が来た場合は昭和トーク開始にリダイレクト
            _birth  = (user_info or {}).get("birthdate", "")
            _gender = (user_info or {}).get("gender")
            _era    = _get_era_from_birthdate(_birth)
            _showa_sessions[user_id] = {"era": _era, "gender": _gender, "topic": ""}

        def _showa_cont_process(uid, message, uinfo, r_token):
            try:
                reply_text = get_showa_reply(uid, message, uinfo)
                qr = _build_quick_reply([
                    ("もっと話す",     "もっと話す"),
                    ("別の昭和の話題", "別の昭和の話題"),
                    ("昭和の歌を聴く", "昭和の歌"),
                    _QR_BACK,
                ])
                try:
                    line_bot_api.reply_message(r_token, TextSendMessage(text=reply_text, quick_reply=qr))
                except Exception:
                    safe_push_message(uid, [TextSendMessage(text=reply_text, quick_reply=qr)], uinfo)
            except Exception as e:
                logging.exception("showa cont error: %s", e)

        threading.Thread(
            target=_showa_cont_process,
            args=(user_id, user_message, user_info, event.reply_token),
            daemon=True,
        ).start()
        return

    # ── 趣味・生きがい（Claude呼び出しあり）────────────────────────────

    # 状態ベース：昔の趣味再開・生きがい（自由入力 → Claude Sonnet）
    _hob = _hobby_states.get(user_id)
    if _hob:
        _hob_step = _hob.get("step")

        if _hob_step == "awaiting_hobby_reopen":
            _hobby_states.pop(user_id, None)
            hobby_name = msg
            _name = (user_info or {}).get("name") or ""

            def _reopen_proc(uid, uinfo, r_token, hname, name):
                try:
                    prompt = (
                        f"{name}さんが「{hname}」を再開したいそうです。\n"
                        "再開のためのアドバイスをお願いします。"
                        if name else
                        f"「{hname}」を再開したい方へのアドバイスをお願いします。"
                    )
                    reply_text = _hobby_claude_text(HOBBY_SYSTEM_REOPEN, prompt)
                    qr = _build_quick_reply([
                        ("教室・仲間を探す",    "教室仲間を探す"),
                        ("詳しく教えて",        f"趣味詳細:{hname}"),
                        ("他の趣味も見てみる",  "趣味を探す"),
                        _QR_BACK,
                    ])
                    try:
                        line_bot_api.reply_message(r_token, TextSendMessage(text=reply_text, quick_reply=qr))
                    except Exception:
                        safe_push_message(uid, [TextSendMessage(text=reply_text, quick_reply=qr)], uinfo)
                except Exception as e:
                    logging.exception("hobby reopen error: %s", e)

            threading.Thread(
                target=_reopen_proc,
                args=(user_id, user_info, event.reply_token, hobby_name, _name),
                daemon=True,
            ).start()
            return

        if _hob_step == "awaiting_ikigai":
            _hobby_states.pop(user_id, None)
            ikigai_text = msg
            _name = (user_info or {}).get("name") or ""

            def _ikigai_proc(uid, uinfo, r_token, text, name):
                try:
                    prompt = (
                        f"{name}さんからこんな話を聞かせてもらいました。\n\n{text}"
                        if name else
                        f"以下の話を聞かせてもらいました。\n\n{text}"
                    )
                    reply_text = _hobby_claude_text(HOBBY_SYSTEM_IKIGAI, prompt)
                    qr = _build_quick_reply([
                        ("もっと詳しく聞く",       "もっと詳しく教えてください"),
                        ("教室・仲間を探す",       "教室仲間を探す"),
                        ("スケジュールに入れる",   "スケジュール登録"),
                        _QR_BACK,
                    ])
                    try:
                        line_bot_api.reply_message(r_token, TextSendMessage(text=reply_text, quick_reply=qr))
                    except Exception:
                        safe_push_message(uid, [TextSendMessage(text=reply_text, quick_reply=qr)], uinfo)
                except Exception as e:
                    logging.exception("ikigai proc error: %s", e)

            threading.Thread(
                target=_ikigai_proc,
                args=(user_id, user_info, event.reply_token, ikigai_text, _name),
                daemon=True,
            ).start()
            return

    # 体力選択 → Claude Sonnetで趣味提案カルーセル
    if msg.startswith("趣味体力:"):
        fitness = msg[len("趣味体力:"):]
        _hob_state = _hobby_states.pop(user_id, {})
        interest = _hob_state.get("data", {}).get("interest", "なんでも")
        _birth = (user_info or {}).get("birthdate", "")
        _era = _get_era_from_birthdate(_birth)
        age_approx = 2025 - _era

        def _fitness_proc(uid, uinfo, r_token, intr, fit, age):
            try:
                proposals = _hobby_proposal_list(intr, fit, age)
                carousel = _flex_hobby_proposals(proposals)
                try:
                    line_bot_api.reply_message(r_token, [
                        TextSendMessage(text="あなたにぴったりの趣味を\n3つ提案しますね😊"),
                        carousel,
                    ])
                except Exception:
                    safe_push_message(uid, [carousel], uinfo)
            except Exception as e:
                logging.exception("hobby fitness proc error: %s", e)

        threading.Thread(
            target=_fitness_proc,
            args=(user_id, user_info, event.reply_token, interest, fitness, age_approx),
            daemon=True,
        ).start()
        return

    # 体の状態選択 → Claude Sonnetで趣味提案カルーセル
    if msg.startswith("趣味体状態:"):
        condition = msg[len("趣味体状態:"):]
        _birth = (user_info or {}).get("birthdate", "")
        _era = _get_era_from_birthdate(_birth)
        age_approx = 2025 - _era

        def _condition_proc(uid, uinfo, r_token, cond, age):
            try:
                proposals = _hobby_proposal_list(f"体の状態：{cond}", "", age)
                carousel = _flex_hobby_proposals(proposals)
                try:
                    line_bot_api.reply_message(r_token, [
                        TextSendMessage(text="体の状態に合った趣味を\n提案しますね😊"),
                        carousel,
                    ])
                except Exception:
                    safe_push_message(uid, [carousel], uinfo)
            except Exception as e:
                logging.exception("hobby condition proc error: %s", e)

        threading.Thread(
            target=_condition_proc,
            args=(user_id, user_info, event.reply_token, condition, age_approx),
            daemon=True,
        ).start()
        return

    # 趣味詳細
    if msg.startswith("趣味詳細:"):
        hobby_name = msg[len("趣味詳細:"):]
        _name = (user_info or {}).get("name") or ""

        def _detail_proc(uid, uinfo, r_token, hname, name):
            try:
                prompt = (
                    f"{name}さんが「{hname}」について詳しく聞きたいそうです。\n"
                    "以下の形式で答えてください（マークダウン禁止）：\n"
                    f"{hname}についてご説明しますね😊\n\n"
                    "始め方：\n具体的な3ステップ\n\n"
                    "必要なもの・費用：\n目安を具体的に\n\n"
                    "体への効果：\n健康・認知症予防など\n\n"
                    "お住まいの地域での楽しみ方：\n地域の教室・公園など"
                    if name else
                    f"「{hname}」について詳しく教えてください。\n"
                    "以下の形式で答えてください（マークダウン禁止）：\n"
                    f"{hname}についてご説明しますね😊\n\n"
                    "始め方：\n具体的な3ステップ\n\n"
                    "必要なもの・費用：\n目安を具体的に\n\n"
                    "体への効果：\n健康・認知症予防など\n\n"
                    "お住まいの地域での楽しみ方：\n地域の教室・公園など"
                )
                reply_text = _hobby_claude_text(HOBBY_SYSTEM_DETAIL, prompt)
                qr = _build_quick_reply([
                    ("教室・仲間を探す",    "教室仲間を探す"),
                    ("別の趣味も見てみる",  "新しい趣味を始めたい"),
                    _QR_BACK,
                ])
                try:
                    line_bot_api.reply_message(r_token, TextSendMessage(text=reply_text, quick_reply=qr))
                except Exception:
                    safe_push_message(uid, [TextSendMessage(text=reply_text, quick_reply=qr)], uinfo)
            except Exception as e:
                logging.exception("hobby detail error: %s", e)

        threading.Thread(
            target=_detail_proc,
            args=(user_id, user_info, event.reply_token, hobby_name, _name),
            daemon=True,
        ).start()
        return

    # ── 健康相談トリアージ（状態ベース）────────────────────────────────
    _hstate = _health_states.get(user_id)
    if _hstate == "awaiting_symptom":
        result = _triage_symptoms(msg)
        if result:
            _health_states.pop(user_id, None)
            line_bot_api.reply_message(event.reply_token, result)
            return
        else:
            # 判断できない → 追加質問（状態は維持しない・Claude へフォールスルー）
            _health_states.pop(user_id, None)
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "もう少し教えてください😊\n\n"
                        "・いつから症状がありますか？\n"
                        "・痛みの強さは1〜10でどのくらいですか？"
                    ),
                    quick_reply=_build_quick_reply([
                        ("症状を詳しく話す", "体の症状を相談する"),
                        ("病院を探す",       "近くの病院を探す"),
                        _QR_BACK,
                    ]),
                ),
            )
            return

    if _hstate == "awaiting_dept":
        _health_states.pop(user_id, None)
        line_bot_api.reply_message(event.reply_token, _route_department(msg))
        return

    if _hstate == "awaiting_side_effect":
        _health_states.pop(user_id, None)
        # 重篤な副作用キーワード
        _SEVERE_SIDE = {"呼吸困難", "全身に発疹", "意識がもうろう", "意識がない", "意識もうろう"}
        if any(kw in msg for kw in _SEVERE_SIDE):
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "これはすぐに病院へ行ってください！\n"
                        "薬の副作用の可能性があります\n\n"
                        "飲んでいる薬を持って\n救急に行きましょう"
                    ),
                    quick_reply=_build_quick_reply([
                        ("近くの病院を探す", "近くの病院を探す"),
                        _QR_BACK,
                    ]),
                ),
            )
        else:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        "しばらく様子を見て😊\n\n"
                        "改善しない場合は\n"
                        "薬局や病院に相談してみてください"
                    ),
                    quick_reply=_build_quick_reply([
                        ("近くの病院を探す", "近くの病院を探す"),
                        ("他のことを聞く",   "他のことを聞かせてください"),
                        _QR_BACK,
                    ]),
                ),
            )
        return

    # ── 食事レシピ（状態ベース・Claude呼び出し）────────────────────────
    _rstate = _recipe_states.get(user_id)
    if _rstate:
        _rs_step = _rstate.get("step")

        if _rs_step in ("ingredients", "condition"):
            # ユーザーが食材／体調を入力 → レシピカルーセルを生成
            ctx_type = _rstate.get("type", "dinner")
            _recipe_states.pop(user_id, None)
            try:
                carousel = _flex_recipe_carousel(ctx_type, msg)
                line_bot_api.reply_message(
                    event.reply_token,
                    [
                        TextSendMessage(text="少々お待ちください🍳\nぴったりのレシピを考えています..."),
                        carousel,
                    ],
                )
            except Exception as _e:
                logging.error("recipe carousel error: %s", _e)
                line_bot_api.reply_message(
                    event.reply_token,
                    TextSendMessage(
                        text="レシピの取得に失敗しました😢\nもう一度お試しください",
                        quick_reply=_build_quick_reply([("レシピを選ぶ", "食事レシピ"), _QR_BACK]),
                    ),
                )
            return

    # レシピ詳細（カルーセルのボタンから）
    if msg.startswith("レシピ詳細:"):
        recipe_name = msg[len("レシピ詳細:"):]
        detail = _claude_recipe_detail(recipe_name)
        bubble = _flex_recipe_detail_bubble(recipe_name, detail)
        line_bot_api.reply_message(
            event.reply_token,
            FlexSendMessage(alt_text=recipe_name, contents=bubble),
        )
        return

    # 1ステップずつ教えて（詳細カードのボタンから）
    if msg.startswith("1ステップずつ教えて:"):
        recipe_name = msg[len("1ステップずつ教えて:"):]
        detail = _claude_recipe_detail(recipe_name)
        steps = _parse_recipe_steps(detail)
        if steps:
            _recipe_states[user_id] = {
                "step": "step_by_step",
                "recipe_name": recipe_name,
                "steps": steps,
                "current": 0,
            }
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=(
                        f"「{recipe_name}」を\n一緒に作りましょう！😊\n\n"
                        f"【ステップ 1/{len(steps)}】\n\n{steps[0]}"
                    ),
                    quick_reply=_build_quick_reply([
                        ("次のステップ", "次のステップ"),
                        ("やっぱりやめる", "食事レシピ"),
                    ]),
                ),
            )
        else:
            # ステップを解析できなかった場合はテキストをそのまま表示
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=detail,
                    quick_reply=_build_quick_reply([
                        ("別のレシピを選ぶ", "別のレシピを提案してください"),
                        _QR_BACK,
                    ]),
                ),
            )
        return

    # 別のレシピを提案（直前の状態を見て再提案）
    if msg == "別のレシピを提案してください":
        line_bot_api.reply_message(event.reply_token, _flex_recipe_menu())
        return

    # ── 医療・法律の専門判断キーワード ──────────────────────────────
    _EXPERT_PATTERNS = re.compile(
        r'診断|手術|訴訟|裁判|遺言|相続|契約書|保険金|投資|詐欺被害'
    )
    if _EXPERT_PATTERNS.search(msg):
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=(
                    "専門の先生に相談するのが\n"
                    "一番安心ですよ😊\n\n"
                    "一緒に相談先を探しましょうか？"
                ),
                quick_reply=_build_quick_reply([
                    ("相談先を探す", "相談できる窓口を教えてください"),
                    ("他のことを聞く", "他のことを聞かせてください"),
                    _QR_BACK,
                ]),
            ),
        )
        return

    # FAQ直接返信チェック（会話継続中はスキップして文脈を維持）
    # FlexSendMessage はリッチメニューボタン経由のみ表示。通常会話はテキスト返答のみ。
    _FAQ_QR = _build_quick_reply([
        ("もっと詳しく聞く", "もっと詳しく教えてください"),
        ("他のことを聞く",   "他のことを聞かせてください"),
        _QR_BACK,
    ])
    if not in_conversation:
        try:
            faq_msg = _faq_direct_reply(user_message, user_info)
            if faq_msg is not None and isinstance(faq_msg, TextSendMessage):
                faq_msg.quick_reply = _FAQ_QR
                line_bot_api.reply_message(
                    event.reply_token,
                    faq_msg,
                )
                return
        except Exception as e:
            logging.error("faq direct reply check error: %s", e)

    # 登録済みユーザーへの Claude 返答：バックグラウンドスレッドで処理
    # reply_token（1分有効）を優先して使い、期限切れの場合のみ safe_push_message にフォールバック
    def _process(
        uid: str, msg: str, uinfo: dict,
        skip_faq: bool, save_missed: bool, r_token: str,
    ) -> None:
        reply_text = "申し訳ありません。\nただいま少し調子が悪いようです。\nしばらくしてからもう一度お試しください。"
        try:
            # TOTAL_REPLY_TIMEOUT 秒のハードタイムアウトで返答を保証
            # メッセージのDB保存は get_claude_reply 内で行う
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(get_claude_reply, uid, msg, uinfo, skip_faq, save_missed)
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
            # 飲食系クエリかつ地域登録済みユーザーならカルーセルも追加（新規トピックのみ）
            user_region = (uinfo or {}).get("region", "")
            if not skip_faq and _is_food_query(msg) and user_region:
                restaurants = _query_restaurants(msg)
                if restaurants:
                    messages_to_send.append(_build_restaurant_carousel(restaurants))
            # reply_token を 1 回で使い切る（無料）
            # 失敗（期限切れ等）した場合のみ safe_push_message にフォールバック（有料会員のみ）
            try:
                line_bot_api.reply_message(r_token, messages_to_send)
            except Exception:
                safe_push_message(uid, messages_to_send, uinfo)
        except Exception as e:
            logging.exception("send reply error: %s", e)

    # skip_faq = in_conversation（会話継続中は飲食店DB注入もスキップ）
    # save_missed = not in_conversation（新規トピックでFAQミスの場合のみ記録）
    threading.Thread(
        target=_process,
        args=(user_id, user_message, user_info, in_conversation, not in_conversation, event.reply_token),
        daemon=True,
    ).start()


# ── LIFF 共通レトロデザイン CSS ──────────────────────────
# 全LIFFページの <style> タグ内で _RETRO_CSS を埋め込んで使う。
# 例: <style>{retro_css} /* ページ固有のスタイル */ </style>
#     html = _LIFF_XXX_HTML.format(retro_css=_RETRO_CSS, ...)

_RETRO_CSS = """
/* ── レトロデザイン共通スタイル ─────────────────────── */
:root {
  --bg:          #F5E6A3;   /* 和紙イエロー */
  --header-bg:   #8B1A1A;   /* えんじ */
  --header-text: #FFD700;   /* 金 */
  --text:        #4A2C0A;   /* 濃茶 */
  --sub-text:    #6B4010;   /* 茶 */
  --btn-bg:      #8B1A1A;   /* えんじ */
  --btn-text:    #FFD700;   /* 金 */
  --border:      #8B6914;   /* 茶 */
  --card-bg:     #FFF8DC;   /* カード背景（少し明るいクリーム） */
  --divider:     #C8A060;   /* 区切り線 */
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: 'Hiragino Kaku Gothic ProN', 'Noto Sans JP', sans-serif;
  font-size: 16px;
  min-height: 100vh;
}

/* ヘッダー */
.retro-header {
  background: var(--header-bg);
  color: var(--header-text);
  text-align: center;
  padding: 14px 16px;
  font-size: 20px;
  font-weight: bold;
  letter-spacing: 0.1em;
  border-bottom: 3px solid var(--border);
}
.retro-header h1 { font-size: 22px; font-weight: bold; }
.retro-header p  { font-size: 15px; margin-top: 5px; opacity: .9; }

/* カード */
.retro-card {
  background: var(--card-bg);
  border: 2px solid var(--border);
  border-radius: 10px;
  padding: 16px;
  margin: 12px 16px;
  box-shadow: 2px 3px 0 var(--border);
}

/* ボタン */
.retro-btn {
  display: block;
  width: 100%;
  background: var(--btn-bg);
  color: var(--btn-text);
  border: none;
  border-radius: 8px;
  padding: 14px;
  font-size: 16px;
  font-weight: bold;
  text-align: center;
  cursor: pointer;
  letter-spacing: 0.05em;
  margin-top: 12px;
  box-shadow: 2px 3px 0 #5C1010;
}
.retro-btn:active { transform: translateY(2px); box-shadow: none; }

/* 入力フォーム */
.retro-input, .retro-select {
  width: 100%;
  padding: 10px 12px;
  border: 2px solid var(--border);
  border-radius: 8px;
  background: #FFFFF0;
  color: var(--text);
  font-size: 16px;
  margin-top: 6px;
}

/* ラベル */
.retro-label {
  font-size: 13px;
  color: var(--sub-text);
  font-weight: bold;
  margin-top: 12px;
  display: block;
}

/* 区切り線 */
.retro-divider {
  border: none;
  border-top: 2px dashed var(--divider);
  margin: 16px 0;
}

/* セクションタイトル */
.retro-section-title {
  font-size: 14px;
  font-weight: bold;
  color: var(--header-bg);
  border-left: 4px solid var(--header-bg);
  padding-left: 8px;
  margin: 16px 16px 8px;
}
"""

# ── LIFF マイページ ───────────────────────────────────

_LIFF_HTML = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=yes">
<title>マイページ</title>
<script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
<style>
{retro_css}
.wrap{{max-width:480px;margin:0 auto;padding:16px}}
.row{{display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px dashed var(--divider)}}
.row:last-child{{border-bottom:none}}
.lbl{{font-size:18px;color:var(--sub-text)}}
.val{{font-size:22px;font-weight:bold;color:var(--header-bg)}}
.val.grn{{color:#2e7d32}}
.info-lbl{{font-size:14px;color:var(--sub-text);margin-bottom:4px}}
.info-val{{font-size:20px;font-weight:bold;color:var(--text);margin-bottom:14px}}
.form-grp{{margin-bottom:14px}}
.loader{{text-align:center;padding:48px;color:var(--sub-text);font-size:18px}}
.errmsg{{background:#fff0f0;border:2px solid #c62828;border-radius:10px;padding:18px;color:#b71c1c;text-align:center;font-size:18px;line-height:1.6}}
.ok-msg{{background:#e8f5e9;border:2px solid #43a047;border-radius:8px;padding:12px;color:#2e7d32;text-align:center;font-size:18px;display:none;margin-bottom:12px}}
.badge{{display:inline-block;background:#f57c00;color:#fff;padding:4px 12px;border-radius:20px;font-size:16px;font-weight:bold;margin-left:8px}}
</style>
</head>
<body>
<div class="retro-header">
  <h1>🏠 マイページ</h1>
  <p id="greeting"></p>
</div>
<div class="wrap">
  <div id="loader" class="loader">読み込み中…</div>
  <div id="errmsg" class="errmsg" style="display:none"></div>
  <div id="content" style="display:none">

    <!-- 利用状況 -->
    <div class="retro-card">
      <div class="retro-section-title">📊 今日の利用状況</div>
      <div class="row">
        <span class="lbl">今日の残り回数</span>
        <span class="val" id="v-remaining">-</span>
      </div>
      <div class="row">
        <span class="lbl">ボーナス回数 🎁</span>
        <span class="val grn" id="v-bonus">-</span>
      </div>
    </div>

    <!-- 登録情報 -->
    <div class="retro-card">
      <div class="retro-section-title">👤 登録情報</div>
      <div class="info-lbl">お住まい</div>
      <div class="info-val" id="v-location">-</div>
      <div class="info-lbl">生年月日</div>
      <div class="info-val" id="v-birthdate">-</div>
    </div>

    <!-- 編集フォーム -->
    <div class="retro-card">
      <div class="retro-section-title">✏️ 登録情報を変更する</div>
      <div id="ok-msg" class="ok-msg">✅ 保存しました！</div>
      <div class="form-grp">
        <label class="retro-label">お名前（任意）</label>
        <input class="retro-input" id="f-name" type="text" placeholder="例：田中 花子">
      </div>
      <div class="form-grp">
        <label class="retro-label">都道府県</label>
        <select class="retro-select" id="f-pref"></select>
      </div>
      <div class="form-grp">
        <label class="retro-label">市区町村</label>
        <select class="retro-select" id="f-city"></select>
      </div>
      <div class="form-grp">
        <label class="retro-label">生年月日</label>
        <input class="retro-input" id="f-birth" type="text" placeholder="例：1950年1月1日">
      </div>
      <button class="retro-btn" onclick="save()">💾 保存する</button>
    </div>

  </div><!-- /content -->
</div><!-- /wrap -->

<script>
var LIFF_ID = "{liff_id}";
var PREFS   = {prefs_json};
var CITIES  = {cities_json};
var uid     = null;

// 都道府県セレクト構築
var psel = document.getElementById('f-pref');
psel.innerHTML = '<option value="">選択してください</option>';
PREFS.forEach(function(p){{
  var o = document.createElement('option'); o.value = o.textContent = p; psel.appendChild(o);
}});
psel.addEventListener('change', function(){{
  buildCitySelect(this.value, '');
}});

function buildCitySelect(pref, selected){{
  var csel = document.getElementById('f-city');
  csel.innerHTML = '<option value="">選択してください</option>';
  (CITIES[pref]||[]).forEach(function(c){{
    var o=document.createElement('option'); o.value=o.textContent=c;
    if(c===selected) o.selected=true;
    csel.appendChild(o);
  }});
}}

// LIFF 初期化
liff.init({{liffId: LIFF_ID}})
  .then(function(){{
    if(!liff.isLoggedIn()){{ liff.login(); return; }}
    return liff.getProfile();
  }})
  .then(function(profile){{
    if(!profile) return;
    uid = profile.userId;
    document.getElementById('greeting').textContent = profile.displayName + 'さん';
    return loadUser(profile.userId);
  }})
  .catch(function(e){{
    showErr('ログインできませんでした。\\nLINEアプリから開き直してください。');
  }});

function loadUser(userId){{
  return fetch('/liff/api/user?line_user_id=' + encodeURIComponent(userId))
    .then(function(r){{ return r.json(); }})
    .then(function(d){{
      if(d.error){{ showErr('ユーザー情報が見つかりません。\\nLINEで登録を完了してください。'); return; }}
      render(d);
    }})
    .catch(function(){{ showErr('通信エラーが発生しました。'); }});
}}

function render(d){{
  document.getElementById('v-remaining').textContent = d.is_paid ? '無制限' : (d.remaining_today + ' 回');
  document.getElementById('v-bonus').textContent = d.bonus_count + ' 回';
  document.getElementById('v-location').textContent = (d.prefecture||'') + (d.city||'') || '未設定';
  document.getElementById('v-birthdate').textContent = d.birthdate || '未設定';
  document.getElementById('f-name').value  = d.name || '';
  document.getElementById('f-birth').value = d.birthdate || '';
  if(d.prefecture){{
    document.getElementById('f-pref').value = d.prefecture;
    buildCitySelect(d.prefecture, d.city||'');
  }}
  document.getElementById('loader').style.display  = 'none';
  document.getElementById('content').style.display = 'block';
}}

function save(){{
  if(!uid) return;
  var pref  = document.getElementById('f-pref').value;
  var city  = document.getElementById('f-city').value;
  var body  = {{
    line_user_id: uid,
    name:         document.getElementById('f-name').value.trim(),
    prefecture:   pref,
    city:         city,
    birthdate:    document.getElementById('f-birth').value.trim()
  }};
  fetch('/liff/api/user', {{
    method: 'POST',
    headers: {{'Content-Type':'application/json'}},
    body: JSON.stringify(body)
  }})
  .then(function(r){{ return r.json(); }})
  .then(function(d){{
    if(d.success){{
      document.getElementById('v-location').textContent = (pref||'') + (city||'') || '未設定';
      var m = document.getElementById('ok-msg');
      m.style.display = 'block';
      setTimeout(function(){{ m.style.display='none'; }}, 3000);
    }} else {{
      alert('保存に失敗しました。もう一度お試しください。');
    }}
  }})
  .catch(function(){{ alert('通信エラーが発生しました。'); }});
}}

function showErr(msg){{
  document.getElementById('loader').style.display = 'none';
  var b = document.getElementById('errmsg');
  b.textContent = msg; b.style.display = 'block';
}}
</script>
</body>
</html>
"""


# LIFF がエンドポイント URL に liff.state クエリパラメータを付けてリダイレクトする際の
# ベースルート。パスを解析して各ページへサーバーサイドリダイレクトする。
_LIFF_VALID_PATHS = {
    "/mypage":   "/liff/mypage",
    "/invite":   "/liff/invite",
    "/faq":      "/liff/faq",
    "/search":   "/liff/search",
    "/map":      "/liff/map",
    "/schedule": "/liff/schedule",
    "/memo":     "/liff/memo",
    "/travel":   "/liff/travel",
    "/calendar": "/liff/calendar",
    "/today":    "/liff/today",
}

@app.route("/liff", methods=["GET"])
def liff_base():
    import urllib.parse
    state = request.args.get("liff.state", "").strip()
    if state:
        # state は "/invite" や "/map?foo=bar" のような形式
        path = urllib.parse.unquote(state)
        # パスのみ取り出す（クエリ込みの場合も考慮）
        path_only = path.split("?")[0].rstrip("/")
        dest = _LIFF_VALID_PATHS.get(path_only)
        if dest:
            qs = path.split("?", 1)[1] if "?" in path else ""
            return redirect(dest + ("?" + qs if qs else ""), code=302)
    # state がない・不明なパスはマイページへ
    return redirect("/liff/mypage", code=302)


@app.route("/liff/mypage", methods=["GET"])
def liff_mypage():
    return render_template("liff_mypage.html", liff_id=LIFF_ID)


@app.route("/liff/api/mypage", methods=["GET"])
def liff_api_mypage_get():
    user_id = request.args.get("line_user_id", "").strip()
    if not user_id:
        return jsonify({"error": "line_user_id required"}), 400
    try:
        sb = get_supabase()
        result = sb.table("users").select(
            "name, prefecture, city, birthdate, gender, is_paid, "
            "daily_count, bonus_count, last_used_date, referral_code"
        ).eq("line_user_id", user_id).limit(1).execute()
        if not result.data:
            return jsonify({"error": "user not found"}), 404
        u = result.data[0]
        today = date.today().isoformat()
        is_paid = bool(u.get("is_paid"))
        if is_paid:
            daily_remaining = 999
        else:
            last_used   = u.get("last_used_date")
            daily_count = u.get("daily_count") or 0
            if last_used != today:
                daily_count = 0
            daily_remaining = max(0, FREE_DAILY_LIMIT - daily_count)
        bonus_count = u.get("bonus_count") or 0
        total_remaining = 999 if is_paid else (daily_remaining + bonus_count)
        return jsonify({
            "name":            u.get("name") or "",
            "prefecture":      u.get("prefecture") or "",
            "city":            u.get("city") or "",
            "birthday":        u.get("birthdate") or "",
            "gender":          u.get("gender") or "",
            "is_paid":         is_paid,
            "daily_remaining": daily_remaining,
            "bonus_count":     bonus_count,
            "total_remaining": total_remaining,
            "referral_code":   u.get("referral_code") or "",
        })
    except Exception as e:
        logging.exception("liff_api_mypage_get error: %s", e)
        return jsonify({"error": "server error"}), 500


@app.route("/liff/api/mypage", methods=["POST"])
def liff_api_mypage_post():
    data = request.get_json(silent=True) or {}
    user_id = data.get("line_user_id", "").strip()
    if not user_id:
        return jsonify({"error": "line_user_id required"}), 400
    update: dict = {}
    if "name" in data:
        update["name"] = (data["name"] or "").strip()
    if "prefecture" in data or "city" in data:
        pref = (data.get("prefecture") or "").strip()
        city = (data.get("city") or "").strip()
        update["prefecture"] = pref
        update["city"]       = city
        update["region"]     = pref + city
    if "birthday" in data:
        update["birthdate"] = (data["birthday"] or "").strip()
    if "gender" in data:
        g_val = (data["gender"] or "").strip()
        if g_val in ("male", "female", ""):
            update["gender"] = g_val if g_val else None
    if not update:
        return jsonify({"error": "no fields to update"}), 400
    try:
        get_supabase().table("users").update(update).eq("line_user_id", user_id).execute()
        user_cache.pop(user_id, None)
        return jsonify({"success": True})
    except Exception as e:
        logging.exception("liff_api_mypage_post error: %s", e)
        return jsonify({"error": "server error"}), 500


@app.route("/liff/api/user", methods=["GET"])
def liff_get_user():
    user_id = request.args.get("line_user_id", "").strip()
    if not user_id:
        return jsonify({"error": "line_user_id required"}), 400
    try:
        result = get_supabase().table("users").select(
            "name, region, prefecture, city, birthdate, is_paid, daily_count, bonus_count, last_used_date"
        ).eq("line_user_id", user_id).limit(1).execute()
        if not result.data:
            return jsonify({"error": "user not found"}), 404
        u = result.data[0]
        today = date.today().isoformat()
        is_paid = bool(u.get("is_paid"))
        if is_paid:
            remaining = 999
        else:
            last_used   = u.get("last_used_date")
            daily_count = u.get("daily_count") or 0
            if last_used != today:
                daily_count = 0
            remaining = max(0, FREE_DAILY_LIMIT - daily_count)
        return jsonify({
            "name":           u.get("name") or "",
            "prefecture":     u.get("prefecture") or "",
            "city":           u.get("city") or "",
            "birthdate":      u.get("birthdate") or "",
            "is_paid":        is_paid,
            "remaining_today": remaining,
            "bonus_count":    u.get("bonus_count") or 0,
        })
    except Exception as e:
        logging.exception("liff_get_user error: %s", e)
        return jsonify({"error": "server error"}), 500


@app.route("/liff/api/user", methods=["POST"])
def liff_update_user():
    data = request.get_json(silent=True) or {}
    user_id = data.get("line_user_id", "").strip()
    if not user_id:
        return jsonify({"error": "line_user_id required"}), 400
    update: dict = {}
    if "name" in data:
        update["name"] = (data["name"] or "").strip()
    if "prefecture" in data or "city" in data:
        pref = (data.get("prefecture") or "").strip()
        city = (data.get("city") or "").strip()
        update["prefecture"] = pref
        update["city"]       = city
        update["region"]     = pref + city
    if "birthdate" in data:
        update["birthdate"] = (data["birthdate"] or "").strip()
    if not update:
        return jsonify({"error": "no fields to update"}), 400
    try:
        get_supabase().table("users").update(update).eq("line_user_id", user_id).execute()
        user_cache.pop(user_id, None)   # キャッシュを無効化
        return jsonify({"success": True})
    except Exception as e:
        logging.exception("liff_update_user error: %s", e)
        return jsonify({"error": "server error"}), 500


# ── LIFF 紹介ページ ───────────────────────────────────

@app.route("/liff/invite", methods=["GET"])
def liff_invite():
    return render_template("liff_invite.html",
        liff_id=os.environ.get("LIFF_ID", LIFF_ID),
    )


@app.route("/liff/api/invite", methods=["GET"])
def liff_api_invite():
    user_id = request.args.get("line_user_id", "").strip()
    if not user_id:
        return jsonify({"error": "line_user_id required"}), 400
    try:
        sb = get_supabase()
        result = sb.table("users").select(
            "referral_code, bonus_count"
        ).eq("line_user_id", user_id).execute()
        if not result.data:
            return jsonify({"error": "user not found"}), 404
        row = result.data[0]
        referral_code = row.get("referral_code") or ""
        bonus_count   = row.get("bonus_count") or 0
        # 自分の紹介コードを使って登録した人数
        referred_count = 0
        if referral_code:
            cnt = sb.table("users").select("id", count="exact").eq(
                "referred_by", referral_code.upper()
            ).execute()
            referred_count = cnt.count if cnt.count is not None else 0
        return jsonify({
            "referral_code": referral_code,
            "bonus_count":   bonus_count,
            "referred_count": referred_count,
        })
    except Exception as e:
        logging.exception("liff_api_invite error: %s", e)
        return jsonify({"error": "server error"}), 500


@app.route("/liff/api/referral", methods=["GET"])
def liff_get_referral():
    user_id = request.args.get("line_user_id", "").strip()
    if not user_id:
        return jsonify({"error": "line_user_id required"}), 400
    try:
        code = _get_referral_code(user_id)
        return jsonify({"referral_code": code})
    except Exception as e:
        logging.exception("liff_get_referral error: %s", e)
        return jsonify({"error": "server error"}), 500


# ── ① LIFF FAQ一覧・検索 ─────────────────────────────

_LIFF_FAQ_GENRES = [
    "健康・病院", "食事・レシピ", "地元情報", "スマホ相談",
    "趣味・生きがい", "お金・年金", "家の困り事", "季節の話題",
    "運動", "家族・介護",
]

_LIFF_FAQ_HTML = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=yes">
<title>よくある質問</title>
<script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
<style>
{retro_css}
body{{padding-bottom:100px}}
/* 検索バー */
.search-wrap{{background:var(--card-bg);padding:14px 16px;position:sticky;top:0;z-index:20;box-shadow:0 2px 8px rgba(0,0,0,.12);border-bottom:2px solid var(--border)}}
.search-inner{{display:flex;gap:10px;max-width:600px;margin:0 auto}}
.search-inner input{{
  flex:1;font-size:18px;padding:12px 14px;
  border:2px solid var(--border);border-radius:8px;
  outline:none;color:var(--text);background:#fffff0;
}}
.search-inner input:focus{{border-color:var(--header-bg);box-shadow:0 0 0 3px rgba(139,26,26,.15)}}
.search-inner button{{
  font-size:16px;padding:12px 18px;
  background:var(--btn-bg);color:var(--btn-text);border:none;border-radius:8px;
  cursor:pointer;font-weight:bold;white-space:nowrap;
  box-shadow:2px 3px 0 #5C1010;
}}
.search-inner button:active{{transform:translateY(2px);box-shadow:none}}
/* ジャンルタブ */
.genre-wrap{{background:var(--card-bg);border-bottom:2px solid var(--border);overflow-x:auto}}
.genre-wrap::-webkit-scrollbar{{display:none}}
.genre-list{{display:flex;padding:10px 12px;gap:8px;min-width:max-content}}
.g-btn{{
  font-size:15px;padding:8px 16px;
  border:2px solid var(--border);background:var(--bg);color:var(--text);
  border-radius:20px;cursor:pointer;white-space:nowrap;
  font-weight:bold;transition:background .15s,color .15s;
}}
.g-btn.active{{background:var(--header-bg);color:var(--header-text);border-color:var(--header-bg)}}
/* 件数 */
.count-bar{{max-width:600px;margin:10px auto 4px;padding:0 16px;font-size:15px;color:var(--sub-text)}}
/* FAQリスト */
.faq-wrap{{max-width:600px;margin:0 auto;padding:0 12px 16px}}
.faq-item{{
  background:var(--card-bg);border:2px solid var(--border);border-radius:10px;margin-bottom:10px;
  box-shadow:2px 3px 0 var(--border);overflow:hidden;
}}
.faq-q{{
  padding:16px 48px 16px 16px;font-size:18px;font-weight:bold;
  cursor:pointer;position:relative;color:var(--header-bg);
  border-left:5px solid var(--header-bg);
  -webkit-tap-highlight-color:rgba(0,0,0,.06);
}}
.faq-q::after{{
  content:'▼';position:absolute;right:16px;top:50%;
  transform:translateY(-50%);font-size:16px;color:var(--sub-text);
  transition:transform .25s;
}}
.faq-item.open .faq-q::after{{transform:translateY(-50%) rotate(180deg)}}
.faq-item.open .faq-q{{background:var(--bg)}}
.faq-a{{
  display:none;padding:14px 16px 18px;
  font-size:17px;color:var(--text);border-top:1px dashed var(--divider);
  line-height:1.85;white-space:pre-wrap;word-break:break-all;
}}
.faq-item.open .faq-a{{display:block}}
.genre-badge{{
  display:inline-block;font-size:12px;
  background:var(--bg);color:var(--header-bg);border:1px solid var(--border);
  padding:2px 8px;border-radius:6px;
  margin-right:8px;font-weight:normal;vertical-align:middle;
}}
/* 状態表示 */
.loader{{text-align:center;padding:56px 16px;color:var(--sub-text);font-size:18px}}
.loader-spin{{display:inline-block;width:40px;height:40px;border:4px solid var(--bg);border-top-color:var(--header-bg);border-radius:50%;animation:spin .8s linear infinite;margin-bottom:12px}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
.empty{{text-align:center;padding:48px 16px;color:var(--sub-text);font-size:18px}}
/* AIボタン */
.ai-btn-wrap{{position:fixed;bottom:0;left:0;right:0;background:var(--card-bg);border-top:3px solid var(--border);padding:12px 16px;z-index:30}}
.ai-btn{{
  display:block;width:100%;max-width:600px;margin:0 auto;
  font-size:18px;font-weight:bold;padding:16px;
  background:var(--btn-bg);color:var(--btn-text);border:none;border-radius:8px;
  cursor:pointer;text-align:center;letter-spacing:.05em;
  box-shadow:2px 3px 0 #5C1010;
}}
.ai-btn:active{{transform:translateY(2px);box-shadow:none}}
</style>
</head>
<body>
<div class="retro-header">
  <h1>📖 よくある質問</h1>
  <p>知りたいことを検索、またはジャンルから探せます</p>
</div>

<div class="search-wrap">
  <div class="search-inner">
    <input id="q" type="search" placeholder="例：血圧、スマホ設定…" autocomplete="off"
      oninput="onSearchInput()" onkeydown="if(event.key==='Enter'){{this.blur();load();}}">
    <button onclick="load()">検索</button>
  </div>
</div>

<div class="genre-wrap">
  <div class="genre-list" id="genreList">
    <button class="g-btn active" data-g="" onclick="setGenre(this,'')">すべて</button>
  </div>
</div>

<div class="count-bar" id="countBar"></div>
<div class="faq-wrap">
  <div id="loader" class="loader">
    <div class="loader-spin"></div><br>読み込み中…
  </div>
  <div id="list"></div>
</div>

<div class="ai-btn-wrap">
  <button class="ai-btn" onclick="askAI()">💬 AIに質問する</button>
</div>

<script>
var LIFF_ID="{liff_faq_id}";
var curGenre='';
var searchTimer=null;

liff.init({{liffId:LIFF_ID}}).catch(function(){{}});

// ジャンルボタン追加
var GENRES={genres_json};
var bar=document.getElementById('genreList');
GENRES.forEach(function(g){{
  var b=document.createElement('button');
  b.className='g-btn'; b.textContent=g; b.dataset.g=g;
  b.onclick=function(){{setGenre(b,g);}};
  bar.appendChild(b);
}});

function setGenre(el,g){{
  curGenre=g;
  document.querySelectorAll('.g-btn').forEach(function(b){{b.classList.remove('active');}});
  el.classList.add('active');
  // スクロールして見えるように
  el.scrollIntoView({{inline:'center',behavior:'smooth',block:'nearest'}});
  load();
}}

function onSearchInput(){{
  clearTimeout(searchTimer);
  searchTimer=setTimeout(load,400);
}}

function load(){{
  document.getElementById('loader').style.display='block';
  document.getElementById('list').innerHTML='';
  document.getElementById('countBar').textContent='';
  var q=encodeURIComponent(document.getElementById('q').value.trim());
  var g=encodeURIComponent(curGenre);
  fetch('/liff/api/faq?q='+q+'&genre='+g)
    .then(function(r){{return r.json();}})
    .then(function(d){{
      document.getElementById('loader').style.display='none';
      var items=d.items||[];
      document.getElementById('countBar').textContent=items.length+'件';
      if(!items.length){{
        document.getElementById('list').innerHTML='<div class="empty">見つかりませんでした<br><span style="font-size:16px;color:#bbb">キーワードを変えてみてください</span></div>';
        return;
      }}
      var html='';
      items.forEach(function(it,i){{
        html+='<div class="faq-item" id="fi'+i+'">'
            +'<div class="faq-q" onclick="toggle('+i+')">'
            +'<span class="genre-badge">'+esc(it.genre)+'</span>'
            +esc(it.question)+'</div>'
            +'<div class="faq-a">'+esc(it.answer)+'</div>'
            +'</div>';
      }});
      document.getElementById('list').innerHTML=html;
    }})
    .catch(function(){{
      document.getElementById('loader').style.display='none';
      document.getElementById('list').innerHTML='<div class="empty">読み込みに失敗しました</div>';
    }});
}}

function toggle(i){{
  var el=document.getElementById('fi'+i);
  var wasOpen=el.classList.contains('open');
  // 同じジャンル内のすべてを閉じる（任意: コメントアウトで複数展開可）
  // document.querySelectorAll('.faq-item.open').forEach(function(e){{e.classList.remove('open');}});
  if(wasOpen){{el.classList.remove('open');}}
  else{{
    el.classList.add('open');
    // 少しスクロールして見やすく
    setTimeout(function(){{el.scrollIntoView({{behavior:'smooth',block:'nearest'}});}},200);
  }}
}}

function esc(s){{
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\n/g,'<br>');
}}

function askAI(){{
  try{{
    liff.sendMessages([{{type:'text',text:'質問があります'}}])
      .then(function(){{liff.closeWindow();}})
      .catch(function(){{liff.closeWindow();}});
  }}catch(e){{liff.closeWindow();}}
}}

load();
</script>
</body></html>
"""


@app.route("/liff/faq", methods=["GET"])
def liff_faq():
    import json as _json
    genres_json = _json.dumps(_LIFF_FAQ_GENRES, ensure_ascii=False)
    html = _LIFF_FAQ_HTML.format(retro_css=_RETRO_CSS, liff_faq_id=LIFF_FAQ_ID, genres_json=genres_json)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/liff/api/faq/genres", methods=["GET"])
def liff_api_faq_genres():
    try:
        result = get_supabase().table("faq").select("genre").execute()
        db_genres = {r["genre"] for r in (result.data or []) if r.get("genre")}
        # 固定順序を優先し、DB にしかないジャンルを末尾に追加
        genres = _LIFF_FAQ_GENRES + sorted(db_genres - set(_LIFF_FAQ_GENRES))
        return jsonify({"genres": genres})
    except Exception as e:
        logging.exception("liff_api_faq_genres error: %s", e)
        return jsonify({"error": "server error"}), 500


@app.route("/liff/api/faq", methods=["GET"])
def liff_api_faq():
    genre = request.args.get("genre", "").strip()
    q     = request.args.get("q", "").strip()
    try:
        query = get_supabase().table("faq").select("genre, question, answer")
        if genre:
            query = query.eq("genre", genre)
        if q:
            # 質問・回答の両方をキーワード検索（OR）
            query = query.or_(f"question.ilike.%{q}%,answer.ilike.%{q}%")
        result = query.order("genre").limit(100).execute()
        return jsonify({"items": result.data or []})
    except Exception as e:
        logging.exception("liff_api_faq error: %s", e)
        return jsonify({"error": "server error"}), 500


# ── ② LIFF 地図・病院・お店検索 ──────────────────────

_LIFF_SEARCH_HTML = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=yes">
<title>お店・病院を探す</title>
<script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
<style>
{retro_css}
.tabs{{display:flex;background:var(--card-bg);border-bottom:3px solid var(--border)}}
.tab{{flex:1;padding:14px;text-align:center;font-size:18px;cursor:pointer;color:var(--sub-text);font-weight:bold}}
.tab.active{{color:var(--header-bg);border-bottom:3px solid var(--header-bg);margin-bottom:-3px}}
.search-bar{{background:var(--card-bg);padding:12px 16px;display:flex;gap:8px;border-bottom:2px solid var(--border)}}
.search-bar input{{flex:1;font-size:18px;padding:10px;border:2px solid var(--border);border-radius:8px;background:#fffff0;color:var(--text)}}
.search-bar button{{font-size:16px;padding:10px 16px;background:var(--btn-bg);color:var(--btn-text);border:none;border-radius:8px;cursor:pointer;font-weight:bold;box-shadow:2px 3px 0 #5C1010}}
.search-bar button:active{{transform:translateY(2px);box-shadow:none}}
.wrap{{padding:12px 16px;max-width:600px;margin:0 auto}}
.card-name{{font-size:20px;font-weight:bold;color:var(--header-bg);margin-bottom:6px}}
.card-info{{font-size:16px;color:var(--sub-text);margin-bottom:4px}}
.card-info span{{color:var(--border);font-size:14px;margin-right:6px}}
.card-btns{{display:flex;gap:8px;margin-top:10px}}
.cbtn{{flex:1;padding:10px;font-size:16px;border-radius:8px;border:none;cursor:pointer;text-align:center;text-decoration:none;display:block;font-weight:bold}}
.cbtn-map{{background:var(--bg);color:var(--header-bg);border:2px solid var(--border)}}
.cbtn-call{{background:#fffff0;color:#1565c0;border:2px solid #1565c0}}
.loader{{text-align:center;padding:48px;color:var(--sub-text);font-size:18px}}
.empty{{text-align:center;padding:40px;color:var(--sub-text);font-size:18px}}
.hospital-links{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px}}
.hlink{{background:var(--card-bg);border:2px solid var(--border);border-radius:10px;padding:14px;text-align:center;text-decoration:none;color:var(--header-bg);font-size:17px;font-weight:bold;display:block;box-shadow:2px 3px 0 var(--border)}}
.hlink:active{{transform:translateY(2px);box-shadow:none}}
.stars{{color:#f57c00;font-size:17px}}
</style>
</head>
<body>
<div class="retro-header"><h1>🔍 お店・病院を探す</h1></div>
<div class="tabs">
  <div class="tab active" onclick="setTab('restaurant')" id="tab-restaurant">🍽️ お店</div>
  <div class="tab"        onclick="setTab('hospital')"   id="tab-hospital">🏥 病院・施設</div>
</div>
<div class="search-bar" id="search-bar">
  <input id="q" type="text" placeholder="ジャンル・エリアで検索" onkeydown="if(event.key==='Enter')load()">
  <button onclick="load()">検索</button>
</div>
<div class="wrap">
  <div id="loader" class="loader" style="display:none"></div>
  <div id="list"></div>
</div>
<script>
var LIFF_ID="{liff_search_id}"; var curTab='restaurant';
liff.init({{liffId:LIFF_ID}}).catch(function(){{}});
function setTab(t){{
  curTab=t;
  ['restaurant','hospital'].forEach(function(x){{
    document.getElementById('tab-'+x).classList.toggle('active',x===t);
  }});
  document.getElementById('q').value='';
  if(t==='hospital'){{showHospitalLinks();}}else{{load();}}
}}
function load(){{
  document.getElementById('loader').style.display='block';
  document.getElementById('list').innerHTML='';
  var q=encodeURIComponent(document.getElementById('q').value.trim());
  fetch('/liff/api/spots?type='+curTab+'&q='+q).then(function(r){{return r.json();}}).then(function(d){{
    document.getElementById('loader').style.display='none';
    var items=d.items||[];
    if(!items.length){{document.getElementById('list').innerHTML='<div class="empty">見つかりませんでした</div>';return;}}
    var html='';
    items.forEach(function(it){{
      var stars='';
      if(it.rating){{for(var i=0;i<Math.round(it.rating);i++)stars+='★';}}
      var mapQ=encodeURIComponent((it.name||'')+'　'+(it.address||''));
      html+='<div class="retro-card">'
          +'<div class="card-name">'+esc(it.name)+'</div>'
          +(it.genre?'<div class="card-info"><span>ジャンル</span>'+esc(it.genre)+'</div>':'')
          +(it.area?'<div class="card-info"><span>エリア</span>'+esc(it.area)+'</div>':'')
          +(it.address?'<div class="card-info"><span>住所</span>'+esc(it.address)+'</div>':'')
          +(stars?'<div class="stars">'+stars+'</div>':'')
          +'<div class="card-btns">'
          +'<a class="cbtn cbtn-map" href="https://maps.google.com/?q='+mapQ+'" target="_blank">🗺️ 地図</a>'
          +(it.phone?'<a class="cbtn cbtn-call" href="tel:'+esc(it.phone)+'">📞 電話</a>':'')
          +'</div></div>';
    }});
    document.getElementById('list').innerHTML=html;
  }}).catch(function(){{document.getElementById('loader').style.display='none';}});
}}
function showHospitalLinks(){{
  var area="{area}";
  var links=[
    ['内科・かかりつけ医','病院 内科 '+area],['整形外科','整形外科 '+area],
    ['歯科','歯科 '+area],['皮膚科','皮膚科 '+area],
    ['眼科','眼科 '+area],['救急・夜間','救急病院 '+area],
    ['市役所','市役所 '+area],['図書館','図書館 '+area],
  ];
  var html='<div class="retro-card"><div class="card-name">近くの病院・施設をGoogleマップで探す</div><div class="hospital-links">';
  links.forEach(function(l){{
    html+='<a class="hlink" href="https://maps.google.com/?q='+encodeURIComponent(l[1])+'" target="_blank">'+l[0]+'</a>';
  }});
  html+='</div></div>';
  document.getElementById('loader').style.display='none';
  document.getElementById('list').innerHTML=html;
}}
function esc(s){{return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}}
load();
</script>
</body></html>
"""


@app.route("/liff/search", methods=["GET"])
def liff_search():
    area = _AREA_KEYWORDS[0] if _AREA_KEYWORDS else ""
    html = _LIFF_SEARCH_HTML.format(retro_css=_RETRO_CSS, liff_search_id=LIFF_SEARCH_ID, area=area)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/liff/api/spots", methods=["GET"])
def liff_api_spots():
    spot_type = request.args.get("type", "restaurant")
    q = request.args.get("q", "").strip()
    try:
        if spot_type == "restaurant":
            query = get_supabase().table("restaurants").select(
                "name, genre, area, address, phone, rating"
            )
            if q:
                query = query.or_(f"name.ilike.%{q}%,genre.ilike.%{q}%,area.ilike.%{q}%")
            result = query.order("rating", desc=True).limit(20).execute()
            return jsonify({"items": result.data or []})
        return jsonify({"items": []})
    except Exception as e:
        logging.exception("liff_api_spots error: %s", e)
        return jsonify({"error": "server error"}), 500


# ── ④ 地図・周辺検索（LIFF） ────────────────────────

# HTML は templates/liff_map.html に移行済み
_LIFF_MAP_HTML_DELETED = """\
<!DOCTYPE html>
<html lang="ja-DELETED">
<head>
<meta charset="UTF-8">
<title>deleted</title>
<style>
{retro_css}
.loc-bar{{display:flex;align-items:center;gap:8px;padding:12px 16px;background:var(--card-bg);font-size:16px;border-bottom:2px solid var(--border);color:var(--sub-text)}}
.loc-bar.success{{color:#2e7d32}}
.loc-bar.error{{color:#c62828;background:#fff8f8}}
.cat-wrap{{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:14px 16px;background:var(--card-bg);border-bottom:3px solid var(--border)}}
.cat-btn{{padding:14px 8px;font-size:17px;font-weight:bold;border:3px solid var(--border);border-radius:10px;background:var(--bg);color:var(--text);cursor:pointer;text-align:center;line-height:1.4}}
.cat-btn:active,.cat-btn.active{{background:var(--header-bg);color:var(--header-text);border-color:var(--header-bg)}}
.cat-btn.full{{grid-column:1/-1}}
.wrap{{padding:12px 16px;max-width:600px;margin:0 auto}}
.card-name{{font-size:20px;font-weight:bold;color:var(--header-bg);margin-bottom:8px}}
.card-row{{font-size:16px;color:var(--sub-text);margin-bottom:4px}}
.card-row.addr{{font-size:15px;color:var(--sub-text)}}
.open{{color:#2e7d32;font-weight:bold;background:#e8f5e9;padding:2px 8px;border-radius:6px;font-size:14px}}
.closed{{color:#c62828;font-weight:bold;background:#fff3f3;padding:2px 8px;border-radius:6px;font-size:14px}}
.card-btns{{display:flex;gap:10px;margin-top:12px}}
.cbtn{{flex:1;padding:12px 8px;font-size:16px;font-weight:bold;border-radius:8px;border:none;cursor:pointer;text-align:center;text-decoration:none;display:block}}
.cbtn-map{{background:var(--bg);color:var(--header-bg);border:2px solid var(--border)}}
.cbtn-call{{background:#fffff0;color:#1565c0;border:2px solid #1565c0}}
.loader{{text-align:center;padding:48px;color:var(--sub-text);font-size:18px}}
.empty{{text-align:center;padding:40px;color:var(--sub-text);font-size:18px}}
.note{{background:var(--card-bg);border:2px dashed var(--border);border-radius:10px;padding:14px 16px;font-size:16px;color:var(--sub-text);margin:14px 0}}
</style>
</head>
<body>
<div id="map-hidden" style="width:1px;height:1px;visibility:hidden;position:absolute"></div>
<div class="retro-header"><h1>📍 地図・周辺検索</h1></div>
<div id="loc-bar" class="loc-bar">⏳ 現在地を取得中...</div>
<div class="cat-wrap">
  <button class="cat-btn" id="btn-hospital"    onclick="doSearch('hospital',   this)">🏥 病院・クリニック</button>
  <button class="cat-btn" id="btn-pharmacy"    onclick="doSearch('pharmacy',   this)">💊 薬局</button>
  <button class="cat-btn" id="btn-restaurant"  onclick="doSearch('restaurant', this)">🍽️ 飲食店</button>
  <button class="cat-btn" id="btn-supermarket" onclick="doSearch('supermarket',this)">🏪 スーパー</button>
  <button class="cat-btn full" id="btn-public" onclick="doSearch('public',     this)">🏛️ 公共施設（市役所・図書館など）</button>
</div>
<div class="wrap">
  <div id="loader" class="loader" style="display:none">🔍 検索中...</div>
  <div id="list"></div>
</div>

<script>
var LIFF_ID = "{liff_map_id}";
var GMAPS_KEY = "{google_maps_api_key}";
var userLat = null, userLng = null;
var placesService = null, mapObj = null;
var mapsReady = false;
var pendingCategory = null;

liff.init({{liffId: LIFF_ID}}).catch(function(){{}});

var locBar = document.getElementById('loc-bar');

if (!navigator.geolocation) {{
  locBar.className = 'loc-bar error';
  locBar.textContent = '⚠️ このブラウザは位置情報に対応していません';
}} else {{
  navigator.geolocation.getCurrentPosition(
    function(pos) {{
      userLat = pos.coords.latitude;
      userLng  = pos.coords.longitude;
      locBar.className = 'loc-bar success';
      locBar.textContent = '📍 現在地を取得しました ✓';
      loadGoogleMaps();
    }},
    function(err) {{
      locBar.className = 'loc-bar error';
      locBar.textContent = '⚠️ 現在地を取得できませんでした（設定から位置情報を許可してください）';
    }},
    {{timeout: 12000, enableHighAccuracy: true}}
  );
}}

function loadGoogleMaps() {{
  if (!GMAPS_KEY) {{
    locBar.className = 'loc-bar error';
    locBar.textContent = '⚠️ Google Maps APIキーが設定されていません';
    return;
  }}
  var s = document.createElement('script');
  s.src = 'https://maps.googleapis.com/maps/api/js?key=' + GMAPS_KEY
        + '&libraries=places,geometry&language=ja&callback=onMapsLoaded';
  s.async = true; s.defer = true;
  document.head.appendChild(s);
}}

function onMapsLoaded() {{
  var center = new google.maps.LatLng(userLat, userLng);
  mapObj = new google.maps.Map(document.getElementById('map-hidden'), {{center: center, zoom: 15}});
  placesService = new google.maps.places.PlacesService(mapObj);
  mapsReady = true;
  if (pendingCategory) {{ doSearch(pendingCategory, null); pendingCategory = null; }}
}}

var CAT_CFG = {{
  hospital:    {{type: 'hospital',          keyword: '病院 クリニック 内科'}},
  pharmacy:    {{type: 'pharmacy',          keyword: '薬局 ドラッグストア'}},
  restaurant:  {{type: 'restaurant',        keyword: null}},
  supermarket: {{type: 'supermarket',       keyword: 'スーパー 食料品 イオン'}},
  public:      {{type: null,               keyword: '市役所 区役所 図書館 郵便局 公共施設'}}
}};

function doSearch(cat, btn) {{
  document.querySelectorAll('.cat-btn').forEach(function(b) {{ b.classList.remove('active'); }});
  if (btn) btn.classList.add('active');

  if (!mapsReady) {{
    pendingCategory = cat;
    document.getElementById('loader').style.display = 'block';
    document.getElementById('list').innerHTML = '';
    return;
  }}
  document.getElementById('loader').style.display = 'block';
  document.getElementById('list').innerHTML = '';

  var cfg = CAT_CFG[cat];
  var center = new google.maps.LatLng(userLat, userLng);
  var req = {{location: center, radius: 1500, language: 'ja'}};
  if (cfg.type)    req.type    = cfg.type;
  if (cfg.keyword) req.keyword = cfg.keyword;

  placesService.nearbySearch(req, function(results, status) {{
    document.getElementById('loader').style.display = 'none';
    var PS = google.maps.places.PlacesServiceStatus;
    if (status !== PS.OK && status !== PS.ZERO_RESULTS) {{
      document.getElementById('list').innerHTML = '<div class="note">⚠️ 検索エラーが発生しました（' + status + '）</div>';
      return;
    }}
    if (!results || !results.length) {{
      document.getElementById('list').innerHTML = '<div class="empty">近くに見つかりませんでした</div>';
      return;
    }}
    results = results.slice(0, 8);
    var html = '';
    results.forEach(function(place) {{
      var dist = Math.round(
        google.maps.geometry.spherical.computeDistanceBetween(center, place.geometry.location)
      );
      var distStr = dist >= 1000 ? (dist / 1000).toFixed(1) + 'km' : dist + 'm';
      var openStr = '';
      if (place.opening_hours) {{
        openStr = place.opening_hours.open_now
          ? '<span class="open">営業中</span>'
          : '<span class="closed">営業時間外</span>';
      }}
      var mapsUrl = 'https://maps.google.com/?place_id=' + encodeURIComponent(place.place_id);
      html += '<div class="retro-card">'
        + '<div class="card-name">' + esc(place.name) + '</div>'
        + '<div class="card-row">📍 ' + distStr + (openStr ? '　' + openStr : '') + '</div>'
        + (place.vicinity ? '<div class="card-row addr">🏠 ' + esc(place.vicinity) + '</div>' : '')
        + '<div class="card-btns">'
        + '<a class="cbtn cbtn-map" href="' + mapsUrl + '" target="_blank">🗺️ 地図で見る</a>'
        + '<button class="cbtn cbtn-call" data-pid="' + esc(place.place_id) + '" onclick="callPlace(this)">📞 電話する</button>'
        + '</div></div>';
    }});
    document.getElementById('list').innerHTML = html;
  }});
}}

function callPlace(btn) {{
  if (btn.dataset.phone) {{
    location.href = 'tel:' + btn.dataset.phone;
    return;
  }}
  var pid = btn.dataset.pid;
  btn.textContent = '取得中...';
  placesService.getDetails(
    {{placeId: pid, fields: ['formatted_phone_number']}},
    function(detail, st) {{
      if (st === google.maps.places.PlacesServiceStatus.OK && detail.formatted_phone_number) {{
        btn.dataset.phone = detail.formatted_phone_number;
        btn.textContent = '📞 ' + detail.formatted_phone_number;
        location.href = 'tel:' + detail.formatted_phone_number;
      }} else {{
        btn.textContent = '電話番号なし';
        btn.disabled = true;
      }}
    }}
  );
}}

function esc(s) {{
  return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}
</script>
</body></html>
"""


@app.route("/liff/map", methods=["GET"])
def liff_map():
    return render_template(
        "liff_map.html",
        liff_id=LIFF_ID,
        openweather_key=OPENWEATHER_API_KEY,
    )


# ── ⑤ スケジュール（LIFF） ───────────────────────────

_LIFF_SCHEDULE_HTML = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=yes,maximum-scale=2">
<title>お約束帳</title>
<script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
<style>
:root {{
  --bg:      #F5E6A3;
  --text:     #4A2C0A;
  --sunday:  #C0392B;
  --btn-bg:  #8B1A1A;
  --btn-text:#FFD700;
  --green:   #27AE60;
  --sub:     #6B4010;
  --line:    #C8A060;
  --card:    #FFF8DC;
  --border:  #8B6914;
  --dim:     #AAA;
  --today-bg:#FFF8DC;
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: var(--bg);
  color: var(--text);
  font-family: 'Hiragino Mincho ProN','Yu Mincho','Noto Serif JP',serif;
  font-size: 18px;
  min-height: 100vh;
  padding-bottom: 120px;
}}
/* ヘッダー */
.app-header {{
  background: #8B1A1A;
  color: #FFD700;
  padding: 14px 50px;
  text-align: center;
  font-size: 22px;
  font-weight: bold;
  letter-spacing: 0.1em;
  border-bottom: 4px solid #5C1010;
  position: sticky;
  top: 0;
  z-index: 100;
}}
.back-btn {{
  position: absolute;
  left: 10px;
  top: 50%;
  transform: translateY(-50%);
  background: none;
  border: none;
  color: #FFD700;
  font-size: 26px;
  cursor: pointer;
  display: none;
  padding: 4px 10px;
  line-height: 1;
}}
/* 今日のカード */
.today-card {{
  margin: 16px;
  padding: 16px 18px;
  background: var(--today-bg);
  border: 2px solid var(--line);
  border-radius: 8px;
  box-shadow: 2px 3px 0 var(--line);
}}
.today-label {{
  font-size: 14px;
  color: var(--sub);
  font-weight: bold;
  margin-bottom: 6px;
}}
.today-date {{
  font-size: 22px;
  font-weight: bold;
  color: var(--text);
  margin-bottom: 10px;
}}
.today-msg {{
  font-size: 18px;
  line-height: 1.8;
  color: var(--text);
}}
.today-event {{
  display: flex;
  align-items: center;
  padding: 8px 0;
  border-bottom: 1px dashed var(--line);
  font-size: 20px;
}}
.today-event:last-child {{ border-bottom: none; }}
.today-event-text {{ flex: 1; }}
/* セクション */
.section-title {{
  padding: 10px 16px 6px;
  font-size: 14px;
  font-weight: bold;
  color: var(--sub);
  border-bottom: 2px solid var(--line);
  background: var(--bg);
  letter-spacing: 0.1em;
}}
.empty-future {{
  text-align: center;
  padding: 30px 20px;
  color: var(--dim);
  font-size: 18px;
  line-height: 2;
}}
/* 予定リスト */
.event-item {{
  padding: 14px 16px;
  border-bottom: 1px dashed var(--line);
  background: var(--card-bg);
  display: flex;
  align-items: flex-start;
  gap: 12px;
}}
.event-item.past {{
  background: var(--bg);
  opacity: 0.6;
}}
.event-left {{ flex: 1; min-width: 0; }}
.event-date {{
  font-size: 24px;
  font-weight: bold;
  color: var(--text);
  line-height: 1.2;
}}
.event-date.sun {{ color: var(--sunday); }}
.event-content {{
  font-size: 20px;
  line-height: 1.5;
  margin-top: 4px;
  word-break: break-all;
}}
.del-btn {{
  flex-shrink: 0;
  width: 44px;
  height: 44px;
  border-radius: 50%;
  background: var(--card-bg);
  color: var(--sub);
  font-size: 20px;
  border: 2px solid var(--border);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-top: 4px;
}}
.del-btn:active {{ background: var(--btn-bg); color: var(--btn-text); border-color: var(--btn-bg); }}
/* 過去の予定のトグル */
.past-toggle {{
  text-align: center;
  padding: 12px;
  color: var(--dim);
  font-size: 15px;
  cursor: pointer;
  border-bottom: 1px dashed var(--line);
}}
/* ボタンエリア */
.btn-area {{
  margin: 20px 16px 8px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}}
.btn-add {{
  width: 100%;
  padding: 20px;
  font-size: 20px;
  font-weight: bold;
  background: var(--btn-bg);
  color: var(--btn-text);
  border: none;
  border-radius: 10px;
  cursor: pointer;
  box-shadow: 0 4px 0 #5C1010;
  letter-spacing: 0.05em;
}}
.btn-add:active {{ transform: translateY(3px); box-shadow: none; }}
.btn-migrate {{
  width: 100%;
  padding: 16px;
  font-size: 17px;
  font-weight: bold;
  background: var(--btn-bg);
  color: var(--btn-text);
  border: none;
  border-radius: 10px;
  cursor: pointer;
  box-shadow: 0 4px 0 #5C1010;
  letter-spacing: 0.05em;
}}
.btn-migrate:active {{ transform: translateY(3px); box-shadow: none; }}
/* 追加画面 */
#view-add {{ display: none; padding: 20px 16px; }}
.field-label {{
  font-size: 16px;
  font-weight: bold;
  color: var(--sub);
  margin-bottom: 8px;
  margin-top: 20px;
}}
.field-label:first-child {{ margin-top: 0; }}
.date-input {{
  width: 100%;
  padding: 16px;
  font-size: 22px;
  border: 2px solid var(--line);
  border-radius: 8px;
  background: var(--card-bg);
  color: var(--text);
  font-family: inherit;
}}
.content-input {{
  width: 100%;
  padding: 14px;
  font-size: 20px;
  border: 2px solid var(--line);
  border-radius: 8px;
  background: var(--card-bg);
  color: var(--text);
  font-family: inherit;
  min-height: 120px;
  resize: vertical;
  line-height: 1.6;
}}
.date-input:focus, .content-input:focus {{ outline: none; border-color: var(--sub); }}
.btn-save {{
  display: block;
  width: 100%;
  margin-top: 28px;
  padding: 20px;
  font-size: 22px;
  font-weight: bold;
  background: var(--btn-bg);
  color: var(--btn-text);
  border: none;
  border-radius: 10px;
  cursor: pointer;
  box-shadow: 0 4px 0 #5C1010;
  letter-spacing: 0.05em;
}}
.btn-save:active {{ transform: translateY(3px); box-shadow: none; }}
</style>
</head>
<body>

<div class="app-header">
  <button class="back-btn" id="back-btn" onclick="showList()">&#9664;</button>
  <span id="header-title">&#128197; お約束帳</span>
</div>

<!-- リスト画面 -->
<div id="view-list">
  <!-- 今日のカード -->
  <div class="today-card">
    <div class="today-label">&#9728;&#65038; 今日</div>
    <div class="today-date" id="today-date"></div>
    <div id="today-content"></div>
  </div>
  <!-- これからの予定 -->
  <div class="section-title">&#9650; これからのお約束</div>
  <div id="future-list"></div>
  <!-- 過去の予定 -->
  <div id="past-section" style="display:none">
    <div class="section-title" style="color:var(--dim)">&#9660; 過去のお約束</div>
    <div id="past-list"></div>
  </div>
  <!-- ボタン -->
  <div class="btn-area">
    <button class="btn-add" onclick="showAdd()">&#65291; 新しいお約束を追加する</button>
    <button class="btn-migrate" onclick="doMigration()">&#128230; 機種変更のお引越し準備</button>
  </div>
</div>

<!-- 追加画面 -->
<div id="view-add">
  <div class="field-label">&#128197; 日付</div>
  <input class="date-input" type="date" id="date-input">
  <div class="field-label">&#128221; 内容</div>
  <textarea class="content-input" id="content-input" placeholder="例：病院（定期検診）、孫の運動会..."></textarea>
  <button class="btn-save" onclick="saveEvent()">保存する</button>
</div>

<script>
var LIFF_ID  = "{liff_schedule_id}";
var STOR_KEY = "oyakusoku_v1";
var liffOK   = false;
var showPast = false;

function getEvents(){{ try{{ return JSON.parse(localStorage.getItem(STOR_KEY)||"[]"); }}catch(e){{ return []; }} }}
function setEvents(a){{ localStorage.setItem(STOR_KEY, JSON.stringify(a)); }}
function genId(){{ return Date.now().toString(36)+Math.random().toString(36).slice(2,6); }}

function todayStr(){{
  var d=new Date();
  return d.getFullYear()+"-"+pad(d.getMonth()+1)+"-"+pad(d.getDate());
}}
function pad(n){{ return n<10?"0"+n:""+n; }}

function b64enc(obj){{
  var j=JSON.stringify(obj);
  return btoa(encodeURIComponent(j).replace(/%([0-9A-F]{{2}})/g,function(_,p){{
    return String.fromCharCode(parseInt(p,16));
  }}));
}}
function b64dec(s){{
  return JSON.parse(decodeURIComponent(Array.prototype.map.call(atob(s),function(c){{
    return '%'+('00'+c.charCodeAt(0).toString(16)).slice(-2);
  }}).join('')));
}}

function tryRestore(){{
  var dp=new URLSearchParams(location.search).get('data');
  if(!dp)return;
  try{{
    var arr=b64dec(dp);
    if(confirm(arr.length+"件のお約束が見つかりました。\nこの端末に復元しますか？")){{
      setEvents(arr);
      alert("復元しました！（"+arr.length+"件）");
    }}
  }}catch(e){{ console.log("restore error",e); }}
}}

liff.init({{liffId:LIFF_ID}}).then(function(){{
  liffOK=true; tryRestore(); renderAll();
}}).catch(function(){{ tryRestore(); renderAll(); }});

var WDAYS=["日","月","火","水","木","金","土"];
function fmtDateStr(ds){{
  var d=new Date(ds+"T00:00:00");
  return (d.getMonth()+1)+"月"+d.getDate()+"日（"+WDAYS[d.getDay()]+"）";
}}
function isSun(ds){{
  return new Date(ds+"T00:00:00").getDay()===0;
}}
function fmtTodayFull(){{
  var d=new Date();
  return d.getFullYear()+"年"+(d.getMonth()+1)+"月"+d.getDate()+"日（"+WDAYS[d.getDay()]+"）";
}}

function renderAll(){{
  // 今日のカード
  document.getElementById("today-date").textContent=fmtTodayFull();
  var td=todayStr();
  var evs=getEvents();
  var todayEvs=evs.filter(function(e){{return e.date===td;}});
  var tc=document.getElementById("today-content");
  if(todayEvs.length){{
    var h="";
    todayEvs.forEach(function(e){{
      h+='<div class="today-event">'
        +'<span class="today-event-text">&#10022; '+esc(e.content)+'</span>'
        +'</div>';
    }});
    tc.innerHTML=h;
  }}else{{
    tc.innerHTML='<div class="today-msg">今日のお約束はありません。<br>ゆっくりお過ごしください &#128578;</div>';
  }}
  // 振り分け
  var future=evs.filter(function(e){{return e.date>=td&&e.date!==td;}});
  var past=evs.filter(function(e){{return e.date<td;}});
  future.sort(function(a,b){{return a.date<b.date?-1:1;}});
  past.sort(function(a,b){{return a.date>b.date?-1:1;}});
  // 未来
  var fl=document.getElementById("future-list");
  if(!future.length){{
    fl.innerHTML='<div class="empty-future">これからのお約束はありません。<br>下のボタンから追加できます。</div>';
  }}else{{
    fl.innerHTML=future.map(function(e){{return eventHTML(e,false);}}).join('');
  }}
  // 過去
  var ps=document.getElementById("past-section");
  if(past.length){{
    ps.style.display="block";
    document.getElementById("past-list").innerHTML=past.map(function(e){{return eventHTML(e,true);}}).join('');
  }}else{{
    ps.style.display="none";
  }}
}}

function eventHTML(e,isPast){{
  var cls="event-item"+(isPast?" past":"");
  var dateCls="event-date"+(isSun(e.date)?" sun":"");
  return '<div class="'+cls+'">'
    +'<div class="event-left">'
    +'<div class="'+dateCls+'">'+fmtDateStr(e.date)+'</div>'
    +'<div class="event-content">'+esc(e.content)+'</div>'
    +'</div>'
    +'<button class="del-btn" onclick="delEvent(\''+e.id+'\')">&#10005;</button>'
    +'</div>';
}}

function delEvent(id){{
  if(!confirm("このお約束を消去しますか？"))return;
  setEvents(getEvents().filter(function(e){{return e.id!==id;}}));
  renderAll();
}}

function showList(){{
  document.getElementById("view-list").style.display="block";
  document.getElementById("view-add").style.display="none";
  document.getElementById("back-btn").style.display="none";
  document.getElementById("header-title").textContent="📅 お約束帳";
  renderAll();
}}

function showAdd(){{
  document.getElementById("view-list").style.display="none";
  document.getElementById("view-add").style.display="block";
  document.getElementById("back-btn").style.display="block";
  document.getElementById("header-title").textContent="新しいお約束";
  // デフォルト日付を明日に設定
  var tmr=new Date(); tmr.setDate(tmr.getDate()+1);
  document.getElementById("date-input").value=tmr.getFullYear()+"-"+pad(tmr.getMonth()+1)+"-"+pad(tmr.getDate());
  document.getElementById("content-input").value="";
  setTimeout(function(){{document.getElementById("content-input").focus();}},150);
}}

function saveEvent(){{
  var date=document.getElementById("date-input").value;
  var content=document.getElementById("content-input").value.trim();
  if(!date){{alert("日付を選んでください。");return;}}
  if(!content){{alert("内容を入力してください。");return;}}
  var evs=getEvents();
  evs.push({{id:genId(),date:date,content:content,ts:Date.now()}});
  setEvents(evs);
  showList();
}}

function doMigration(){{
  var evs=getEvents();
  if(!evs.length){{alert("まだお約束が登録されていません。");return;}}
  var enc=encodeURIComponent(b64enc(evs));
  var url="https://liff.line.me/"+LIFF_ID+"/schedule?data="+enc;
  var msg="📅 お約束帳のお引越し用リンクです。\n新しいスマホでこのリンクをタップするとお約束が戻ります。\n\n"+url;
  if(msg.length>4900){{
    alert("お約束が多すぎてリンクが長くなりすぎます。\n古いお約束をいくつか消去してから試してください。");
    return;
  }}
  if(liffOK&&liff.isInClient()){{
    liff.sendMessages([{{type:"text",text:msg}}])
      .then(function(){{alert("お引越し用メッセージをトークに送りました！\n新しいスマホでそのリンクをタップしてください。");}})
      .catch(function(){{copyMsg(msg);}});
  }}else{{copyMsg(msg);}}
}}
function copyMsg(msg){{
  if(navigator.clipboard){{
    navigator.clipboard.writeText(msg).then(function(){{
      alert("お引越し用リンクをコピーしました。\nLINEに貼り付けて自分に送ってください。");
    }});
  }}else{{alert("LINEアプリ内で開いてください。");}}
}}

function esc(s){{return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}}
</script>
</body>
</html>
"""


@app.route("/liff/schedule", methods=["GET"])
def liff_schedule():
    html = _LIFF_SCHEDULE_HTML.format(retro_css=_RETRO_CSS, liff_schedule_id=LIFF_SCHEDULE_ID)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/liff/api/schedule", methods=["GET"])
def liff_api_schedule_get():
    user_id = request.args.get("line_user_id", "").strip()
    year    = request.args.get("year", "").strip()
    month   = request.args.get("month", "").strip()
    if not user_id or not year or not month:
        return jsonify({"error": "missing params"}), 400
    try:
        import calendar as cal_mod
        y, m = int(year), int(month)
        last_day = cal_mod.monthrange(y, m)[1]
        start = f"{y:04d}-{m:02d}-01"
        end   = f"{y:04d}-{m:02d}-{last_day:02d}"
        result = (
            get_supabase().table("schedules")
            .select("id, date, content, created_at")
            .eq("line_user_id", user_id)
            .gte("date", start)
            .lte("date", end)
            .order("date")
            .order("created_at")
            .execute()
        )
        return jsonify({"schedules": result.data or []})
    except Exception as e:
        logging.exception("schedule get error: %s", e)
        return jsonify({"error": "server error"}), 500


@app.route("/liff/api/schedule", methods=["POST"])
def liff_api_schedule_post():
    data    = request.get_json(silent=True) or {}
    user_id = data.get("line_user_id", "").strip()
    date_s  = data.get("date", "").strip()
    content = data.get("content", "").strip()
    if not user_id or not date_s or not content:
        return jsonify({"error": "missing params"}), 400
    try:
        result = (
            get_supabase().table("schedules")
            .insert({"line_user_id": user_id, "date": date_s, "content": content})
            .execute()
        )
        return jsonify(result.data[0] if result.data else {"error": "insert failed"})
    except Exception as e:
        logging.exception("schedule post error: %s", e)
        return jsonify({"error": "server error"}), 500


@app.route("/liff/api/schedule/<schedule_id>", methods=["DELETE"])
def liff_api_schedule_delete(schedule_id):
    try:
        get_supabase().table("schedules").delete().eq("id", schedule_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        logging.exception("schedule delete error: %s", e)
        return jsonify({"error": "server error"}), 500


# ── ⑥ メモ帳（LIFF） ────────────────────────────────

_LIFF_MEMO_HTML = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=yes,maximum-scale=2">
<title>覚え書き</title>
<!-- 1. vConsole: 実機デバッグ用（問題解決後に削除） -->
<script src="https://unpkg.com/vconsole@latest/dist/vconsole.min.js"></script>
<!-- 2. LIFF SDK -->
<script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: #F5E6A3;
  color: #4A2C0A;
  font-family: 'Hiragino Mincho ProN','Yu Mincho','Noto Serif JP',serif;
  font-size: 18px;
  min-height: 100vh;
  padding-bottom: 120px;
}}
.app-header {{
  background: #8B1A1A;
  color: #FFD700;
  padding: 14px 55px;
  text-align: center;
  font-size: 22px;
  font-weight: bold;
  letter-spacing: 0.1em;
  border-bottom: 4px solid #5C1010;
  position: relative;
}}
.back-btn {{
  position: absolute;
  left: 10px; top: 50%;
  transform: translateY(-50%);
  background: none; border: none;
  color: #FFD700; font-size: 28px;
  cursor: pointer; padding: 6px 12px;
  display: none;
}}
.header-save {{
  position: absolute;
  right: 10px; top: 50%;
  transform: translateY(-50%);
  background: #FFD700; color: #8B1A1A;
  border: none; border-radius: 8px;
  font-size: 17px; font-weight: bold;
  padding: 8px 16px; cursor: pointer;
  display: none;
}}
.memo-item {{
  display: flex; align-items: center;
  padding: 18px;
  border-bottom: 2px dashed #C8A060;
  cursor: pointer; background: #FFF8DC;
}}
.item-body {{ flex: 1; min-width: 0; }}
.item-title {{
  font-size: 18px; font-weight: bold;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.item-date {{ font-size: 13px; color: #888; margin-top: 4px; }}
.item-arrow {{ color: #C8A060; font-size: 22px; padding-left: 10px; flex-shrink: 0; }}
.empty-msg {{
  text-align: center; padding: 60px 20px;
  color: #AAA; font-size: 18px; line-height: 2.4;
}}
.migration-box {{
  margin: 24px 16px 16px;
  padding: 16px;
  background: #FFF8DC;
  border: 2px solid #8B6914;
  border-radius: 8px;
  box-shadow: 2px 3px 0 #8B6914;
}}
.migration-title {{
  font-size: 15px; color: #6B4010;
  margin-bottom: 12px; text-align: center; font-weight: bold;
}}
.migration-btn {{
  display: block; width: 100%;
  padding: 18px; font-size: 18px; font-weight: bold;
  background: #8B1A1A; color: #FFD700;
  border: none; border-radius: 8px;
  cursor: pointer; box-shadow: 0 4px 0 #5C1010;
  letter-spacing: 0.05em;
}}
.fab-new {{
  position: fixed;
  bottom: 36px; right: 24px;
  width: 72px; height: 72px;
  border-radius: 50%;
  background: #8B1A1A; color: #FFD700;
  font-size: 42px; font-weight: bold;
  border: 4px solid #FFD700;
  cursor: pointer;
  box-shadow: 0 4px 14px rgba(0,0,0,0.3);
  display: flex; align-items: center; justify-content: center;
  z-index: 200;
  -webkit-tap-highlight-color: rgba(255,215,0,0.3);
  touch-action: manipulation;
}}
#view-edit {{ display: none; }}
.edit-date {{
  padding: 10px 16px; font-size: 13px; color: #888;
  background: #FFF8DC; border-bottom: 1px solid #C8A060;
}}
.notebook-wrap {{ padding: 8px 16px 0; background: #F5E6A3; }}
.notebook-textarea {{
  width: 100%; min-height: 45vh;
  font-size: 20px; line-height: 2em;
  padding: 0.2em 6px;
  border: none; outline: none; resize: none;
  background:
    repeating-linear-gradient(
      #F5E6A3,
      #F5E6A3 calc(2em - 1px),
      #C8A060 calc(2em - 1px),
      #C8A060 2em
    );
  font-family: inherit; color: #4A2C0A; word-break: break-all;
}}
.btn-row {{
  display: flex; gap: 12px;
  padding: 14px 16px;
  background: #F5E6A3;
  border-top: 2px solid #C8A060;
}}
.btn-save {{
  flex: 2; padding: 20px;
  font-size: 20px; font-weight: bold;
  background: #8B1A1A; color: #FFD700;
  border: none; border-radius: 8px;
  cursor: pointer; box-shadow: 0 4px 0 #5C1010;
  touch-action: manipulation;
}}
.btn-del {{
  flex: 1; padding: 20px;
  font-size: 18px; font-weight: bold;
  background: #F5E6A3; color: #6B4010;
  border: 2px solid #8B6914; border-radius: 8px;
  cursor: pointer; touch-action: manipulation;
}}
</style>
</head>
<body>

<div class="app-header">
  <button class="back-btn" id="back-btn">&#9664;</button>
  <span id="header-title">&#9998; &#35226;&#12360;&#26360;&#12365;</span>
  <button class="header-save" id="header-save">&#20445;&#23559;</button>
</div>

<div id="view-list">
  <div id="memo-list"></div>
  <div class="migration-box">
    <div class="migration-title">&#128230; &#27231;&#31278;&#22793;&#26356;&#12398;&#12392;&#12365;&#12398;&#12487;&#12540;&#12479;&#12362;&#24341;&#36234;&#12375;</div>
    <button class="migration-btn" id="btn-migration">&#12362;&#24341;&#36234;&#12375;&#12398;&#28310;&#20633;&#12434;&#12377;&#12427;</button>
  </div>
</div>
<button class="fab-new" id="fab-new">&#65291;</button>

<div id="view-edit">
  <div class="edit-date" id="edit-date"></div>
  <div class="notebook-wrap">
    <textarea class="notebook-textarea" id="memo-ta" placeholder="&#12371;&#12371;&#12395;&#12513;&#12514;&#12434;&#26360;&#12356;&#12390;&#12367;&#12384;&#12373;&#12356;..."></textarea>
  </div>
  <div class="btn-row">
    <button class="btn-save" id="btn-save">&#20445;&#23559;&#12377;&#12427;</button>
    <button class="btn-del" id="btn-del" style="display:none">&#28040;&#21435;</button>
  </div>
</div>

<script>
// 1. vConsole 初期化（実機デバッグ用）
var vc = new VConsole();
console.log('[memo] page loaded');

var LIFF_ID  = "{liff_memo_id}";
var STOR_KEY = "kakioki_v1";
var editId   = null;
var liffReady = false;

// --- データ操作 ---
function getMemos(){{
  try{{ return JSON.parse(localStorage.getItem(STOR_KEY) || "[]"); }}
  catch(e){{ console.error('[memo] getMemos error', e); return []; }}
}}
function setMemos(arr){{
  try{{ localStorage.setItem(STOR_KEY, JSON.stringify(arr)); }}
  catch(e){{ console.error('[memo] setMemos error', e); alert('保存に失敗しました: ' + e.message); }}
}}
function genId(){{ return Date.now().toString(36) + Math.random().toString(36).slice(2,6); }}
function esc(s){{ return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }}
function fmtDate(ts){{
  var d = new Date(ts);
  var w = ["\u65e5","\u6708","\u706b","\u6c34","\u6728","\u91d1","\u571f"][d.getDay()];
  return d.getFullYear()+"\u5e74"+(d.getMonth()+1)+"\u6708"+d.getDate()+"\u65e5\uff08"+w+"\uff09"
    +" "+d.getHours()+":"+(d.getMinutes()<10?"0":"")+d.getMinutes();
}}

// --- 画面表示 ---
function renderList(){{
  console.log('[memo] renderList');
  var ms = getMemos();
  var el = document.getElementById("memo-list");
  if(!el){{ console.error('[memo] memo-list not found'); return; }}
  if(!ms.length){{
    el.innerHTML = '<div class="empty-msg">\u307e\u3060\u30e1\u30e2\u304c\u3042\u308a\u307e\u305b\u3093\u3002<br>\u4e0b\u306e \uff0b \u304b\u3089\u66f8\u304d\u59cb\u3081\u307e\u3057\u3087\u3046\u3002</div>';
    return;
  }}
  var h = "";
  ms.forEach(function(m){{
    var t = (m.content||"").replace(/\n/g," ").substring(0,24) || "\uff08\u7a7a\u306e\u30e1\u30e2\uff09";
    h += '<div class="memo-item" data-id="'+m.id+'">'
       + '<div class="item-body">'
       + '<div class="item-title">'+esc(t)+'</div>'
       + '<div class="item-date">'+fmtDate(m.ts)+'</div>'
       + '</div><span class="item-arrow">&#9654;</span></div>';
  }});
  el.innerHTML = h;
  el.querySelectorAll('.memo-item').forEach(function(el){{
    el.addEventListener('click', function(){{ openMemo(this.dataset.id); }});
  }});
}}

function showList(){{
  console.log('[memo] showList');
  document.getElementById("view-list").style.display = "block";
  document.getElementById("view-edit").style.display = "none";
  document.getElementById("fab-new").style.display = "flex";
  document.getElementById("back-btn").style.display = "none";
  document.getElementById("header-save").style.display = "none";
  document.getElementById("header-title").textContent = "\u9d5b \u899a\u3048\u66f8\u304d";
  editId = null;
  renderList();
}}

function showEdit(title){{
  console.log('[memo] showEdit:', title);
  document.getElementById("view-list").style.display = "none";
  document.getElementById("view-edit").style.display = "block";
  document.getElementById("fab-new").style.display = "none";
  document.getElementById("back-btn").style.display = "block";
  document.getElementById("header-save").style.display = "block";
  document.getElementById("header-title").textContent = title;
}}

function newMemo(){{
  console.log('[memo] newMemo');
  editId = null;
  document.getElementById("memo-ta").value = "";
  document.getElementById("btn-del").style.display = "none";
  document.getElementById("edit-date").textContent = fmtDate(Date.now()) + " \uff08\u65b0\u898f\uff09";
  showEdit("\u65b0\u3057\u3044\u30e1\u30e2");
}}

function openMemo(id){{
  console.log('[memo] openMemo:', id);
  var m = getMemos().find(function(x){{ return x.id === id; }});
  if(!m){{ console.error('[memo] memo not found:', id); return; }}
  editId = id;
  document.getElementById("memo-ta").value = m.content || "";
  document.getElementById("btn-del").style.display = "inline-block";
  document.getElementById("edit-date").textContent = fmtDate(m.ts);
  showEdit("\u30e1\u30e2\u3092\u898b\u308b\u30fb\u76f4\u3059");
}}

function saveMemo(){{
  console.log('[memo] saveMemo');
  var c = document.getElementById("memo-ta").value.trim();
  if(!c){{ alert("\u4f55\u304b\u66f8\u3044\u3066\u304b\u3089\u4fdd\u5b58\u3057\u3066\u304f\u3060\u3055\u3044\u3002"); return; }}
  var ms = getMemos();
  if(editId){{
    ms = ms.map(function(m){{ return m.id===editId ? {{id:m.id, content:c, ts:Date.now()}} : m; }});
  }} else {{
    ms.unshift({{id:genId(), content:c, ts:Date.now()}});
  }}
  setMemos(ms);
  console.log('[memo] saved, total:', ms.length);
  showList();
}}

function deleteMemo(){{
  if(!editId) return;
  if(!confirm("\u3053\u306e\u30e1\u30e2\u3092\u6d88\u53bb\u3057\u307e\u3059\u304b\uff1f")) return;
  setMemos(getMemos().filter(function(m){{ return m.id !== editId; }}));
  showList();
}}

// --- 移行（バックアップ）---
function b64enc(obj){{
  var j = JSON.stringify(obj);
  return btoa(encodeURIComponent(j).replace(/%([0-9A-F]{{2}})/g, function(_,p){{
    return String.fromCharCode(parseInt(p,16));
  }}));
}}

function doMigration(){{
  var ms = getMemos();
  if(!ms.length){{ alert("\u307e\u3060\u30e1\u30e2\u304c\u3042\u308a\u307e\u305b\u3093\u3002"); return; }}
  var enc = encodeURIComponent(b64enc(ms));
  var url = "https://liff.line.me/" + LIFF_ID + "?data=" + enc;
  var msg = "\u899a\u3048\u66f8\u304d\u306e\u304a\u5f15\u8d8a\u3057\u7528\u30ea\u30f3\u30af\u3067\u3059\u3002\n\u65b0\u3057\u3044\u30b9\u30de\u30db\u3067\u3053\u306e\u30ea\u30f3\u30af\u3092\u30bf\u30c3\u30d7\u3059\u308b\u3068\u30e1\u30e2\u304c\u623b\u308a\u307e\u3059\u3002\n\n" + url;
  if(liffReady && liff.isInClient()){{
    liff.sendMessages([{{type:"text", text:msg}}])
      .then(function(){{ alert("\u304a\u5f15\u8d8a\u3057\u7528\u30e1\u30c3\u30bb\u30fc\u30b8\u3092\u30c8\u30fc\u30af\u306b\u9001\u308a\u307e\u3057\u305f\uff01"); }})
      .catch(function(e){{ console.error('[memo] sendMessages error', e); copyOrPrompt(msg, url); }});
  }} else {{
    copyOrPrompt(msg, url);
  }}
}}
function copyOrPrompt(msg, url){{
  if(navigator.clipboard){{
    navigator.clipboard.writeText(msg)
      .then(function(){{ alert("\u30ea\u30f3\u30af\u3092\u30b3\u30d4\u30fc\u3057\u307e\u3057\u305f\u3002LINE\u306b\u8cbc\u308a\u4ed8\u3051\u3066\u9001\u3063\u3066\u304f\u3060\u3055\u3044\u3002"); }})
      .catch(function(){{ prompt("URL\u3092\u30b3\u30d4\u30fc\u3057\u3066LINE\u306b\u9001\u3063\u3066\u304f\u3060\u3055\u3044\u3002", url); }});
  }} else {{
    prompt("URL\u3092\u30b3\u30d4\u30fc\u3057\u3066LINE\u306b\u9001\u3063\u3066\u304f\u3060\u3055\u3044\u3002", url);
  }}
}}

// --- URL復元チェック ---
function tryRestore(){{
  var dp = new URLSearchParams(location.search).get('data');
  if(!dp) return;
  try{{
    var dec = JSON.parse(decodeURIComponent(
      Array.prototype.map.call(atob(dp), function(c){{
        return '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2);
      }}).join('')
    ));
    if(confirm(dec.length + "\u4ef6\u306e\u30e1\u30e2\u304c\u898b\u3064\u304b\u308a\u307e\u3057\u305f\u3002\u3053\u306e\u7aef\u672b\u306b\u5fa9\u5143\u3057\u307e\u3059\u304b\uff1f")){{
      setMemos(dec);
      alert("\u5fa9\u5143\u3057\u307e\u3057\u305f\uff01\uff08" + dec.length + "\u4ef6\uff09");
      renderList();
    }}
  }} catch(e){{ console.log('[memo] restore error', e); }}
}}

// 4. DOMContentLoaded: HTML読み込み後にJSを実行
document.addEventListener('DOMContentLoaded', function(){{
  console.log('[memo] DOMContentLoaded');

  // 3. ボタンの紐付け（id と addEventListener で確実に登録）
  document.getElementById('fab-new').addEventListener('click', function(){{ newMemo(); }});
  document.getElementById('btn-save').addEventListener('click', function(){{ saveMemo(); }});
  document.getElementById('btn-del').addEventListener('click', function(){{ deleteMemo(); }});
  document.getElementById('back-btn').addEventListener('click', function(){{ showList(); }});
  document.getElementById('header-save').addEventListener('click', function(){{ saveMemo(); }});
  document.getElementById('btn-migration').addEventListener('click', function(){{ doMigration(); }});
  console.log('[memo] event listeners registered');

  // まずリスト表示（LIFF初期化を待たない）
  renderList();
  tryRestore();

  // 2. LIFF初期化（バックグラウンドで実行・LINEログイン状態の取得のみ）
  if(typeof liff !== 'undefined'){{
    liff.init({{ liffId: LIFF_ID }})
      .then(function(){{
        liffReady = true;
        console.log('[memo] liff.init OK');
        // 5. ログイン状態の確認
        if(liff.isLoggedIn()){{
          console.log('[memo] logged in, userID:', liff.getContext() && liff.getContext().userId);
        }} else {{
          console.log('[memo] not logged in');
        }}
      }})
      .catch(function(err){{
        console.error('[memo] liff.init error:', err.message || err);
        // LIFFエラーでも本体機能は使えるのでalertしない
      }});
  }} else {{
    console.warn('[memo] liff SDK not loaded');
  }}
}});
</script>
</body>
</html>
"""


@app.route("/liff/memo", methods=["GET"])
def liff_memo():
    html = _LIFF_MEMO_HTML.format(retro_css=_RETRO_CSS, liff_memo_id=LIFF_MEMO_ID)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/liff/api/memo", methods=["GET"])
def liff_api_memo_get():
    user_id = request.args.get("line_user_id", "").strip()
    if not user_id:
        return jsonify({"error": "line_user_id required"}), 400
    try:
        result = (
            get_supabase().table("memos")
            .select("id, content, created_at, updated_at")
            .eq("line_user_id", user_id)
            .order("updated_at", desc=True)
            .execute()
        )
        return jsonify({"memos": result.data or []})
    except Exception as e:
        logging.exception("memo get error: %s", e)
        return jsonify({"error": "server error"}), 500


@app.route("/liff/api/memo", methods=["POST"])
def liff_api_memo_post():
    data    = request.get_json(silent=True) or {}
    user_id = data.get("line_user_id", "").strip()
    content = data.get("content", "").strip()
    if not user_id or not content:
        return jsonify({"error": "missing params"}), 400
    try:
        result = (
            get_supabase().table("memos")
            .insert({"line_user_id": user_id, "content": content})
            .execute()
        )
        return jsonify(result.data[0] if result.data else {"error": "insert failed"})
    except Exception as e:
        logging.exception("memo post error: %s", e)
        return jsonify({"error": "server error"}), 500


@app.route("/liff/api/memo/<memo_id>", methods=["PUT"])
def liff_api_memo_put(memo_id):
    data    = request.get_json(silent=True) or {}
    content = data.get("content", "").strip()
    if not content:
        return jsonify({"error": "content required"}), 400
    try:
        from datetime import timezone, datetime as _dt
        now = _dt.now(timezone.utc).isoformat()
        result = (
            get_supabase().table("memos")
            .update({"content": content, "updated_at": now})
            .eq("id", memo_id)
            .execute()
        )
        return jsonify(result.data[0] if result.data else {"error": "not found"})
    except Exception as e:
        logging.exception("memo put error: %s", e)
        return jsonify({"error": "server error"}), 500


@app.route("/liff/api/memo/<memo_id>", methods=["DELETE"])
def liff_api_memo_delete(memo_id):
    try:
        get_supabase().table("memos").delete().eq("id", memo_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        logging.exception("memo delete error: %s", e)
        return jsonify({"error": "server error"}), 500


# ── ⑦ 旅行相談（LIFF） ──────────────────────────────

_LIFF_TRAVEL_HTML = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,user-scalable=no">
<title>旅行相談</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  background: #F5E6A3;
  font-family: 'Hiragino Mincho ProN','Yu Mincho','Noto Serif JP',serif;
  font-size: 17px;
  height: 100dvh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}}
.hd {{
  background: #8B1A1A;
  color: #FFD700;
  padding: 14px 16px;
  text-align: center;
  font-size: 20px;
  font-weight: bold;
  letter-spacing: 0.08em;
  border-bottom: 4px solid #5C1010;
  flex-shrink: 0;
}}
.chat-area {{
  flex: 1;
  overflow-y: auto;
  padding: 16px 12px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}}
.bubble {{
  max-width: 82%;
  padding: 12px 16px;
  border-radius: 18px;
  font-size: 17px;
  line-height: 1.7;
  word-break: break-all;
  white-space: pre-wrap;
}}
.bubble.ai {{
  background: #FFF8DC;
  border: 2px solid #C8A060;
  align-self: flex-start;
  border-bottom-left-radius: 4px;
}}
.bubble.user {{
  background: #8B1A1A;
  color: #FFD700;
  align-self: flex-end;
  border-bottom-right-radius: 4px;
}}
.bubble.loading {{
  background: #FFF8DC;
  border: 2px dashed #C8A060;
  align-self: flex-start;
  color: #AAA;
  font-style: italic;
}}
.input-area {{
  display: flex;
  gap: 8px;
  padding: 10px 12px;
  background: #FFF8DC;
  border-top: 2px solid #C8A060;
  flex-shrink: 0;
}}
.input-area textarea {{
  flex: 1;
  font-size: 17px;
  font-family: inherit;
  border: 2px solid #C8A060;
  border-radius: 12px;
  padding: 10px 12px;
  resize: none;
  background: #FFFFF0;
  color: #4A2C0A;
  height: 54px;
  max-height: 120px;
  line-height: 1.5;
  overflow-y: auto;
}}
.input-area textarea:focus {{ outline: none; border-color: #8B1A1A; }}
.send-btn {{
  background: #8B1A1A;
  color: #FFD700;
  border: none;
  border-radius: 12px;
  width: 64px;
  font-size: 22px;
  font-weight: bold;
  cursor: pointer;
  flex-shrink: 0;
  touch-action: manipulation;
}}
.send-btn:disabled {{ background: #CCC; color: #FFF; }}
</style>
</head>
<body>

<div class="hd">&#9992; &#26053;&#34892;&#30456;&#35527;</div>

<div class="chat-area" id="chat-area">
  <div class="bubble ai">&#12371;&#12435;&#12395;&#12385;&#12399;&#65281;&#26053;&#34892;&#30456;&#35527;AI&#12391;&#12377;&#12290;<br>&#21306;&#20869;&#12539;&#22269;&#20869;&#12539;&#28023;&#22806;&#12289;&#12393;&#12435;&#12394;&#26053;&#34892;&#12398;&#12372;&#30456;&#35527;&#12418;&#12393;&#12358;&#12382;&#12290;<br>&#12393;&#12371;&#12395;&#34892;&#12365;&#12383;&#12356;&#12391;&#12377;&#12363;&#65311;</div>
</div>

<div class="input-area">
  <textarea id="user-input" placeholder="&#36074;&#21839;&#12434;&#20837;&#21147;&#12375;&#12390;&#12367;&#12384;&#12373;&#12356;..." rows="1"></textarea>
  <button class="send-btn" id="send-btn">&#9658;</button>
</div>

<script>
var history = [];
var TRAVEL_SYSTEM = "\u3042\u306a\u305f\u306f\u65e5\u672c\u4e00\u306e\u65cf;x6d;x31;\u884c\u5c02\u9580AI\u30a2\u30c9\u30d0\u30a4\u30b6\u30fc\u3067\u3059\u3002\u56fd\u5185\u30fb\u6d77\u5916\u3092\u554f\u308f\u305a\u3001\u89b3\u5149\u5730\u3001\u30db\u30c6\u30eb\u9078\u3073\u3001\u65c5\u7a0b\u4f5c\u308a\u3001\u4e88\u7b97\u306e\u76ee\u5b89\u3001\u5b63\u7bc0\u306e\u304a\u3059\u3059\u3081\u3001\u6301\u3061\u7269\u306a\u3069\u3001\u65e6;x6c;\u306b\u95a2\u3059\u308b\u304c\u304f\u3056\u3093\u306e\u8cea\u554f\u306b\u4e01\u5be7\u306b\u304a\u7b54\u3048\u3057\u307e\u3059\u3002\u56de\u7b54\u306f\u65e5\u672c\u8a9e\u3067\u3001\u89aa\u3057\u307f\u3084\u3059\u304f\u5206\u304b\u308a\u3084\u3059\u3044\u8a00\u8449\u3067\u3001\u7c21\u6f54\u306b\u307e\u3068\u3081\u3066\u304f\u3060\u3055\u3044\u3002";

document.addEventListener('DOMContentLoaded', function() {{
  var chatArea = document.getElementById('chat-area');
  var input    = document.getElementById('user-input');
  var btn      = document.getElementById('send-btn');

  btn.addEventListener('click', sendMsg);
  input.addEventListener('keydown', function(e) {{
    if(e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); sendMsg(); }}
  }});
  input.addEventListener('input', function() {{
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
  }});

  function addBubble(text, type) {{
    var el = document.createElement('div');
    el.className = 'bubble ' + type;
    el.textContent = text;
    chatArea.appendChild(el);
    chatArea.scrollTop = chatArea.scrollHeight;
    return el;
  }}

  function sendMsg() {{
    var q = input.value.trim();
    if(!q || btn.disabled) return;
    addBubble(q, 'user');
    history.push({{role: 'user', content: q}});
    input.value = '';
    input.style.height = '54px';
    btn.disabled = true;

    var loading = addBubble('\u8003\u3048\u3066\u3044\u307e\u3059\u2026', 'loading');

    fetch('/liff/api/travel', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{history: history}})
    }})
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
      chatArea.removeChild(loading);
      var reply = d.reply || '\u7533\u3057\u8a33\u3054\u3056\u3044\u307e\u305b\u3093\u3002\u3082\u3046\u4e00\u5ea6\u304a\u8a66\u3057\u304f\u3060\u3055\u3044\u3002';
      addBubble(reply, 'ai');
      history.push({{role: 'assistant', content: reply}});
    }})
    .catch(function(e) {{
      chatArea.removeChild(loading);
      addBubble('\u901a\u4fe1\u30a8\u30e9\u30fc\u304c\u767a\u751f\u3057\u307e\u3057\u305f\u3002\u3082\u3046\u4e00\u5ea6\u304a\u8a66\u3057\u304f\u3060\u3055\u3044\u3002', 'ai');
    }})
    .finally(function() {{ btn.disabled = false; input.focus(); }});
  }}
}});
</script>
</body>
</html>
"""


@app.route("/liff/calendar", methods=["GET"])
def liff_calendar():
    return render_template("liff_calendar.html", liff_id=os.environ.get("LIFF_ID", LIFF_ID))


@app.route("/liff/travel", methods=["GET"])
def liff_travel():
    return _LIFF_TRAVEL_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/liff/api/travel", methods=["POST"])
def liff_api_travel():
    data = request.get_json(silent=True) or {}
    history = data.get("history", [])
    # 最大10ターンまで保持
    if len(history) > 20:
        history = history[-20:]
    # 旅行特化のシステムプロンプト
    travel_system = (
        "あなたは旅行専門のAIアドバイザーです。"
        "国内・海外を問わず、観光地・ホテル選び・旅程作り・予算の目安・季節のおすすめ・"
        "持ち物・交通手段など、旅行に関することなら何でも丁寧にお答えします。"
        "回答は日本語で、高齢者にも分かりやすい親しみやすい言葉で、"
        "箇条書きを活用して簡潔にまとめてください。"
    )
    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=travel_system,
            messages=history,
            timeout=30,
        )
        reply = next(
            (b.text for b in response.content if b.type == "text"),
            "申し訳ありません。もう一度お試しください。",
        )
        return jsonify({"reply": reply})
    except Exception as e:
        logging.exception("travel API error: %s", e)
        return jsonify({"reply": "ただいま混み合っています。しばらくしてからもう一度お試しください。"}), 500


# ── ③ 特商法ページ・利用規約 ────────────────────────

_LEGAL_CSS = """
body{font-family:'Hiragino Sans','Noto Sans JP',sans-serif;font-size:18px;
  background:#fff;color:#333;line-height:1.8;max-width:700px;margin:0 auto;padding:20px}
h1{font-size:22px;border-bottom:3px solid #1565c0;padding-bottom:10px;margin-bottom:24px;color:#1565c0}
h2{font-size:20px;margin:24px 0 8px;color:#333}
table{width:100%;border-collapse:collapse;margin-bottom:24px}
td{padding:12px;border:1px solid #ddd;font-size:18px;vertical-align:top}
td:first-child{background:#f5f7fa;font-weight:bold;width:35%;white-space:nowrap}
p{margin-bottom:16px}
ul{margin:0 0 16px 24px}
li{margin-bottom:8px}
.note{background:#fff9e6;border:1px solid #f0c060;border-radius:8px;padding:14px;font-size:17px;margin-top:24px}
"""

_TOKUSHOUHO_HTML = f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>特定商取引法に基づく表示</title>
<style>{_LEGAL_CSS}</style>
</head><body>
<h1>特定商取引法に基づく表示</h1>
<table>
<tr><td>販売事業者名</td><td>【会社名・屋号】</td></tr>
<tr><td>代表者名</td><td>【代表者氏名】</td></tr>
<tr><td>所在地</td><td>〒【郵便番号】<br>【都道府県・市区町村・番地】</td></tr>
<tr><td>電話番号</td><td>【電話番号】<br>（受付時間：平日10:00〜18:00）</td></tr>
<tr><td>メールアドレス</td><td>【メールアドレス】</td></tr>
<tr><td>サービス名</td><td>地元くらしの御用聞き</td></tr>
<tr><td>サービス内容</td><td>AIによる生活相談・地域情報提供サービス（LINEアプリ）</td></tr>
<tr><td>料金</td><td>有料プラン：月額【金額】円（税込）<br>無料プランあり（1日5回まで）</td></tr>
<tr><td>支払方法</td><td>クレジットカード（Stripe決済）</td></tr>
<tr><td>支払時期</td><td>お申し込み時に即時決済</td></tr>
<tr><td>サービス提供時期</td><td>決済完了後、即時ご利用いただけます</td></tr>
<tr><td>返金・キャンセル</td><td>月額料金のご返金はいたしかねます。<br>解約はいつでも可能です。</td></tr>
<tr><td>動作環境</td><td>LINEアプリ（iOS / Android）最新版</td></tr>
</table>
<div class="note">※ 【】内の情報は事業者が設定してください</div>
</body></html>
"""

_TERMS_HTML = f"""<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>利用規約</title>
<style>{_LEGAL_CSS}</style>
</head><body>
<h1>利用規約</h1>
<p>本利用規約（以下「本規約」）は、【会社名】（以下「当社」）が提供するLINEサービス「地元くらしの御用聞き」（以下「本サービス」）の利用条件を定めるものです。</p>

<h2>第1条（適用）</h2>
<p>本規約は、ユーザーと当社との間の本サービスの利用に関わる一切の関係に適用されます。</p>

<h2>第2条（利用登録）</h2>
<p>登録希望者が当社の定める方法によって利用登録を申請し、当社がこれを承認することによって、利用登録が完了するものとします。</p>

<h2>第3条（料金）</h2>
<ul>
<li>無料プランは1日5回まで本サービスをご利用いただけます。</li>
<li>有料プランは月額【金額】円（税込）にて無制限でご利用いただけます。</li>
<li>料金はStripeを通じてクレジットカードにて決済されます。</li>
</ul>

<h2>第4条（禁止事項）</h2>
<p>ユーザーは以下の行為を行ってはなりません。</p>
<ul>
<li>法令または公序良俗に違反する行為</li>
<li>犯罪行為に関連する行為</li>
<li>当社のサービスの運営を妨害する行為</li>
<li>他のユーザーまたは第三者を誹謗中傷する行為</li>
<li>本サービスを商業目的で無断利用する行為</li>
</ul>

<h2>第5条（免責事項）</h2>
<p>当社は、本サービスが提供するAI回答の正確性・完全性を保証しません。医療・法律・金融等の専門的判断については、必ず専門家にご相談ください。</p>

<h2>第6条（個人情報）</h2>
<p>当社は、ユーザーの個人情報を別途定めるプライバシーポリシーに従い適切に取り扱います。</p>

<h2>第7条（規約変更）</h2>
<p>当社は、必要と判断した場合には、ユーザーへの事前通知をもって本規約を変更できるものとします。</p>

<h2>第8条（準拠法・管轄）</h2>
<p>本規約の解釈は日本法に準拠し、本サービスに関する紛争は当社所在地を管轄する裁判所を第一審の専属的合意管轄とします。</p>

<p style="text-align:right;color:#888;font-size:16px">制定日：【制定日】</p>
<div class="note">※ 【】内の情報は事業者が設定してください</div>
</body></html>
"""


@app.route("/tokushouho", methods=["GET"])
def tokushouho():
    return _TOKUSHOUHO_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/terms", methods=["GET"])
def terms():
    return _TERMS_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


# ── ④ Stripe 決済 ────────────────────────────────────

@app.route("/stripe/checkout", methods=["GET"])
def stripe_checkout():
    """Stripe Checkout セッションを作成してリダイレクト。
    クエリパラメータ: line_user_id=xxx
    """
    user_id = request.args.get("line_user_id", "").strip()
    if not user_id:
        return "line_user_id required", 400
    if not STRIPE_SECRET_KEY or not STRIPE_PRICE_ID:
        return "Stripe not configured", 503
    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            mode="subscription",
            client_reference_id=user_id,
            success_url=STRIPE_SUCCESS_URL + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=STRIPE_CANCEL_URL,
        )
        from flask import redirect
        return redirect(session.url, code=303)
    except Exception as e:
        logging.exception("stripe_checkout error: %s", e)
        return "決済ページの作成に失敗しました。", 500


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """Stripe からのイベントを受け取り、支払い完了時に is_paid を更新する。"""
    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")
    if not STRIPE_WEBHOOK_SECRET:
        return "Webhook secret not configured", 503
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        return "Invalid signature", 400

    if event["type"] in ("checkout.session.completed", "invoice.paid"):
        obj     = event["data"]["object"]
        uid     = obj.get("client_reference_id") or obj.get("metadata", {}).get("line_user_id")
        if uid:
            try:
                get_supabase().table("users").update({"is_paid": True}).eq("line_user_id", uid).execute()
                user_cache.pop(uid, None)
                logging.error("Stripe: user %s upgraded to paid (event=%s)", uid, event["type"])
            except Exception as e:
                logging.exception("Stripe: DB update failed for %s: %s", uid, e)

    if event["type"] in ("customer.subscription.deleted", "invoice.payment_failed"):
        obj = event["data"]["object"]
        uid = obj.get("metadata", {}).get("line_user_id")
        if uid:
            try:
                get_supabase().table("users").update({"is_paid": False}).eq("line_user_id", uid).execute()
                user_cache.pop(uid, None)
                logging.error("Stripe: user %s downgraded to free (event=%s)", uid, event["type"])
            except Exception as e:
                logging.exception("Stripe: DB update failed for %s: %s", uid, e)

    return "OK", 200


@app.route("/stripe/success", methods=["GET"])
def stripe_success():
    return """<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>お申し込み完了</title>
<style>body{{font-family:'Hiragino Sans',sans-serif;text-align:center;padding:60px 20px;background:#e8f5e9}}
h1{{color:#2e7d32;font-size:26px;margin-bottom:16px}}p{{font-size:20px;color:#555;line-height:1.8}}</style>
</head><body>
<h1>🎉 お申し込みありがとうございます！</h1>
<p>有料会員への登録が完了しました。<br>LINEに戻って引き続きご利用ください。</p>
</body></html>""", 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/stripe/cancel", methods=["GET"])
def stripe_cancel():
    return """<!DOCTYPE html><html lang="ja"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>キャンセル</title>
<style>body{{font-family:'Hiragino Sans',sans-serif;text-align:center;padding:60px 20px;background:#fff}}
h1{{color:#555;font-size:24px;margin-bottom:16px}}p{{font-size:20px;color:#888;line-height:1.8}}</style>
</head><body>
<h1>お申し込みをキャンセルしました</h1>
<p>またいつでもお気軽にどうぞ。<br>LINEに戻ってご利用ください。</p>
</body></html>""", 200, {"Content-Type": "text/html; charset=utf-8"}


# ── LIFF 今日の情報 ─────────────────────────────────────

import urllib.parse as _up
import xml.etree.ElementTree as _ET
from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _as_completed

_TODAY_WORD_CACHE: dict[str, str] = {}   # date_str → word（当日キャッシュ）


def _weather_emoji(icon_code: str) -> str:
    m = {"01": "☀️", "02": "🌤️", "03": "⛅", "04": "☁️",
         "09": "🌦️", "10": "🌧️", "11": "⛈️", "13": "❄️", "50": "🌫️"}
    return m.get((icon_code or "01")[:2], "🌤️")


def _clothes_advice(temp_max: float, temp_min: float) -> str:
    avg = (temp_max + temp_min) / 2
    if avg <= 5:
        return "厚手のコートが必要です🧥"
    if avg <= 15:
        return "上着を忘れずに🧣"
    if avg <= 22:
        return "長袖1枚でOK👕"
    if avg <= 28:
        return "半袖でOKですよ👕"
    return "熱中症に注意☀️ 水分補給を忘れずに"


def _umbrella_advice(rain_prob: int) -> str:
    if rain_prob <= 30:
        return "傘は不要です☀️"
    if rain_prob <= 60:
        return "折りたたみ傘があると安心🌂"
    return "傘を持って出かけましょう☂️"


def _health_advice(temp_max: float, temp_min: float) -> str:
    month = date.today().month
    diff = temp_max - temp_min
    msgs = []
    if 2 <= month <= 5:
        msgs.append("花粉の季節です😷 外出時はマスクを")
    elif 6 <= month <= 9:
        msgs.append("熱中症に注意🌡️ こまめに水分補給を")
    elif month in (12, 1, 2):
        msgs.append("路面凍結に注意❄️ 転倒しないよう気をつけて")
    if diff >= 10:
        msgs.append(f"気温差が{int(diff)}度あります。羽織るものを😊")
    return "　".join(msgs) if msgs else ""


def _fetch_weather(prefecture: str, city: str) -> dict:
    """OpenWeatherMap から天気情報を取得する。"""
    if not OPENWEATHER_API_KEY:
        return {"error": "APIキー未設定"}
    location = city or prefecture
    if not location:
        return {"error": "地域情報なし"}
    try:
        geo = httpx.get(
            "http://api.openweathermap.org/geo/1.0/direct",
            params={"q": f"{location},JP", "limit": 1, "appid": OPENWEATHER_API_KEY},
            timeout=8,
        ).json()
        if not geo:
            return {"error": "地域が見つかりません"}
        lat, lon = geo[0]["lat"], geo[0]["lon"]

        cur = httpx.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY,
                    "units": "metric", "lang": "ja"},
            timeout=8,
        ).json()

        fct = httpx.get(
            "https://api.openweathermap.org/data/2.5/forecast",
            params={"lat": lat, "lon": lon, "appid": OPENWEATHER_API_KEY,
                    "units": "metric", "lang": "ja", "cnt": 8},
            timeout=8,
        ).json()

        # 今日の降水確率（3h ブロック最大値）
        today_str = date.today().isoformat()
        rain_prob = 0
        for item in (fct.get("list") or []):
            if datetime.fromtimestamp(item["dt"]).strftime("%Y-%m-%d") == today_str:
                rain_prob = max(rain_prob, int(item.get("pop", 0) * 100))

        main = cur["main"]
        temp_max = round(main.get("temp_max", main["temp"]))
        temp_min = round(main.get("temp_min", main["temp"]))
        description = (cur["weather"][0].get("description") or "").strip()
        icon_code = cur["weather"][0].get("icon", "01d")
        wind_speed = round(cur.get("wind", {}).get("speed", 0), 1)

        return {
            "description": description,
            "icon_emoji":  _weather_emoji(icon_code),
            "temp_max":    temp_max,
            "temp_min":    temp_min,
            "rain_prob":   rain_prob,
            "wind_speed":  wind_speed,
            "clothes":     _clothes_advice(temp_max, temp_min),
            "umbrella":    _umbrella_advice(rain_prob),
            "health":      _health_advice(temp_max, temp_min),
            "error":       None,
        }
    except Exception as e:
        logging.error("weather fetch error: %s", e)
        return {"error": "天気情報を取得できませんでした"}


def _fetch_today_word() -> str:
    """Claude Haiku で今日のひとことを生成（当日キャッシュ）。"""
    today = date.today()
    key = today.isoformat()
    if key in _TODAY_WORD_CACHE:
        return _TODAY_WORD_CACHE[key]

    month, day = today.month, today.day
    season = {12: "冬", 1: "冬", 2: "冬", 3: "春", 4: "春", 5: "春",
              6: "夏", 7: "夏", 8: "夏", 9: "秋", 10: "秋", 11: "秋"}[month]
    fallbacks = {
        "春": "春の風が心地よい季節ですね🌸 今日も元気に過ごしましょう！",
        "夏": "暑い日が続きますね☀️ こまめに水分補給してください！",
        "秋": "秋の空気が気持ちいいですね🍂 今日も良い一日を！",
        "冬": "寒い日が続いています❄️ 暖かくしてお過ごしください！",
    }
    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": (
                f"今日は{today.year}年{month}月{day}日（{season}）です。\n"
                "高齢者向けの温かい一言メッセージを日本語で生成してください。\n"
                "季節・記念日・豆知識・励ましなど内容を毎日変えて。\n"
                "40文字以内で絵文字を1〜2個使用。メッセージのみ出力してください。"
            )}],
            timeout=12,
        )
        word = resp.content[0].text.strip()
        _TODAY_WORD_CACHE[key] = word
        # 前日以前のキャッシュを削除
        for old_key in list(_TODAY_WORD_CACHE):
            if old_key < key:
                _TODAY_WORD_CACHE.pop(old_key, None)
        return word
    except Exception as e:
        logging.error("today_word error: %s", e)
        return fallbacks[season]


def _fetch_local_news(prefecture: str, city: str) -> list:
    """Google News RSS からローカルニュースを最大3件取得する。"""
    query = city or prefecture or "地域ニュース"
    try:
        url = (
            "https://news.google.com/rss/search"
            f"?q={_up.quote(query)}&hl=ja&gl=JP&ceid=JP:ja"
        )
        resp = httpx.get(url, timeout=8, follow_redirects=True)
        root = _ET.fromstring(resp.text)
        items = []
        for item in root.findall(".//item")[:3]:
            t = item.find("title")
            l = item.find("link")
            if t is not None and l is not None:
                title = (t.text or "").strip()
                link  = (l.text or "").strip()
                if " - " in title:
                    title = title.rsplit(" - ", 1)[0]
                items.append({"title": title[:55], "link": link})
        return items
    except Exception as e:
        logging.error("news fetch error: %s", e)
        return []


def _closing_word() -> str:
    hour = datetime.now(timezone(timedelta(hours=9))).hour
    month = date.today().month
    season_emoji = {
        12: "❄️", 1: "❄️", 2: "❄️",
        3: "🌸", 4: "🌸", 5: "🌿",
        6: "☔", 7: "☀️", 8: "☀️",
        9: "🍂", 10: "🍂", 11: "🍂"
    }.get(month, "😊")
    if hour < 12:
        return f"今日も素敵な一日になりますように{season_emoji}"
    if hour < 18:
        return f"午後も元気に過ごしてくださいね{season_emoji}"
    return f"今日もお疲れさまでした。ゆっくり休んでください{season_emoji}"


@app.route("/liff/api/today", methods=["GET"])
def liff_today_api():
    user_id = request.args.get("line_user_id", "").strip()
    if not user_id:
        return jsonify({"error": "line_user_id required"}), 400
    try:
        result = get_supabase().table("users").select(
            "name, region, prefecture, city, is_paid"
        ).eq("line_user_id", user_id).limit(1).execute()
        if not result.data:
            return jsonify({"error": "user not found"}), 404
        user = result.data[0]
    except Exception as e:
        logging.exception("today api user fetch: %s", e)
        return jsonify({"error": "server error"}), 500

    pref = user.get("prefecture") or ""
    city = user.get("city") or ""
    if not pref and not city:
        region = user.get("region") or ""
        m = re.match(r'^(.+?[都道府県])(.+)$', region)
        if m:
            pref, city = m.group(1), m.group(2)

    # 並列取得
    results: dict = {}
    with _TPE(max_workers=3) as ex:
        fut_weather = ex.submit(_fetch_weather, pref, city)
        fut_word    = ex.submit(_fetch_today_word)
        fut_news    = ex.submit(_fetch_local_news, pref, city)
        results["weather"] = fut_weather.result()
        results["today_word"] = fut_word.result()
        results["news"] = fut_news.result()

    return jsonify({
        "user":         {"name": user.get("name") or "", "region": pref + city},
        "weather":      results["weather"],
        "today_word":   results["today_word"],
        "news":         results["news"],
        "closing_word": _closing_word(),
    })


_LIFF_TODAY_HTML = """\
<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0,user-scalable=yes">
<title>今日の情報</title>
<script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
<style>
{retro_css}
/* ── 今日ページ固有スタイル ─── */
body {{ background:#FFFFFF; font-size:18px; }}
.wrap {{ max-width:520px; margin:0 auto; padding-bottom:40px; }}

/* ヘッダー */
.today-header {{
  background: var(--header-bg);
  color: var(--header-text);
  padding: 20px 16px 14px;
  text-align: center;
}}
.today-header .greeting {{ font-size:24px; font-weight:bold; letter-spacing:.05em; }}
.today-header .date-str {{ font-size:16px; margin-top:6px; opacity:.9; }}

/* ローディング */
.loader {{
  text-align:center; padding:60px 20px;
  color:var(--sub-text); font-size:20px;
}}
.spinner {{
  display:inline-block; width:44px; height:44px;
  border:5px solid #ddd; border-top-color:var(--header-bg);
  border-radius:50%; animation:spin .8s linear infinite;
  margin-bottom:14px;
}}
@keyframes spin {{ to{{transform:rotate(360deg)}} }}

/* セクションタイトル */
.sec-title {{
  font-size:17px; font-weight:bold; color:var(--header-bg);
  border-left:5px solid var(--header-bg);
  padding-left:10px; margin:20px 16px 8px;
}}

/* 今日のひとこと */
.word-card {{
  background:#FFFDE7; border:2px solid #F9A825;
  border-radius:14px; padding:18px 20px;
  margin:8px 16px; font-size:20px; line-height:1.6;
  color:#4A2C0A; text-align:center;
  box-shadow:2px 3px 0 #F9A825;
}}

/* 天気カード（最重要）*/
.weather-card {{
  background:var(--card-bg); border:2px solid var(--border);
  border-radius:14px; padding:20px 16px;
  margin:8px 16px; box-shadow:2px 3px 0 var(--border);
}}
.weather-main {{
  display:flex; align-items:center; gap:14px;
  margin-bottom:16px;
}}
.weather-icon {{ font-size:64px; line-height:1; }}
.weather-desc {{}}
.weather-desc .desc-text {{ font-size:20px; color:var(--text); font-weight:bold; }}
.weather-desc .temp-row {{
  font-size:28px; font-weight:bold; color:var(--header-bg);
  margin-top:4px;
}}
.weather-desc .temp-row .temp-min {{ font-size:22px; color:var(--sub-text); }}

.advice-row {{
  display:flex; gap:10px; flex-wrap:wrap; margin-top:8px;
}}
.advice-chip {{
  background:#FFF8DC; border:2px solid var(--border);
  border-radius:20px; padding:8px 14px;
  font-size:17px; color:var(--text); flex:1; min-width:140px;
  text-align:center; line-height:1.4;
}}
.advice-chip.full {{ flex:100%; min-width:unset; }}
.detail-row {{
  display:flex; gap:16px; margin-top:12px;
  font-size:16px; color:var(--sub-text);
}}
.health-advice {{
  background:#FFF3E0; border:2px solid #FF8F00;
  border-radius:10px; padding:10px 14px;
  font-size:17px; color:#4A2C0A; margin-top:12px;
  line-height:1.5;
}}
.weather-err {{
  text-align:center; padding:24px; font-size:18px;
  color:var(--sub-text);
}}

/* スケジュール */
.schedule-card {{
  background:var(--card-bg); border:2px solid var(--border);
  border-radius:14px; padding:16px;
  margin:8px 16px; box-shadow:2px 3px 0 var(--border);
}}
.sched-item {{
  font-size:19px; padding:10px 0;
  border-bottom:1px dashed var(--divider);
  color:var(--text); line-height:1.5;
}}
.sched-item:last-child {{ border-bottom:none; }}
.sched-link {{
  display:block; width:100%;
  background:var(--header-bg); color:var(--header-text);
  border:none; border-radius:10px; padding:14px;
  font-size:18px; font-weight:bold; text-align:center;
  text-decoration:none; cursor:pointer;
  margin-top:8px; box-shadow:2px 3px 0 #5C1010;
}}
.sched-link:active {{ transform:translateY(2px); box-shadow:none; }}

/* ゴミ */
.garbage-card {{
  background:#F1F8E9; border:2px solid #7CB342;
  border-radius:14px; padding:16px;
  margin:8px 16px; font-size:20px; color:#33691E;
  text-align:center; box-shadow:2px 3px 0 #7CB342;
}}

/* ニュース */
.news-card {{
  background:var(--card-bg); border:2px solid var(--border);
  border-radius:14px; padding:4px 16px;
  margin:8px 16px; box-shadow:2px 3px 0 var(--border);
}}
.news-item {{
  padding:14px 0; border-bottom:1px dashed var(--divider);
  font-size:17px; color:var(--text); line-height:1.5;
}}
.news-item:last-child {{ border-bottom:none; }}
.news-item a {{
  color:var(--text); text-decoration:none; display:block;
}}
.news-item a:active {{ color:var(--header-bg); }}

/* 締めのひとこと */
.closing-card {{
  background:var(--header-bg); color:var(--header-text);
  border-radius:14px; padding:20px;
  margin:8px 16px; text-align:center;
  font-size:22px; font-weight:bold;
  box-shadow:2px 3px 0 #5C1010;
}}

/* エラー・再読み込み */
.err-card {{
  background:#FFF0F0; border:2px solid #C62828;
  border-radius:14px; padding:16px; margin:8px 16px;
  text-align:center; color:#B71C1C; font-size:18px;
}}
.reload-btn {{
  background:var(--header-bg); color:var(--header-text);
  border:none; border-radius:8px; padding:12px 24px;
  font-size:18px; font-weight:bold; cursor:pointer;
  margin-top:12px;
}}
</style>
</head>
<body>
<div class="today-header">
  <div class="greeting" id="greeting">読み込み中…</div>
  <div class="date-str" id="date-str"></div>
</div>

<div class="wrap">
  <!-- ローディング -->
  <div id="loader" class="loader">
    <div><div class="spinner"></div></div>
    <div>情報を取得しています…</div>
  </div>

  <!-- コンテンツ（取得後に表示） -->
  <div id="content" style="display:none">

    <!-- 今日のひとこと -->
    <div class="sec-title">💬 今日のひとこと</div>
    <div class="word-card" id="word-card"></div>

    <!-- 天気 -->
    <div class="sec-title">🌤️ 今日の天気</div>
    <div id="weather-area"></div>

    <!-- スケジュール -->
    <div class="sec-title">📅 今日の予定</div>
    <div class="schedule-card" id="schedule-area"></div>

    <!-- ゴミ -->
    <div id="garbage-wrap" style="display:none">
      <div class="sec-title">🗑️ 今日のゴミ</div>
      <div class="garbage-card" id="garbage-area"></div>
    </div>

    <!-- ニュース -->
    <div id="news-wrap" style="display:none">
      <div class="sec-title">📰 地元のニュース</div>
      <div class="news-card" id="news-area"></div>
    </div>

    <!-- 締め -->
    <div class="closing-card" id="closing-card"></div>

    <!-- 再読み込み -->
    <div style="text-align:center;margin:24px 16px">
      <button class="reload-btn" onclick="reload()">🔄 再読み込み</button>
    </div>
  </div><!-- /content -->

  <!-- グローバルエラー -->
  <div id="global-err" style="display:none" class="err-card">
    <div id="global-err-msg">エラーが発生しました</div>
    <button class="reload-btn" onclick="reload()">🔄 再読み込み</button>
  </div>
</div><!-- /wrap -->

<script>
var LIFF_ID = "{liff_id}";
var userId  = null;

// 日付・挨拶
(function(){{
  var now = new Date();
  var h   = now.getHours();
  var weekdays = ['日','月','火','水','木','金','土'];
  var dateStr = now.getFullYear() + '年' +
    (now.getMonth()+1) + '月' + now.getDate() + '日（' +
    weekdays[now.getDay()] + '）';
  document.getElementById('date-str').textContent = dateStr;
}})();

function greet(name){{
  var h = new Date().getHours();
  var salut = h < 11 ? 'おはようございます' : h < 17 ? 'こんにちは' : 'こんばんは';
  var who = name ? name + 'さん' : 'ようこそ';
  document.getElementById('greeting').textContent = salut + '、' + who + '😊';
}}

// LIFF 初期化
liff.init({{liffId: LIFF_ID}})
  .then(function(){{
    if(!liff.isLoggedIn()){{ liff.login(); return; }}
    return liff.getProfile();
  }})
  .then(function(profile){{
    if(!profile) return;
    userId = profile.userId;
    loadData();
  }})
  .catch(function(){{
    showGlobalErr('LINEログインに失敗しました。\\nLINEアプリから開き直してください。');
  }});

function loadData(){{
  fetch('/liff/api/today?line_user_id=' + encodeURIComponent(userId))
    .then(function(r){{ return r.json(); }})
    .then(function(d){{
      if(d.error){{ showGlobalErr('ユーザー情報が見つかりません。\\nLINEで登録を完了してください。'); return; }}
      render(d);
    }})
    .catch(function(){{
      showGlobalErr('データを取得できませんでした。\\n通信状況をご確認ください。');
    }});
}}

function render(d){{
  // 挨拶
  greet((d.user||{{}}).name||'');

  // 今日のひとこと
  document.getElementById('word-card').textContent = d.today_word || '今日も良い一日を😊';

  // 天気
  renderWeather(d.weather||{{}});

  // スケジュール
  renderSchedule();

  // ゴミ（サーバーから返ってくることがあれば表示）
  if(d.garbage){{
    document.getElementById('garbage-area').textContent = d.garbage;
    document.getElementById('garbage-wrap').style.display = 'block';
  }}

  // ニュース
  renderNews(d.news||[]);

  // 締め
  document.getElementById('closing-card').textContent = d.closing_word || '今日も良い一日を😊';

  document.getElementById('loader').style.display  = 'none';
  document.getElementById('content').style.display = 'block';
}}

function renderWeather(w){{
  var area = document.getElementById('weather-area');
  if(w.error){{
    area.innerHTML = '<div class="weather-card"><div class="weather-err">⚠️ ' + esc(w.error) + '</div></div>';
    return;
  }}
  var healthHtml = w.health
    ? '<div class="health-advice">⚠️ ' + esc(w.health) + '</div>'
    : '';
  area.innerHTML =
    '<div class="weather-card">' +
      '<div class="weather-main">' +
        '<div class="weather-icon">' + (w.icon_emoji||'🌤️') + '</div>' +
        '<div class="weather-desc">' +
          '<div class="desc-text">' + esc(w.description||'') + '</div>' +
          '<div class="temp-row">' +
            '最高 <strong>' + (w.temp_max!==undefined?w.temp_max:'--') + '°</strong>&nbsp;' +
            '<span class="temp-min">最低 ' + (w.temp_min!==undefined?w.temp_min:'--') + '°</span>' +
          '</div>' +
        '</div>' +
      '</div>' +
      '<div class="advice-row">' +
        '<div class="advice-chip">👕 ' + esc(w.clothes||'') + '</div>' +
        '<div class="advice-chip">☂️ ' + esc(w.umbrella||'') + '</div>' +
        '<div class="advice-chip full">💨 風速 ' + (w.wind_speed!==undefined?w.wind_speed:'--') + 'm/s　🌂 降水確率 ' + (w.rain_prob!==undefined?w.rain_prob:'--') + '%</div>' +
      '</div>' +
      healthHtml +
    '</div>';
}}

function renderSchedule(){{
  var area = document.getElementById('schedule-area');
  area.innerHTML =
    '<div class="sched-item">📅 スケジュールの確認・登録はこちら</div>' +
    '<a class="sched-link" href="/liff/calendar">📆 カレンダーを開く</a>';
}}

function renderNews(items){{
  if(!items||!items.length) return;
  var area = document.getElementById('news-area');
  var html = '';
  items.forEach(function(item){{
    html += '<div class="news-item"><a href="' + esc(item.link) + '" target="_blank">📰 ' + esc(item.title) + '</a></div>';
  }});
  area.innerHTML = html;
  document.getElementById('news-wrap').style.display = 'block';
}}

function showGlobalErr(msg){{
  document.getElementById('loader').style.display     = 'none';
  document.getElementById('global-err-msg').textContent = msg;
  document.getElementById('global-err').style.display = 'block';
}}

function reload(){{
  location.reload();
}}

function esc(s){{
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}
</script>
</body>
</html>
"""


@app.route("/liff/today", methods=["GET"])
def liff_today():
    html = _LIFF_TODAY_HTML.format(retro_css=_RETRO_CSS, liff_id=LIFF_ID)
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


# ── ヘルスチェック ────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
