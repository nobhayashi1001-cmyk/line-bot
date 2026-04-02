from __future__ import annotations

import os
from flask import Flask, request, abort
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

SYSTEM_PROMPT = """あなたは高齢者の方々に寄り添う、やさしいアシスタントです。

以下のルールを必ず守ってください：
- 難しい言葉や専門用語は使わず、わかりやすい言葉で話してください
- 文章は短く、読みやすくしてください
- 丁寧で温かみのある言葉遣いをしてください（「〜ですね」「〜ましょう」など）
- 漢字にはできるだけふりがなをつけず、平易な漢字を使ってください
- 絵文字は使わず、落ち着いたトーンで話してください
- 相手の話をしっかり聞いて、共感の言葉を添えてください
- 一度にたくさんの情報を伝えず、要点をしぼって話してください
- 返答は3〜5文程度にまとめてください"""

MAX_HISTORY = 20


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

    response = anthropic_client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=SYSTEM_PROMPT,
        messages=history,
    )

    reply_text = response.content[0].text
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

    if user_id in registration_states:
        reply_text = handle_registration(user_id, user_message)
    elif not _is_registered(user_id):
        reply_text = start_registration(user_id)
    else:
        reply_text = get_claude_reply(user_id, user_message)

    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply_text))


# ── ヘルスチェック ────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
