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
COL, ROW = 2, 3           # 2列×3行
CW, CH = W // COL, H // ROW  # 1250 × 562

# ── ボタン定義（2列×3行・6ボタン）──────────────────────────────
# (ラベル, 背景色, 送信メッセージ)
_COMMON = [
    ("相談する",   "#4A90D9", "相談する"),
    ("探す",       "#E8734A", "探す"),
    ("知る",       "#5BAD6F", "知る"),
    ("つながる",   "#8B6BB1", "つながる"),
    ("友達に紹介", "#D95B7A", "友達に紹介"),
]

FREE_BUTTONS = _COMMON + [
    ("会員登録", "#C8A000", "会員登録"),
]

PAID_BUTTONS = _COMMON + [
    ("AIに直接相談", "#C8A000", "AIに直接相談"),
]


def _load_fonts():
    """システムフォントを取得する。なければデフォルトフォントを返す。"""
    paths = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    ]
    for path in paths:
        try:
            large = ImageFont.truetype(path, 160)
            small = ImageFont.truetype(path, 130)
            return large, small
        except Exception:
            continue
    default = ImageFont.load_default()
    return default, default


def _split_label(label: str) -> list[str]:
    """6文字超のラベルを2行に分割する。"""
    if len(label) <= 5:
        return [label]
    if "・" in label:
        idx = label.index("・") + 1
        return [label[:idx], label[idx:]]
    mid = len(label) // 2
    return [label[:mid], label[mid:]]


def make_image(buttons: list, output_path: str) -> str:
    """ボタン定義からリッチメニュー画像を生成し、パスを返す。"""
    font_large, font_small = _load_fonts()
    img = Image.new("RGB", (W, H), "#FFFFFF")
    draw = ImageDraw.Draw(img)

    for idx, (label, bg, _) in enumerate(buttons):
        col = idx % COL
        row = idx // COL
        x0, y0 = col * CW, row * CH
        x1, y1 = x0 + CW, y0 + CH

        # 背景・枠線
        draw.rectangle([x0, y0, x1, y1], fill=bg)
        draw.rectangle([x0, y0, x1, y1], outline="#FFFFFF", width=6)

        cx = x0 + CW // 2
        cy = y0 + CH // 2

        lines = _split_label(label)
        font = font_small if len(lines) > 1 else font_large
        line_h = CH // 4

        if len(lines) == 1:
            draw.text((cx, cy), lines[0], font=font, fill="#FFFFFF", anchor="mm")
        else:
            draw.text((cx, cy - line_h // 2), lines[0], font=font, fill="#FFFFFF", anchor="mm")
            draw.text((cx, cy + line_h // 2), lines[1], font=font, fill="#FFFFFF", anchor="mm")

    img.save(output_path, "JPEG", quality=95)
    print(f"画像を生成しました: {output_path} ({W}x{H})")
    return output_path


def create_rich_menu(buttons: list, menu_name: str) -> str:
    """リッチメニューを LINE API に登録し、richMenuId を返す。"""
    areas = []
    for idx, (_, _, msg) in enumerate(buttons):
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
    resp = requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    resp.raise_for_status()
    print(f"デフォルトリッチメニューに設定: {rich_menu_id}")


def delete_existing() -> None:
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

    free_img = make_image(FREE_BUTTONS, "rich_menu_free.jpg")
    free_id = create_rich_menu(FREE_BUTTONS, "無料会員メニュー")
    upload_image(free_id, free_img)
    set_default(free_id)

    paid_img = make_image(PAID_BUTTONS, "rich_menu_paid.jpg")
    paid_id = create_rich_menu(PAID_BUTTONS, "有料会員メニュー")
    upload_image(paid_id, paid_img)

    print("\n=== 完了 ===")
    print(f"RICH_MENU_FREE_ID={free_id}")
    print(f"RICH_MENU_PAID_ID={paid_id}")
    print("\nRender の環境変数に上記2つを設定してください。")
