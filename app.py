from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration
from flask import Flask, request, abort

logging.basicConfig(level=logging.ERROR)
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage, FollowEvent,
    TemplateSendMessage, ButtonsTemplate, ConfirmTemplate,
    CarouselTemplate, CarouselColumn, MessageAction, URIAction,
    QuickReply, QuickReplyButton,
)
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

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
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

SYSTEM_PROMPT = """あなたは「地元くらしの御用聞き」です。
高齢者の生活を、LINEを通じてそっとサポートする、頼れる近所の案内人です。

このサービスは「AIのすごさ」を見せるためのものではありません。
主役は「暮らしの役立ち感」です。

【あなたの存在意義】
高齢者が毎日安心して使える「生活のホーム画面」になること。
機能の説明者ではなく、「頼れる近所の案内人」として、暮らしに寄り添う。

【呼びかけ方】
・ユーザーの名前が登録されている場合は、必ず名前で呼びかける（例：「田中さん、こんにちは」）
・名前がない場合は「あなた」ではなく自然な言い回しで話しかける

【話し方】
・やさしく、丁寧に、短く話す
・否定しない、責めない、急かさない
・不安をやわらげる一言を添える
・高齢者を子ども扱いしない
・AIらしさより「頼れる近所の案内人」らしさを優先する
・「一緒に確認しましょう」という姿勢を大切にする

【文章ルール（らくらくフォン思想）】
・1返信1テーマ：1つの返信に1つの話題だけ扱う
・短い文で答える（1文に1つの情報だけ）
・改行を多めにして読みやすくする
・専門用語はできるだけ使わない
・質問は一度に1つだけにする
・選択肢を出す時は3つ以内にする

【回答の基本形】
1. 安心できる一言（名前があれば名前を添えて）
2. 要点を短く答える（3行以内）
3. 必要なら箇条書きで整理する
4. 最後に「最初に戻る」「他のことを聞く」などの選択肢を自然に提示する

【失敗しても怖くない設計】
・何を送っても優しく受け止める
・意味がわからないメッセージが来ても責めず、やさしく聞き直す
・ユーザーが困っていそうな時は、選択肢を提示して迷わせない

【毎日使う理由を作る】
・天気・地元情報・季節の話題を会話の中に自然に入れる
・「今日は〇〇の日ですね」など、小さな話題で親しみを作る

【対応エリア】
・ユーザーの登録地域に特化した情報を優先する
・地域情報を出す時は「○○では」「この地域では」など、生活圏に寄り添う表現を使う

【優先カテゴリ】
スマホ相談・病院や薬局・買い物・飲食・行政情報・ごみ出し・天気と防災・詐欺SMS相談

【わからない時】
・推測で断定しない
・情報が足りない時は、その旨をやさしく伝える
・必要なら地域名や状況を1つだけ確認する

【対応しないこと】
医療、法律、お金、緊急対応などの専門判断はしないでください。
その場合は、「専門の窓口に相談するのが安心です」とやさしく案内してください。

【禁止事項】
・不安をあおる表現
・命令口調、上から目線
・高齢者を子ども扱いする表現
・相手の理解力や能力を否定する表現
・情報の詰め込みすぎ
・不確かな内容の断定
・医療・法律・お金の専門判断
・AIっぽい堅い言い回し（「承知しました」「かしこまりました」など）"""

MAX_HISTORY = 20
MAX_WEB_SEARCH_TURNS = 5   # pause_turn の最大継続回数
WEB_SEARCH_TIMEOUT   = 10  # Web検索ありAPI呼び出しのタイムアウト（秒）
NO_TOOL_TIMEOUT      = 20  # Web検索なしAPI呼び出しのタイムアウト（秒）
TOTAL_REPLY_TIMEOUT  = 28  # 返答全体のハードタイムアウト（秒）- 30秒以内を保証

# 最新版（Sonnet 4.6 / Opus 4.6 でダイナミックフィルタリング対応）
WEB_SEARCH_TOOLS_V2 = [
    {"type": "web_search_20260209", "name": "web_search"},
    {"type": "web_fetch_20260209",  "name": "web_fetch"},
]
# 旧版（全モデル対応、フォールバック用）
WEB_SEARCH_TOOLS_V1 = [
    {"type": "web_search_20250305", "name": "web_search"},
    {"type": "web_fetch_20250910",  "name": "web_fetch"},
]


