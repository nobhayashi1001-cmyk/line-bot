"""
LINE リッチメニュー セットアップスクリプト（らくらくスマートフォン風デザイン）

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

# リッチメニュー画像サイズ（2/3サイズ: 2500×1124）
W, H = 2500, 1124
COL, ROW = 2, 3           # 2列×3行
CW, CH = W // COL, H // ROW  # 1250 × 374

# デザイン定数
BG_COLOR    = "#8B1A1A"   # 濃いえんじ色
WHITE       = "#FFFFFF"
DIVIDER_W   = 4           # 区切り線の太さ (px)
# フォントサイズ（画像座標系: 2500px幅基準）
FONT_SIZE_LG = 148        # 1行ラベル用
FONT_SIZE_SM = 118        # 2行ラベル用

# ── ボタン定義（2列×3行・6ボタン）──────────────────────────────
# (ラベル, ※未使用カラー, アクション)
# ラベル形式: "絵文字 テキスト"（スペース区切り）

_LIFF_BASE = "https://liff.line.me/2009711933-tXV7CqW9"

_COMMON = [
    ("相談する",   "#4A90D9", {"type": "message", "text": "相談する"}),
    ("探す",       "#E8734A", {"type": "uri",     "uri":  f"{_LIFF_BASE}/map"}),
    ("知る",       "#5BAD6F", {"type": "message", "text": "知る"}),
    ("つながる",   "#8B6BB1", {"type": "message", "text": "つながる"}),
    ("友達に紹介", "#D95B7A", {"type": "uri",     "uri":  f"{_LIFF_BASE}/invite"}),
]

FREE_BUTTONS = _COMMON + [
    ("会員登録", "#C8A000", {"type": "uri", "uri": f"{_LIFF_BASE}/mypage"}),
]

PAID_BUTTONS = _COMMON + [
    ("AI相談", "#C8A000", {"type": "message", "text": "AIに直接相談"}),
]


# ── フォント読み込み ─────────────────────────────────────────────

def _load_jp_font(size: int) -> ImageFont.FreeTypeFont:
    """日本語対応フォントを読み込む。"""
    paths = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    ]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()



def _split_text(text: str) -> list[str]:
    """5文字超のテキストを2行に分割する。"""
    if len(text) <= 5:
        return [text]
    mid = len(text) // 2
    return [text[:mid], text[mid:]]


# ── 画像生成 ─────────────────────────────────────────────────────

def make_image(buttons: list, output_path: str) -> str:
    """らくらくスマートフォン風リッチメニュー画像を生成し、パスを返す。"""
    font_lg = _load_jp_font(FONT_SIZE_LG)
    font_sm = _load_jp_font(FONT_SIZE_SM)

    img  = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # ── 区切り線（行・列） ────────────────────────────────────────
    for r in range(1, ROW):
        y = r * CH
        draw.line([(0, y), (W, y)], fill=WHITE, width=DIVIDER_W)
    for c in range(1, COL):
        x = c * CW
        draw.line([(x, 0), (x, H)], fill=WHITE, width=DIVIDER_W)
    # 外枠
    draw.rectangle([0, 0, W - 1, H - 1], outline=WHITE, width=DIVIDER_W)

    # ── 各ボタン描画（テキスト中央配置） ─────────────────────────
    for idx, (label, _, _) in enumerate(buttons):
        col = idx % COL
        row = idx // COL
        cx  = col * CW + CW // 2
        cy  = row * CH + CH // 2

        lines    = _split_text(label)
        font_txt = font_sm if len(lines) > 1 else font_lg
        line_gap = FONT_SIZE_SM + 20

        if len(lines) == 1:
            draw.text((cx, cy), lines[0], font=font_txt, fill=WHITE, anchor="mm")
        else:
            draw.text((cx, cy - line_gap // 2), lines[0], font=font_txt, fill=WHITE, anchor="mm")
            draw.text((cx, cy + line_gap // 2), lines[1], font=font_txt, fill=WHITE, anchor="mm")

    img.save(output_path, "JPEG", quality=95)
    print(f"画像を生成しました: {output_path} ({W}x{H})")
    return output_path


# ── LINE API 操作 ────────────────────────────────────────────────

def create_rich_menu(buttons: list, menu_name: str) -> str:
    """リッチメニューを LINE API に登録し、richMenuId を返す。"""
    areas = []
    for idx, (_, _, action) in enumerate(buttons):
        col = idx % COL
        row = idx // COL
        areas.append({
            "bounds": {"x": col * CW, "y": row * CH, "width": CW, "height": CH},
            "action": action,
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
    free_id  = create_rich_menu(FREE_BUTTONS, "無料会員メニュー")
    upload_image(free_id, free_img)
    set_default(free_id)

    paid_img = make_image(PAID_BUTTONS, "rich_menu_paid.jpg")
    paid_id  = create_rich_menu(PAID_BUTTONS, "有料会員メニュー")
    upload_image(paid_id, paid_img)

    print("\n=== 完了 ===")
    print(f"RICH_MENU_FREE_ID={free_id}")
    print(f"RICH_MENU_PAID_ID={paid_id}")
    print("\nRender の環境変数に上記2つを設定してください。")
