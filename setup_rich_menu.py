"""
LINE リッチメニュー 登録スクリプト（シンプル4ボタン版）

使い方:
  export LINE_CHANNEL_ACCESS_TOKEN=your_token
  export LIFF_ID=your_liff_id
  pip install requests
  python setup_rich_menu.py

画像サイズ: 2500×1686px
ボタン構成: 2列×2行（4ボタン）
  左上: 🗣️話しかける（AIに相談）
  右上: 📻なつかしい昭和（なつかしい昭和）
  左下: 👥友達に紹介（友達に紹介）
  右下: 👤会員情報（/liff/mypage）

完了後、出力された RICH_MENU_ID を Render の環境変数に設定してください。
"""

import json
import os
import sys
import requests

# ── 認証 ──────────────────────────────────────────────────────────
TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
if not TOKEN:
    print("ERROR: LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")
    sys.exit(1)

LIFF_ID = os.environ.get("LIFF_ID", "")
if not LIFF_ID:
    print("ERROR: LIFF_ID が設定されていません。")
    sys.exit(1)

LIFF_BASE    = f"https://liff.line.me/{LIFF_ID}"
HEADERS_JSON = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
HEADERS_AUTH = {"Authorization": f"Bearer {TOKEN}"}

# ── 画像レイアウト定数（2500×1686） ───────────────────────────────
W     = 2500
H     = 1686
COL_W = W // 2   # 1250
ROW_H = H // 2   # 843

# ── ボタン定義（左上・右上・左下・右下） ───────────────────────────
BUTTONS = [
    # 左上
    {
        "bounds": {"x": 0,     "y": 0,     "width": COL_W, "height": ROW_H},
        "action": {"type": "message", "label": "AIに相談", "text": "AIに相談"},
    },
    # 右上
    {
        "bounds": {"x": COL_W, "y": 0,     "width": COL_W, "height": ROW_H},
        "action": {"type": "message", "label": "なつかしい昭和", "text": "なつかしい昭和"},
    },
    # 左下
    {
        "bounds": {"x": 0,     "y": ROW_H, "width": COL_W, "height": ROW_H},
        "action": {"type": "message", "label": "友達に紹介", "text": "友達に紹介"},
    },
    # 右下
    {
        "bounds": {"x": COL_W, "y": ROW_H, "width": COL_W, "height": ROW_H},
        "action": {"type": "uri", "label": "会員情報", "uri": f"{LIFF_BASE}/mypage"},
    },
]

RICH_MENU_BODY = {
    "size":        {"width": W, "height": H},
    "selected":    True,
    "name":        "地元くらしの御用聞き メインメニュー",
    "chatBarText": "メニューを開く",
    "areas":       BUTTONS,
}


def delete_existing_aliases() -> None:
    resp = requests.get("https://api.line.me/v2/bot/richmenu/alias/list", headers=HEADERS_AUTH)
    if resp.status_code != 200:
        return
    for alias in resp.json().get("aliases", []):
        aid = alias["richMenuAliasId"]
        r = requests.delete(f"https://api.line.me/v2/bot/richmenu/alias/{aid}", headers=HEADERS_AUTH)
        print(f"  エイリアス削除: {aid} ({r.status_code})")


def delete_existing_menus() -> None:
    resp = requests.get("https://api.line.me/v2/bot/richmenu/list", headers=HEADERS_AUTH)
    if not resp.ok:
        return
    for menu in resp.json().get("richmenus", []):
        mid = menu["richMenuId"]
        r = requests.delete(f"https://api.line.me/v2/bot/richmenu/{mid}", headers=HEADERS_AUTH)
        print(f"  メニュー削除: {mid} ({r.status_code})")


def create_rich_menu() -> str:
    resp = requests.post(
        "https://api.line.me/v2/bot/richmenu",
        headers=HEADERS_JSON,
        data=json.dumps(RICH_MENU_BODY, ensure_ascii=False),
    )
    if not resp.ok:
        print(f"  メニュー作成失敗 ({resp.status_code}): {resp.text}")
        resp.raise_for_status()
    mid = resp.json()["richMenuId"]
    print(f"  メニュー作成完了: {mid}")
    return mid


def upload_image(rich_menu_id: str, path: str) -> None:
    with open(path, "rb") as f:
        resp = requests.post(
            f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content",
            headers={**HEADERS_AUTH, "Content-Type": "image/jpeg"},
            data=f,
        )
    if resp.ok:
        print(f"  画像アップロード完了: {path}")
    else:
        print(f"  画像アップロード失敗 ({resp.status_code}): {resp.text}")
        resp.raise_for_status()


def set_default(rich_menu_id: str) -> None:
    resp = requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
        headers=HEADERS_AUTH,
    )
    if resp.ok:
        print(f"  デフォルト設定完了: {rich_menu_id}")
    else:
        print(f"  デフォルト設定失敗 ({resp.status_code}): {resp.text}")


if __name__ == "__main__":
    print("=== LINE リッチメニュー 登録スクリプト（4ボタン版）===\n")
    print(f"LIFF_BASE: {LIFF_BASE}\n")

    image_path = "rich_menu.jpg"
    if not os.path.exists(image_path):
        print(f"ERROR: 画像ファイルが見つかりません: {image_path}")
        print("2500×1686px の JPG 画像を rich_menu.jpg として配置してください。")
        sys.exit(1)
    print(f"画像ファイル確認: {image_path}\n")

    print("【既存メニューを削除】")
    delete_existing_aliases()
    delete_existing_menus()
    print()

    print("【メニュー作成】")
    menu_id = create_rich_menu()
    print()

    print("【画像アップロード】")
    upload_image(menu_id, image_path)
    print()

    print("【デフォルト設定（全ユーザー）】")
    set_default(menu_id)
    print()

    print("=" * 50)
    print("【完了】Render の環境変数に以下を設定してください:\n")
    print(f"RICH_MENU_ID={menu_id}")
    print("=" * 50)
    print("\n削除してよい Render 環境変数:")
    print("  RICH_MENU_FREE_TAB1_ID")
    print("  RICH_MENU_FREE_TAB2_ID")
    print("  RICH_MENU_PAID_TAB1_ID")
    print("  RICH_MENU_PAID_TAB2_ID")
