from __future__ import annotations

import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import anthropic

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ユーザーごとの会話履歴（メモリ上に保持）
conversation_histories: dict[str, list[dict]] = {}

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


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

    reply_text = get_claude_reply(user_id, user_message)

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text),
    )


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
