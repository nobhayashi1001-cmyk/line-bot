from __future__ import annotations
import logging

from supabase import create_client
from config import SUPABASE_URL, SUPABASE_KEY, MAX_HISTORY

_client = None


def get_supabase():
    global _client
    if _client is None:
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


def save_message(user_id: str, role: str, content: str) -> None:
    try:
        get_supabase().table("messages").insert({
            "line_user_id": user_id,
            "role":         role,
            "content":      content,
        }).execute()
    except Exception:
        pass


def load_history(user_id: str) -> list[dict]:
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
        logging.error("load_history error: %s", e)
        return []
