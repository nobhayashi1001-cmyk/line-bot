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
RICH_MENU_ID        = os.environ.get("RICH_MENU_ID", "")
OPENAI_API_KEY      = os.environ.get("OPENAI_API_KEY", "")
OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
LIFF_ID          = os.environ.get("LIFF_ID", "")
LIFF_INVITE_ID   = os.environ.get("LIFF_INVITE_ID",  LIFF_ID)

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
# 昭和モード：性別未登録ユーザーが「なつかしい昭和」を押して性別入力待ち
_showa_gender_pending: set[str] = set()
# 昭和トーク中のセッション: {user_id: {"era": int, "gender": str|None, "topic": str}}
_showa_sessions: dict[str, dict] = {}

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
・天気情報は提供された【現在の天気情報】をもとに答えてください
・交通、イベントなどのリアルタイム情報は回答できない
・「交通情報はYahoo!カーナビ、イベントは地域の広報誌をご確認くださいね」とやさしく案内する

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
    ("🗣️ AIに相談",    "AIに相談"),
    ("📻 なつかしい昭和", "なつかしい昭和"),
    ("👥 友達に紹介",   "友達に紹介"),
    ("🏠 最初に戻る",   "最初に戻る"),
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


def _generate_referral_code() -> str:
    """衝突チェック付きで6文字の紹介コードを生成する。"""
    for _ in range(10):
        code = secrets.token_hex(3).upper()
        result = get_supabase().table("users").select("id").eq("referral_code", code).execute()
        if not result.data:
            return code
    return secrets.token_hex(4).upper()  # 万一衝突が続いたら8文字で返す


def _apply_rich_menu(user_id: str) -> None:
    """全ユーザーに同じリッチメニューを適用する。
    メモリキャッシュで前回適用済みの場合はスキップ。
    """
    if not RICH_MENU_ID:
        return
    if _applied_menu_cache.get(user_id) == RICH_MENU_ID:
        return
    try:
        line_bot_api.link_rich_menu_to_user(user_id, RICH_MENU_ID)
        _applied_menu_cache[user_id] = RICH_MENU_ID
        logging.info("rich menu applied: %s → %s", user_id, RICH_MENU_ID)
    except Exception as e:
        logging.error("rich menu link error: %s", e)


def safe_push_message(user_id: str, messages, user_info: dict | None = None) -> None:
    """reply_token が失効した場合のフォールバックとして push_message を送る。"""
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

    # 天気・気温・雨などの質問には天気情報を注入
    _WEATHER_KEYWORDS = {"天気", "気温", "雨", "晴れ", "曇り", "傘", "暑い", "寒い", "服装", "気候"}
    if any(kw in user_message for kw in _WEATHER_KEYWORDS):
        user_region = (user_info or {}).get("region", "")
        if user_region:
            try:
                weather_data = _fetch_weather(user_region)
                if weather_data:
                    system += f"\n\n【現在の天気情報】\n{weather_data}\n上記の天気情報を参考に答えてください。"
            except Exception:
                pass

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
            model="claude-haiku-4-5",
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
            args=(user_id,),
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
        _showa_sessions.pop(user_id, None)
        _showa_gender_pending.discard(user_id)
        _clear_history(user_id)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text="何でもどうぞ😊\n下のボタンをタップして\nお話しましょう！",
                quick_reply=_build_quick_reply(_MENU_QR_ITEMS),
            ),
        )
        return

    # AIに相談（御用聞きさんへのエントリー）
    if msg == "AIに相談":
        import random as _random
        _name = (user_info or {}).get("name") or ""
        _name_call = f"{_name}さん" if _name else "あなた"
        if not _has_message_history(user_id):
            _welcome = (
                f"{_name_call}、はじめまして！😊\n\n何でも気軽に話しかけてください。\n間違えても大丈夫ですよ！"
            )
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(
                    text=_welcome,
                    quick_reply=_build_quick_reply([
                        ("健康のこと",   "健康について相談したいです"),
                        ("天気を知りたい", "今日の天気を教えてください"),
                        ("雑談したい",   "少し雑談しませんか"),
                        _QR_BACK,
                    ]),
                ),
            )
        else:
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
                        ("天気を知りたい", "今日の天気を教えてください"),
                        ("雑談したい",   "少し雑談しませんか"),
                        _QR_BACK,
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

    # ── 友達に紹介 ────────────────────────────────────────────────────
    if msg == "友達に紹介":
        referral_code = _get_referral_code(user_id)
        line_bot_api.reply_message(event.reply_token, _flex_referral_menu(referral_code))
        return

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

    # 紹介コード入力
    referral_match = re.match(r'紹介コード[：:]\s*([A-Fa-f0-9]{6,8})', msg)
    if referral_match:
        code = referral_match.group(1).upper()
        reply_text = _handle_referral_input(user_id, code)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply_text, quick_reply=_build_quick_reply(_MENU_QR_ITEMS)),
        )
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
            qr = _build_quick_reply([
                ("もっと詳しく聞く", "もっと詳しく教えてください"),
                ("他のことを聞く",   "他のことを聞かせてください"),
                _QR_BACK,
            ])
            try:
                line_bot_api.reply_message(r_token, TextSendMessage(text=reply_text, quick_reply=qr))
            except Exception:
                safe_push_message(uid, [TextSendMessage(text=reply_text, quick_reply=qr)], uinfo)
        except Exception as e:
            logging.exception("send reply error: %s", e)

    # skip_faq = in_conversation（会話継続中は飲食店DB注入もスキップ）
    # save_missed = not in_conversation（新規トピックでFAQミスの場合のみ記録）
    threading.Thread(
        target=_process,
        args=(user_id, user_message, user_info, in_conversation, not in_conversation, event.reply_token),
        daemon=True,
    ).start()



# LIFF がエンドポイント URL に liff.state クエリパラメータを付けてリダイレクトする。
_LIFF_VALID_PATHS = {
    "/mypage":   "/liff/mypage",
    "/invite":   "/liff/invite",
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



# ── ヘルスチェック ────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
