from __future__ import annotations

import logging
import os
from flask import Flask, request, abort

logging.basicConfig(level=logging.ERROR)
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent,
    TextMessage,
    TextSendMessage,
    FollowEvent,
)
import anthropic
from supabase import create_client

app = Flask(__name__)

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

# 通常会話の履歴: {user_id: [{"role": ..., "content": ...}]}
conversation_histories: dict[str, list[dict]] = {}

# 登録フローの途中状態: {user_id: {"step": str, "name": str, "region": str}}
registration_states: dict[str, dict] = {}

SYSTEM_PROMPT = """あなたは、高齢者の生活を支える地元密着型のAIアシスタントです。

利用者は主に高齢者です。
安心して毎日を過ごせるように、やさしく、わかりやすく、丁寧に案内してください。

あなたの役割は3つです。
1. 不安をやわらげ、安心感を与える
2. 地域の生活に役立つ情報をわかりやすく伝える
3. 孤独感をやわらげる、落ち着いた話し相手になる

【話し方】
・やさしく、丁寧に話す
・否定しない、責めない、急かさない
・高齢者を子ども扱いしない
・不安をやわらげる一言を添える
・「一緒に確認しましょう」という姿勢を大切にする

【文章ルール】
・短い文で答える
・1文に1つの情報だけ入れる
・改行を多めにして読みやすくする
・必要に応じて箇条書きを使う
・専門用語はできるだけ使わない
・質問は一度に1つだけにする
・選択肢を出す時は3つ以内にする

【優先する情報】
利用者の地域で、生活に直結する情報を優先してください。
たとえば、
・地元のイベント
・公共交通
・買い物情報
・医療、介護、相談窓口
・防災情報
・ゴミ出し情報
・季節の生活アドバイス

地域情報を出す時は、
「あなたの地域では」
「この地域では」
など、生活圏に寄り添う表現を使ってください。

【わからない時】
・推測で断定しない
・情報が足りない時は、その旨をやさしく伝える
・必要なら地域名を1つだけ確認する

【対応しないこと】
医療、法律、お金、緊急対応などの専門判断はしないでください。
その場合は、
「専門の窓口に相談するのが安心です」
とやさしく案内してください。

【禁止事項】
・不安をあおる表現
・命令口調、上から目線
・高齢者を子ども扱いする表現
・相手の理解力や能力を否定する表現
・情報の詰め込みすぎ
・不確かな内容の断定

【回答の基本形】
1. 安心できる一言
2. 要点を短く答える
3. 必要なら箇条書きで整理する
4. 最後に、次の行動を選びやすい1つの質問をする

あなたは、地元に詳しい、落ち着いた「頼れる近所の案内人」として応答してください。"""

MAX_HISTORY = 20
MAX_WEB_SEARCH_TURNS = 5  # pause_turn の最大継続回数

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


# ── 登録フロー ─────────────────────────────────────────

def start_registration(user_id: str) -> str:
    registration_states[user_id] = {"step": "awaiting_name"}
    return (
        "はじめまして。\n"
        "ご利用にあたって、簡単なご登録をお願いします。\n\n"
        "まず、お名前を教えていただけますか？"
    )


def handle_registration(user_id: str, message: str) -> str:
    state = registration_states[user_id]
    step = state["step"]

    if step == "awaiting_name":
        state["name"] = message.strip()
        state["step"] = "awaiting_region"
        return (
            f"{state['name']}さん、ありがとうございます。\n\n"
            "お住まいの都道府県を教えていただけますか？\n"
            "（例：東京都、大阪府、北海道）"
        )

    if step == "awaiting_region":
        state["region"] = message.strip()
        state["step"] = "awaiting_birthdate"
        return (
            "ありがとうございます。\n\n"
            "最後に、生年月日を教えていただけますか？\n"
            "（例：1950年1月15日）"
        )

    if step == "awaiting_birthdate":
        state["birthdate"] = message.strip()
        _save_user(user_id, state)
        name = state["name"]
        del registration_states[user_id]
        return (
            f"ご登録ありがとうございました。\n"
            f"{name}さん、これからどうぞよろしくお願いします。\n\n"
            "何かお困りのことや、聞いてみたいことがあれば、\n"
            "いつでもお気軽にメッセージをどうぞ。"
        )

    return "少々お待ちください。"


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


def _save_message(user_id: str, role: str, content: str) -> None:
    try:
        get_supabase().table("messages").insert(
            {"line_user_id": user_id, "role": role, "content": content}
        ).execute()
    except Exception:
        pass  # ログ保存の失敗は返答処理に影響させない


def _is_registered(user_id: str) -> bool:
    result = (
        get_supabase().table("users")
        .select("line_user_id")
        .eq("line_user_id", user_id)
        .limit(1)
        .execute()
    )
    return len(result.data) > 0


# ── Claude 返答 ────────────────────────────────────────

def get_claude_reply(user_id: str, user_message: str) -> str:
    if user_id not in conversation_histories:
        conversation_histories[user_id] = []

    history = conversation_histories[user_id]
    history.append({"role": "user", "content": user_message})

    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
        conversation_histories[user_id] = history

    response = None

    # 三段階フォールバック:
    #   1. 最新ツール (web_search_20260209 / web_fetch_20260209)
    #   2. 旧ツール   (web_search_20250305 / web_fetch_20250910)
    #   3. ツールなし
    for tools in (WEB_SEARCH_TOOLS_V2, WEB_SEARCH_TOOLS_V1, None):
        try:
            messages = list(history)
            for _ in range(MAX_WEB_SEARCH_TURNS + 1):
                kwargs = dict(
                    model="claude-sonnet-4-6",
                    max_tokens=1024,
                    system=SYSTEM_PROMPT,
                    messages=messages,
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

        except anthropic.BadRequestError as e:
            logging.error("tool request failed (%s), trying next fallback: %s", tools, e)
            response = None
            continue  # 次のツールセットで再試行

    reply_text = next(
        (block.text for block in response.content if block.type == "text"),
        "申し訳ありません。うまく答えられませんでした。",
    )

    # 会話履歴にはテキストのみ保存（server_tool_use ブロックは不要）
    history.append({"role": "assistant", "content": reply_text})
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

    if _is_registered(user_id):
        reply = "またお会いできてうれしいです。何でもお気軽にどうぞ。"
    else:
        reply = start_registration(user_id)

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text
    reply_text = "申し訳ありません。\nただいま少し調子が悪いようです。\nしばらくしてからもう一度お試しください。"

    try:
        if user_id in registration_states:
            reply_text = handle_registration(user_id, user_message)
        elif not _is_registered(user_id):
            reply_text = start_registration(user_id)
        else:
            _save_message(user_id, "user", user_message)
            reply_text = get_claude_reply(user_id, user_message)
            _save_message(user_id, "assistant", reply_text)
    except Exception as e:
        logging.exception("handle_message error: %s", e)

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))


# ── ヘルスチェック ────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