_MENU_QR_ITEMS = [
    ("📱 スマホ相談",     "スマホの使い方について教えてください"),
    ("🏥 病院・薬局",     "近くの病院や薬局を教えてください"),
    ("☀️ 天気・防災",     "今日の藤沢の天気を教えてください"),
    ("🛒 ごはん・買い物",  "藤沢のおすすめのお店を教えてください"),
    ("🗑️ ごみ出し",       "ごみ出しのルールを教えてください"),
    ("📰 藤沢の今",       "藤沢市の最新情報を教えてください"),
]

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


def _build_registration_confirm(state: dict) -> TemplateSendMessage:
    """登録内容の確認テンプレートを返す。"""
    return TemplateSendMessage(
        alt_text="登録内容の確認",
        template=ConfirmTemplate(
            text=(
                f"確認します。\n"
                f"お名前：{state['name']}さん\n"
                f"地域：{state['region']}\n"
                f"生年月日：{state['birthdate']}\n"
                "この内容でよろしいですか？"
            ),
            actions=[
                MessageAction(label="はい、登録します",  text="はい"),
                MessageAction(label="最初からやり直す",  text="やり直す"),
            ],
        ),
    )


def _build_welcome_message(name: str) -> TextSendMessage:
    """登録完了後のウェルカムメッセージをQuickReply付きテキストで返す。"""
    return TextSendMessage(
        text=(
            f"ご登録ありがとうございました。\n"
            f"{name}さん、これからよろしくお願いします。\n\n"
            "下のボタンをタップして、さっそく使ってみてください。"
        ),
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


# ── 登録フロー ─────────────────────────────────────────

def start_registration(user_id: str) -> TextSendMessage:
    registration_states[user_id] = {"step": "awaiting_name"}
    return TextSendMessage(text=(
        "はじめまして。\n"
        "ご利用にあたって、簡単なご登録をお願いします。\n\n"
        "まず、お名前を教えていただけますか？"
    ))


def handle_registration(user_id: str, message: str) -> TemplateSendMessage | TextSendMessage:
    state = registration_states[user_id]
    step = state["step"]

    if step == "awaiting_name":
        state["name"] = message.strip()
        state["step"] = "awaiting_region"
        return TextSendMessage(text=(
            f"{state['name']}さん、ありがとうございます。\n\n"
            "お住まいの市区町村を教えていただけますか？\n"
            "（例：藤沢市、横浜市港北区、大阪市天王寺区）"
        ))

    if step == "awaiting_region":
        state["region"] = message.strip()
        state["step"] = "awaiting_birthdate"
        return TextSendMessage(text=(
            "ありがとうございます。\n\n"
            "最後に、生年月日を教えていただけますか？\n"
            "（例：1950年1月15日）"
        ))

    if step == "awaiting_birthdate":
        state["birthdate"] = message.strip()
        state["step"] = "awaiting_confirmation"
        return _build_registration_confirm(state)

    if step == "awaiting_confirmation":
        if message.strip() == "はい":
            _save_user(user_id, state)
            name = state["name"]
            del registration_states[user_id]
            return _build_welcome_message(name)
        else:
            registration_states[user_id] = {"step": "awaiting_name"}
            return TextSendMessage(text="わかりました。最初からやり直しましょう。\nお名前を教えてください。")

    return TextSendMessage(text="少々お待ちください。")


def _save_user(user_id: str, state: dict) -> None:
    get_supabase().table("users").upsert(
        {
            "line_user_id": user_id,
            "name": state["name"],
            "region": state["region"],
            "birthdate": state["birthdate"],
        },
        on_conflict="line_user_id",
    ).execute()
    user_cache.pop(user_id, None)  # 登録完了時にキャッシュを無効化


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
        .select("name, region")
        .eq("line_user_id", user_id)
        .limit(1)
        .execute()
    )
    user = result.data[0] if result.data else None
    user_cache[user_id] = user
    return user


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


# Web検索が必要なキーワード（リアルタイム情報が必要な場合のみ）
_WEB_SEARCH_KEYWORDS = {
    "天気", "気温", "予報", "雨", "台風", "雪", "晴れ", "曇り",
    "今日", "明日", "今週", "最新", "最近", "ニュース", "情報",
    "現在", "今の", "今は", "いま", "何時", "開いてる", "営業",
    "イベント", "祭り", "工事", "渋滞", "運休", "遅延",
}


