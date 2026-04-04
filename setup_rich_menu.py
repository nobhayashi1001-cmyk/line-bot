"""
LINE リッチメニュー セットアップスクリプト

使い方:
  export LINE_CHANNEL_ACCESS_TOKEN=your_token
  pip install Pillow requests
  python setup_rich_menu.py

実行後に表示される RICH_MENU_FREE_ID / RICH_MENU_PAID_ID を
Render の環境変数に設定してください。
"""

import json
import os
import sys

import requests
from PIL import Image, ImageDraw, ImageFont

# ── 設定 ────────────────────────────────────────────────────────────
TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
if not TOKEN:
    print("ERROR: LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")
    sys.exit(1)

HEADERS_JSON = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json",
}

# リッチメニュー画像サイズ（LINE 推奨: 2500×1686）
W, H = 2500, 1686
COL, ROW = 2, 3          # 2列×3行
CW, CH = W // COL, H // ROW  # 1250 × 562

# ── ボタン定義 ──────────────────────────────────────────────────────
# (アイコン, ラベル, 背景色, 送信メッセージ)
_COMMON_BUTTONS = [
    ("📰", "地元情報",     "#4A90D9", "地元情報"),
    ("🍽️", "食事・レシピ", "#E8734A", "食事・レシピ"),
    ("🏥", "健康",         "#5BAD6F", "健康"),
    ("🚶", "運動",         "#E8A84A", "運動"),
]

FREE_BUTTONS = _COMMON_BUTTONS + [
    ("💬", "相談",         "#8B6BB1", "相談"),
    ("🎁", "友達に紹介",   "#D95B7A", "友達に紹介"),
]

PAID_BUTTONS = _COMMON_BUTTONS + [
    ("✨", "AIに直接相談", "#D4AF37", "AIに直接相談"),
    ("🎁", "友達に紹介",   "#D95B7A", "友達に紹介"),
]


def _load_fonts():
    """システムフォントを取得する。なければデフォルトフォントを返す。"""
    paths = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    ]
    for path in paths:
        try:
            return (
                ImageFont.truetype(path, 110),
                ImageFont.truetype(path, 80),
            )
        except Exception:
            continue
    default = ImageFont.load_default()
    return default, default


def make_image(buttons: list, output_path: str) -> str:
    """ボタン定義からリッチメニュー画像を生成し、パスを返す。"""
    font_icon, font_label = _load_fonts()
    img = Image.new("RGB", (W, H), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    for idx, (icon, label, bg, _) in enumerate(buttons):
        col = idx % COL
        row = idx // COL
        x0, y0 = col * CW, row * CH
        x1, y1 = x0 + CW, y0 + CH

        draw.rectangle([x0, y0, x1, y1], fill=bg)
        draw.rectangle([x0, y0, x1, y1], outline="#FFFFFF", width=6)

        cx = x0 + CW // 2
        cy = y0 + CH // 2 - 50
        draw.text((cx, cy), icon, font=font_icon, fill="#FFFFFF", anchor="mm")
        draw.text((cx, cy + 140), label, font=font_label, fill="#FFFFFF", anchor="mm")

    img.save(output_path, "JPEG", quality=95)
    print(f"画像を生成しました: {output_path} ({W}x{H})")
    return output_path


def create_rich_menu(buttons: list, menu_name: str) -> str:
    """リッチメニューを LINE API に登録し、richMenuId を返す。"""
    areas = []
    for idx, (_, _, _, msg) in enumerate(buttons):
        col = idx % COL
        row = idx // COL
        areas.append({
            "bounds": {"x": col * CW, "y": row * CH, "width": CW, "height": CH},
            "action": {"type": "message", "text": msg},
        })

    body = {
        "size": {"width": W, "height": H},
        "selected": True,
        "name": menu_name,
        "chatBarText": "メニューを開く",
        "areas": areas,
    }

    resp = requests.post(
        "https://api.line.me/v2/bot/richmenu",
        headers=HEADERS_JSON,
        data=json.dumps(body),
    )
    resp.raise_for_status()
    rich_menu_id = resp.json()["richMenuId"]
    print(f"リッチメニュー作成: {rich_menu_id} ({menu_name})")
    return rich_menu_id


def upload_image(rich_menu_id: str, image_path: str) -> None:
    """リッチメニューに画像をアップロードする。"""
    with open(image_path, "rb") as f:
        resp = requests.post(
            f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content",
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-Type": "image/jpeg",
            },
            data=f,
        )
    resp.raise_for_status()
    print(f"画像アップロード完了: {rich_menu_id}")


def set_default(rich_menu_id: str) -> None:
    """指定リッチメニューをデフォルト（全ユーザー）に設定する。"""
    resp = requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    resp.raise_for_status()
    print(f"デフォルトリッチメニューに設定: {rich_menu_id}")


def delete_existing() -> None:
    """既存のリッチメニューをすべて削除する。"""
    resp = requests.get(
        "https://api.line.me/v2/bot/richmenu/list",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    resp.raise_for_status()
    for menu in resp.json().get("richmenus", []):
        mid = menu["richMenuId"]
        requests.delete(
            f"https://api.line.me/v2/bot/richmenu/{mid}",
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        print(f"既存メニューを削除: {mid}")


if __name__ == "__main__":
    print("=== LINE リッチメニュー セットアップ ===\n")
    delete_existing()

    # 無料会員メニュー
    free_img = make_image(FREE_BUTTONS, "rich_menu_free.jpg")
    free_id = create_rich_menu(FREE_BUTTONS, "無料会員メニュー")
    upload_image(free_id, free_img)
    set_default(free_id)  # デフォルトは無料メニュー

    # 有料会員メニュー
    paid_img = make_image(PAID_BUTTONS, "rich_menu_paid.jpg")
    paid_id = create_rich_menu(PAID_BUTTONS, "有料会員メニュー")
    upload_image(paid_id, paid_img)

    print("\n=== 完了 ===")
    print(f"RICH_MENU_FREE_ID={free_id}")
    print(f"RICH_MENU_PAID_ID={paid_id}")
    print("\nRender の環境変数に上記2つを設定してください。")
