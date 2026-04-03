"""
朝の定期プッシュ通知スクリプト
毎朝7:00 JST（22:00 UTC）にRender Cron Jobから実行される。
全ユーザーに登録地域の天気情報を添えたメッセージを送る。
"""
from __future__ import annotations

import logging
import os
import sys
from collections import defaultdict

import httpx
from linebot import LineBotApi
from linebot.models import TextSendMessage
from supabase import create_client

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
SUPABASE_URL              = os.environ["SUPABASE_URL"]
SUPABASE_KEY              = os.environ["SUPABASE_KEY"]

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
supabase     = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── WMO天気コード → (説明テキスト, 絵文字, 傘が必要か, 雪・霧など特記) ──────

_WMO: dict[int, tuple[str, str, bool, str]] = {
    0:  ("快晴",         "☀️",  False, ""),
    1:  ("おおむね晴れ", "🌤️", False, ""),
    2:  ("晴れ時々くもり","⛅", False, ""),
    3:  ("くもり",       "☁️",  False, ""),
    45: ("霧",           "🌫️", False, "fog"),
    48: ("霧（霧氷）",   "🌫️", False, "fog"),
    51: ("霧雨",         "🌦️", True,  ""),
    53: ("霧雨",         "🌦️", True,  ""),
    55: ("霧雨",         "🌦️", True,  ""),
    61: ("小雨",         "🌧️", True,  ""),
    63: ("雨",           "🌧️", True,  ""),
    65: ("大雨",         "🌧️", True,  ""),
    71: ("小雪",         "❄️",  False, "snow"),
    73: ("雪",           "❄️",  False, "snow"),
    75: ("大雪",         "❄️",  False, "snow"),
    77: ("みぞれ",       "🌨️", True,  "snow"),
    80: ("にわか雨",     "🌦️", True,  ""),
    81: ("にわか雨",     "🌦️", True,  ""),
    82: ("激しいにわか雨","🌧️",True,  ""),
    85: ("にわか雪",     "🌨️", False, "snow"),
    86: ("大雪",         "🌨️", False, "snow"),
    95: ("雷雨",         "⛈️",  True,  "thunder"),
    96: ("雷雨",         "⛈️",  True,  "thunder"),
    99: ("激しい雷雨",   "⛈️",  True,  "thunder"),
}


def _wmo_info(code: int) -> tuple[str, str, bool, str]:
    """WMOコードを (説明, 絵文字, 傘, 特記) に変換。未知コードはフォールバック。"""
    if code in _WMO:
        return _WMO[code]
    # 範囲でざっくり分類
    if code <= 3:
        return ("晴れ", "☀️", False, "")
    if code <= 48:
        return ("霧・もや", "🌫️", False, "fog")
    if code <= 67:
        return ("雨", "🌧️", True, "")
    if code <= 77:
        return ("雪", "❄️", False, "snow")
    if code <= 82:
        return ("にわか雨", "🌦️", True, "")
    return ("雷雨", "⛈️", True, "thunder")


# ── 天気API ────────────────────────────────────────────

def geocode_region(region: str) -> tuple[float, float] | None:
    """地名を緯度経度に変換する。失敗時は None を返す。"""
    try:
        r = httpx.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": region, "language": "ja", "count": 1},
            timeout=10,
        )
        results = r.json().get("results")
        if not results:
            logging.warning("geocode: no result for '%s'", region)
            return None
        return results[0]["latitude"], results[0]["longitude"]
    except Exception as e:
        logging.error("geocode error for '%s': %s", region, e)
        return None


def get_weather(lat: float, lon: float) -> dict | None:
    """緯度経度から今日の天気情報を取得する。失敗時は None を返す。"""
    try:
        r = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude":  lat,
                "longitude": lon,
                "daily": "weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                "timezone": "Asia/Tokyo",
                "forecast_days": 1,
            },
            timeout=10,
        )
        daily = r.json().get("daily", {})
        return {
            "code":       int(daily["weathercode"][0]),
            "temp_max":   int(daily["temperature_2m_max"][0]),
            "temp_min":   int(daily["temperature_2m_min"][0]),
            "rain_prob":  int(daily["precipitation_probability_max"][0]),
        }
    except Exception as e:
        logging.error("weather API error (%.4f, %.4f): %s", lat, lon, e)
        return None


