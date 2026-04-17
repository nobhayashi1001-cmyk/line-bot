from __future__ import annotations
import logging
import threading

import anthropic
from linebot.models import TextSendMessage

from config import ANTHROPIC_API_KEY, MODEL, MAX_HISTORY, API_TIMEOUT, SYSTEM_PROMPT
import db

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def handle_message(event, line_bot_api) -> None:
    user_id = event.source.user_id
    text    = event.message.text.strip()

    def _process():
        history = db.load_history(user_id)
        history.append({"role": "user", "content": text})
        db.save_message(user_id, "user", text)

        if len(history) > MAX_HISTORY:
            history = history[-MAX_HISTORY:]

        try:
            response = _client.messages.create(
                model=MODEL,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=history,
                timeout=API_TIMEOUT,
            )
            reply = next(
                (b.text for b in response.content if b.type == "text"),
                "申し訳ありません。もう一度お試しください。",
            )
        except Exception as e:
            logging.error("Claude API error: %s", e)
            reply = "申し訳ありません。\nただいま少し混み合っています。\nしばらくしてからもう一度お試しください。"

        db.save_message(user_id, "assistant", reply)
        try:
            line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        except Exception:
            line_bot_api.push_message(user_id, TextSendMessage(text=reply))

    threading.Thread(target=_process, daemon=True).start()
