"""
LINE リッチメニュー 登録スクリプト（4ボタン版 - 画像自動生成）

使い方:
  export LINE_CHANNEL_ACCESS_TOKEN=your_token
  export LIFF_ID=your_liff_id
  pip install requests pillow
  python setup_rich_menu.py

画像を自動生成してアップロードします。rich_menu.jpg は不要です。

ボタン構成（2500×1686px、2列×2行）:
  左上: 🗣️ 話しかける    → メッセージ「AIに相談」
  右上: 📻 なつかしい昭和 → メッセージ「なつかしい昭和」
  左下: 👥 友達に紹介    → LIFF /liff/invite
  右下: 👤 会員情報      → LIFF /liff/mypage

完了後、出力された RICH_MENU_ID を Render の環境変数に設定してください。
"""

import io
import json
import os
import sys

import requests
from PIL import Image, ImageDraw, ImageFont

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

# ── 画像レイアウト定数（2500×1686） ─────────────────────────────────
W     = 2500
H     = 1686
COL_W = W // 2   # 1250（クリックエリア用）
ROW_H = H // 2   # 843

# デザイン定数
BG_COLOR      = "#06C755"   # LINE グリーン（背景・余白）
BTN_COLOR     = "#FFFFFF"   # ボタン背景（白）
TEXT_COLOR    = "#111111"   # テキスト色
RADIUS        = 40          # 角丸
MARGIN        = 28          # 外余白
GAP           = 20          # ボタン間隔
EMOJI_SIZE    = 160         # 絵文字フォントサイズ
LABEL_SIZE    = 72          # ラベルフォントサイズ

# フォントパス（macOS）
FONT_JP    = "/System/Library/Fonts/ヒラギノ角ゴシック W7.ttc"
FONT_EMOJI = "/System/Library/Fonts/Apple Color Emoji.ttc"

# ── ボタン定義 ─────────────────────────────────────────────────────
BUTTON_DEFS = [
    {"emoji": "🗣️", "label": "話しかける",    "row": 0, "col": 0},
    {"emoji": "📻",  "label": "なつかしい昭和", "row": 0, "col": 1},
    {"emoji": "👥",  "label": "友達に紹介",     "row": 1, "col": 0},
    {"emoji": "👤",  "label": "会員情報",       "row": 1, "col": 1},
]

# LINE API クリックエリア定義
AREAS = [
    {
        "bounds": {"x": 0,     "y": 0,     "width": COL_W, "height": ROW_H},
        "action": {"type": "message", "label": "AIに相談",      "text": "AIに相談"},
    },
    {
        "bounds": {"x": COL_W, "y": 0,     "width": COL_W, "height": ROW_H},
        "action": {"type": "message", "label": "なつかしい昭和", "text": "なつかしい昭和"},
    },
    {
        "bounds": {"x": 0,     "y": ROW_H, "width": COL_W, "height": ROW_H},
        "action": {"type": "uri", "label": "友達に紹介", "uri": f"{LIFF_BASE}/invite"},
    },
    {
        "bounds": {"x": COL_W, "y": ROW_H, "width": COL_W, "height": ROW_H},
        "action": {"type": "uri", "label": "会員情報",   "uri": f"{LIFF_BASE}/mypage"},
    },
]

RICH_MENU_BODY = {
    "size":        {"width": W, "height": H},
    "selected":    True,
    "name":        "地元くらしの御用聞き メインメニュー",
    "chatBarText": "メニューを開く",
    "areas":       AREAS,
}


# ── 画像生成 ─────────────────────────────────────────────────────────

def _load_font(path: str, size: int):
    if os.path.exists(path):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _draw_centered_text(draw, font, text, cx, cy, fill, embedded_color=False):
    """テキストをbboxで中央揃えして描画する。"""
    bbox = draw.textbbox((0, 0), text, font=font, embedded_color=embedded_color)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = cx - tw // 2 - bbox[0]
    y = cy - th // 2 - bbox[1]
    draw.text((x, y), text, font=font, fill=fill, embedded_color=embedded_color)


def generate_image() -> bytes:
    """リッチメニュー画像を生成してJPEGバイト列で返す。"""
    img = Image.new("RGBA", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    font_emoji = _load_font(FONT_EMOJI, EMOJI_SIZE)
    font_label = _load_font(FONT_JP,    LABEL_SIZE)

    btn_w = (W - MARGIN * 2 - GAP) // 2   # 1 ボタンの幅
    btn_h = (H - MARGIN * 2 - GAP) // 2   # 1 ボタンの高さ

    for btn in BUTTON_DEFS:
        x0 = MARGIN + btn["col"] * (btn_w + GAP)
        y0 = MARGIN + btn["row"] * (btn_h + GAP)
        x1 = x0 + btn_w
        y1 = y0 + btn_h

        # ボタン背景（角丸白）
        draw.rounded_rectangle([x0, y0, x1, y1], radius=RADIUS, fill=BTN_COLOR)

        # ボタン中心
        cx = (x0 + x1) // 2
        cy = (y0 + y1) // 2

        # 絵文字（上側）
        emoji_cy = cy - LABEL_SIZE // 2 - 20
        try:
            _draw_centered_text(draw, font_emoji, btn["emoji"], cx, emoji_cy,
                                 fill=(0, 0, 0), embedded_color=True)
        except Exception:
            # embedded_color 非対応の場合はモノクロ描画にフォールバック
            _draw_centered_text(draw, font_emoji, btn["emoji"], cx, emoji_cy,
                                 fill=TEXT_COLOR)

        # ラベル（下側）
        label_cy = cy + EMOJI_SIZE // 2 - 10
        _draw_centered_text(draw, font_label, btn["label"], cx, label_cy, fill=TEXT_COLOR)

    # RGBA → RGB に変換してJPEG出力
    rgb = Image.new("RGB", img.size, BG_COLOR)
    rgb.paste(img, mask=img.split()[3])
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=95)
    print(f"  画像生成完了: {W}×{H}px")
    return buf.getvalue()


# ── LINE API ─────────────────────────────────────────────────────────

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


def upload_image_bytes(rich_menu_id: str, image_bytes: bytes) -> None:
    resp = requests.post(
        f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content",
        headers={**HEADERS_AUTH, "Content-Type": "image/jpeg"},
        data=image_bytes,
    )
    if resp.ok:
        print(f"  画像アップロード完了")
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


# ── メイン ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== LINE リッチメニュー 登録スクリプト（4ボタン版）===\n")
    print(f"LIFF_BASE: {LIFF_BASE}\n")

    print("【画像生成】")
    image_bytes = generate_image()
    # ローカルにも保存（確認用）
    with open("rich_menu_preview.jpg", "wb") as f:
        f.write(image_bytes)
    print("  プレビュー保存: rich_menu_preview.jpg\n")

    print("【既存メニューを削除】")
    delete_existing_aliases()
    delete_existing_menus()
    print()

    print("【メニュー作成】")
    menu_id = create_rich_menu()
    print()

    print("【画像アップロード】")
    upload_image_bytes(menu_id, image_bytes)
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