# ── メッセージ生成 ──────────────────────────────────────

def build_message(name: str, region: str, weather: dict) -> str:
    desc, emoji, needs_umbrella, special = _wmo_info(weather["code"])
    temp_max  = weather["temp_max"]
    temp_min  = weather["temp_min"]
    rain_prob = weather["rain_prob"]

    lines: list[str] = [
        f"{name}さん、おはようございます。",
        "",
        f"今日の{region}のお天気です。",
        "",
        f"{emoji} {desc}",
        f"最高気温：{temp_max}℃ / 最低気温：{temp_min}℃",
    ]

    # 雨の確率
    if rain_prob >= 50:
        lines.append(f"傘マーク：雨の可能性 {rain_prob}%")
    elif rain_prob >= 30:
        lines.append(f"傘マーク：雨の可能性 {rain_prob}%（念のため折りたたみ傘をどうぞ）")

    # ワンポイントアドバイス
    lines.append("")
    if special == "thunder":
        lines.append("雷雨の予報が出ています。\n外出はできるだけお控えください。")
    elif special == "snow":
        lines.append("雪が降りそうです。\n足元が滑りやすくなりますのでお気をつけください。")
    elif special == "fog":
        lines.append("霧が出やすい天気です。\n車や自転車の運転にはお気をつけください。")
    elif needs_umbrella:
        lines.append("傘をお持ちになると安心です。")
    elif temp_max >= 30:
        lines.append("暑くなりそうです。\n水分補給をこまめにどうぞ。")
    elif temp_max <= 5:
        lines.append("寒くなりそうです。\n暖かい格好でお出かけください。")
    else:
        lines.append("今日も無理せず、良い一日をお過ごしください。")

    return "\n".join(lines)


def build_fallback_message(name: str) -> str:
    """天気情報が取得できなかった場合のメッセージ。"""
    return (
        f"{name}さん、おはようございます。\n\n"
        "今日も一日、どうぞお元気でお過ごしください。\n"
        "何かお困りのことがあればいつでもどうぞ。"
    )


# ── Supabase ───────────────────────────────────────────

def get_all_users() -> list[dict]:
    """全ユーザーを取得する。失敗時は例外を上げる。"""
    result = supabase.table("users").select("line_user_id, name, region").execute()
    return result.data or []


# ── LINE push ──────────────────────────────────────────

def send_push(user_id: str, text: str) -> bool:
    try:
        line_bot_api.push_message(user_id, TextSendMessage(text=text))
        return True
    except Exception as e:
        logging.error("push_message failed for %s: %s", user_id, e)
        return False


# ── メイン ────────────────────────────────────────────

def main() -> None:
    logging.info("morning push: start")

    try:
        users = get_all_users()
    except Exception as e:
        logging.error("failed to get users: %s", e)
        sys.exit(1)

    if not users:
        logging.info("morning push: no users, exiting")
        return

    # region でグルーピング（同じ地域は1回だけジオコーディング）
    by_region: dict[str, list[dict]] = defaultdict(list)
    for u in users:
        by_region[u.get("region") or ""].append(u)

    ok_count   = 0
    fail_count = 0

    for region, region_users in by_region.items():
        weather = None
        if region:
            coords = geocode_region(region)
            if coords:
                weather = get_weather(*coords)

        for user in region_users:
            name    = user["name"]
            uid     = user["line_user_id"]
            text    = (
                build_message(name, region, weather)
                if weather
                else build_fallback_message(name)
            )
            if send_push(uid, text):
                ok_count += 1
            else:
                fail_count += 1

    logging.info("morning push: done. ok=%d fail=%d", ok_count, fail_count)


if __name__ == "__main__":
    main()