def _needs_web_search(message: str) -> bool:
    """リアルタイム情報が必要な質問かどうかを判定する。"""
    return any(kw in message for kw in _WEB_SEARCH_KEYWORDS)


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
        system += (
            f"\n\n【このユーザーの情報】"
            f"\n・お名前：{user_info['name']}（必ず「{user_info['name']}さん」と呼びかけてください）"
            f"\n・お住まいの地域：{user_info['region']}"
        )

    # 飲食系の質問 かつ 藤沢市ユーザーのみDBから店舗情報を取得（他都市は順次対応予定）
    user_region = (user_info or {}).get("region", "")
    if _is_food_query(user_message) and "藤沢" in user_region:
        restaurant_context = _search_restaurants(user_message)
        if restaurant_context:
            system += f"\n\n{restaurant_context}\n上記の情報を参考にして答えてください。"

    response = None

    # 三段階フォールバック:
    #   1. 最新ツール (web_search_20260209 / web_fetch_20260209)
    #   2. 旧ツール   (web_search_20250305 / web_fetch_20250910)
    #   3. ツールなし（RAGデータとClaudeの知識のみで回答）
    # Web検索が必要な質問のみV2ツールを試みる。それ以外は最初からツールなし。
    tool_candidates = (WEB_SEARCH_TOOLS_V2, None) if _needs_web_search(user_message) else (None,)
    for tools in tool_candidates:
        try:
            messages = list(history)
            # すべてのAPI呼び出しに明示的タイムアウトを設定（スレッド蓄積を防ぐ）
            # Web検索あり: 15秒 / Web検索なし: 20秒
            api_timeout = float(WEB_SEARCH_TIMEOUT) if tools else float(NO_TOOL_TIMEOUT)
            # ツールなしの場合はWeb検索不可をシステムプロンプトに明示し、
            # RAGデータとClaudeの知識だけで誠実に回答するよう指示する
            current_system = system if tools else (
                system + "\n\n【現在の制約】インターネット検索は現在利用できません。"
                "登録済みの地域情報（RAGデータ）とあなた自身の知識の範囲で誠実にお答えください。"
                "最新情報が必要な場合は「最新の情報はお確かめください」と一言添えてください。"
            )
            for _ in range(MAX_WEB_SEARCH_TURNS + 1):
                kwargs = dict(
                    model="claude-sonnet-4-6",
                    max_tokens=1024,
                    system=current_system,
                    messages=messages,
                    timeout=api_timeout,
                )
                if tools:
                    kwargs["tools"] = tools
                response = anthropic_client.messages.create(**kwargs)

                if response.stop_reason == "end_turn":
                    break
                if response.stop_reason == "pause_turn":
                    messages.append({"role": "assistant", "content": response.content})
                    continue
                break
            break  # 成功したのでフォールバックループを抜ける

        except Exception as e:
            # BadRequestError・APITimeoutError・その他すべての例外でフォールバック
            logging.error("tool request failed (%s), trying next fallback: %s", tools, e)
            response = None
            continue  # 次のツールセットで再試行

    if response is None:
        reply_text = "申し訳ありません。\nただいま少し混み合っています。\nしばらくしてからもう一度お試しください。"
    else:
        reply_text = next(
            (block.text for block in response.content if block.type == "text"),
            "申し訳ありません。うまく答えられませんでした。",
        )

    _save_message(user_id, "assistant", reply_text)
    return reply_text


# ── LINE イベントハンドラ ──────────────────────────────

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

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
        reply = TextSendMessage(text=f"またお会いできてうれしいです、{user['name']}さん。\n何でもお気軽にどうぞ。")
    else:
        reply = start_registration(user_id)

    line_bot_api.reply_message(event.reply_token, reply)


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

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

    # 「最初に戻る」系キーワード：履歴をリセットしてメニューを案内（Claudeを呼ばない）
    RESET_KEYWORDS = {"最初に戻る", "メニュー", "メニューに戻る", "他のことを聞く", "はじめに戻る", "トップ", "ホーム"}
    if user_message.strip() in RESET_KEYWORDS:
        conversation_histories.pop(user_id, None)
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(
                text=f"{user_info['name']}さん、何でもどうぞ。\n下のボタンをタップしてください。",
                quick_reply=_build_quick_reply(_MENU_QR_ITEMS),
            ),
        )
        return

    # 登録済みユーザーへの Claude 返答：Web 検索で 30 秒超えることがあるため
    # バックグラウンドスレッドで処理し、reply_token 失効後も届く push_message で送信
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

    # バックグラウンド処理開始前に「お待ちください」を即返信
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=f"{user_info['name']}さん、少しお待ちください。\n確認してみますね。"),
        )
    except Exception as e:
        logging.error("wait message error: %s", e)

    threading.Thread(target=_process, args=(user_id, user_message, user_info), daemon=True).start()


# ── ヘルスチェック ────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
