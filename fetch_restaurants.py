"""
藤沢市の飲食店データを Google Places API から取得して Supabase に保存するスクリプト。

使い方:
  export GOOGLE_MAPS_API_KEY=your_key
  export SUPABASE_URL=your_url
  export SUPABASE_KEY=your_key
  python3 fetch_restaurants.py
"""

import os
import sys
import time
import requests
from supabase import create_client

GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

if not GOOGLE_MAPS_API_KEY:
    print("ERROR: GOOGLE_MAPS_API_KEY が設定されていません。")
    sys.exit(1)
if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL / SUPABASE_KEY が設定されていません。")
    sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# 藤沢市内の主要エリアと座標
AREAS = [
    {"name": "藤沢駅周辺",   "lat": 35.3393, "lng": 139.4917},
    {"name": "辻堂駅周辺",   "lat": 35.3318, "lng": 139.4601},
    {"name": "片瀬江ノ島周辺", "lat": 35.3012, "lng": 139.4800},
    {"name": "湘南台駅周辺",  "lat": 35.3494, "lng": 139.4606},
    {"name": "大船駅周辺",   "lat": 35.3536, "lng": 139.5331},
]

SEARCH_RADIUS = 800   # メートル
PLACE_TYPES   = ["restaurant", "cafe", "bar"]

# Google Places APIのタイプ→日本語ジャンル対応表
TYPE_TO_GENRE = {
    "japanese_restaurant": "和食",
    "chinese_restaurant":  "中華",
    "italian_restaurant":  "イタリアン",
    "ramen_restaurant":    "ラーメン",
    "sushi_restaurant":    "寿司",
    "cafe":                "カフェ",
    "bar":                 "バー",
    "bakery":              "パン・ベーカリー",
    "restaurant":          "レストラン",
}


def _guess_genre(types: list[str]) -> str:
    for t in types:
        if t in TYPE_TO_GENRE:
            return TYPE_TO_GENRE[t]
    return "飲食店"


def _price_label(level: int | None) -> str:
    return {1: "安め", 2: "普通", 3: "やや高め", 4: "高め"}.get(level, "")


def fetch_nearby(lat: float, lng: float, place_type: str) -> list[dict]:
    """Nearby Search で1エリアのお店一覧を取得する。"""
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    results = []
    params = {
        "location": f"{lat},{lng}",
        "radius": SEARCH_RADIUS,
        "type": place_type,
        "language": "ja",
        "key": GOOGLE_MAPS_API_KEY,
    }
    while True:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("results", []))
        next_token = data.get("next_page_token")
        if not next_token:
            break
        time.sleep(2)  # next_page_token が有効になるまで待つ
        params = {"pagetoken": next_token, "key": GOOGLE_MAPS_API_KEY}
    return results


def fetch_detail(place_id: str) -> dict:
    """Place Details で電話番号・営業時間を取得する。"""
    url = "https://maps.googleapis.com/maps/api/place/details/json"
    resp = requests.get(url, params={
        "place_id": place_id,
        "fields": "formatted_phone_number,opening_hours",
        "language": "ja",
        "key": GOOGLE_MAPS_API_KEY,
    }, timeout=10)
    resp.raise_for_status()
    return resp.json().get("result", {})


def upsert_restaurant(place: dict, area_name: str) -> None:
    place_id = place["place_id"]
    name     = place.get("name", "")
    genre    = _guess_genre(place.get("types", []))
    address  = place.get("vicinity", "")
    rating   = place.get("rating")
    price_level = place.get("price_level")

    # 詳細取得（電話・営業時間）
    detail = fetch_detail(place_id)
    phone  = detail.get("formatted_phone_number", "")
    hours_list = detail.get("opening_hours", {}).get("weekday_text", [])
    hours  = "／".join(hours_list) if hours_list else ""

    price_label = _price_label(price_level)
    description = f"{genre}。{address}。"
    if rating:
        description += f"評価{rating}。"
    if price_label:
        description += f"価格帯：{price_label}。"
    if hours:
        description += f"営業時間：{hours}。"

    supabase.table("restaurants").upsert(
        {
            "place_id":    place_id,
            "name":        name,
            "genre":       genre,
            "area":        area_name,
            "address":     address,
            "phone":       phone,
            "rating":      rating,
            "price_level": price_level,
            "description": description,
            "updated_at":  "now()",
        },
        on_conflict="place_id",
    ).execute()


def main() -> None:
    total = 0
    for area in AREAS:
        print(f"\n── {area['name']} ──")
        seen = set()
        for ptype in PLACE_TYPES:
            places = fetch_nearby(area["lat"], area["lng"], ptype)
            for place in places:
                pid = place["place_id"]
                if pid in seen:
                    continue
                seen.add(pid)
                try:
                    upsert_restaurant(place, area["name"])
                    print(f"  保存: {place.get('name', '')} ({_guess_genre(place.get('types', []))})")
                    total += 1
                    time.sleep(0.1)  # API レート制限への配慮
                except Exception as e:
                    print(f"  スキップ: {place.get('name', '')} → {e}")

    print(f"\n完了。合計 {total} 件を保存しました。")


if __name__ == "__main__":
    main()
