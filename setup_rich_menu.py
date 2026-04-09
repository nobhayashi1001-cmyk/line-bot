"""
LINE リッチメニュー セットアップスクリプト（2タブ構成・Rich Menu Alias）

使い方:
  export LINE_CHANNEL_ACCESS_TOKEN=your_token
  pip install Pillow requests
  python setup_rich_menu.py

アイコン画像の配置:
  icons/{ボタン名}.png に PNG を置くと自動で読み込まれます。
  例: icons/相談する.png, icons/探す.png, icons/ニュース.png
  ファイルがない場合はプレースホルダーが描画されます。

作成されるメニュー（計4つ）:
  free-main  : 無料会員 タブ1（タブ1アクティブ）
  free-sub   : 無料会員 タブ2（タブ2アクティブ）
  paid-main  : 有料会員 タブ1（タブ1アクティブ）
  paid-sub   : 有料会員 タブ2（タブ2アクティブ）

出力される RICH_MENU_FREE_ID / RICH_MENU_PAID_ID を
Render の環境変数に設定してください。
"""

import json
import os
import sys
from typing import Optional

import requests
from PIL import Image, ImageDraw, ImageFont

# ── 認証 ──────────────────────────────────────────────────────────
TOKEN = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
if not TOKEN:
    print("ERROR: LINE_CHANNEL_ACCESS_TOKEN が設定されていません。")
    sys.exit(1)

HEADERS_JSON = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
HEADERS_AUTH = {"Authorization": f"Bearer {TOKEN}"}

# ── 画像サイズ・レイアウト ──────────────────────────────────────────
W     = 2500          # 幅
H     = 1400          # 高さ
TAB_H = 200           # タブバーの高さ
TAB_W = W // 2        # タブ幅（2タブで等分）= 1250
BTN_H = H - TAB_H    # ボタンエリアの高さ = 1200
COL   = 3             # ボタン列数
ROW   = 2             # ボタン行数
CW    = W // COL      # ボタン列幅 = 833
CH    = BTN_H // ROW  # ボタン行高さ = 600

# ── アイコン設定 ──────────────────────────────────────────────────
ICON_DIR   = "icons"        # アイコン画像ディレクトリ
ICON_RATIO = 0.62           # ボタン高さに対するアイコンエリアの割合
ICON_SIZE  = 360            # アイコン表示サイズ (px)

# ── デザイン定数 ───────────────────────────────────────────────────
BG_COLOR           = "#F5E6A3"   # 和紙イエロー（ボタン背景）
TAB_BG             = "#8B1A1A"   # えんじ（タブバー背景）
TAB_INACTIVE_BG    = "#6B1010"   # 非アクティブタブ背景（少し暗いえんじ）
TAB_ACTIVE_BG      = "#FFFFFF"   # アクティブタブ背景（白）
TAB_ACTIVE_TEXT    = "#8B1A1A"   # アクティブタブ文字（えんじ）
TAB_INACTIVE_TEXT  = "#FFD700"   # 非アクティブタブ文字（金）
TEXT_COLOR         = "#4A2C0A"   # ボタンテキスト（濃茶）
PLACEHOLDER_BG     = "#F0DFA0"   # プレースホルダー背景
PLACEHOLDER_BORDER = "#8B6914"   # プレースホルダー枠（茶）
DIVIDER_COLOR      = "#8B6914"   # 区切り線（茶）
DIVIDER_W          = 6           # 区切り線の太さ (px)

# フォントサイズ（2500px幅基準）
FONT_BTN   = 100  # ボタンラベル（1行）
FONT_SMALL = 80   # ボタンラベル（2行）
FONT_TAB   = 90   # タブラベル
LINE_GAP   = 96   # 2行テキストの行間 (FONT_SMALL + 16)

# ── Rich Menu Alias ID（事前定義） ────────────────────────────────
ALIAS_FREE_TAB1 = "free-main"
ALIAS_FREE_TAB2 = "free-sub"
ALIAS_PAID_TAB1 = "paid-main"
ALIAS_PAID_TAB2 = "paid-sub"

TAB_LABELS = ("メイン", "ツール")

# ── ボタン定義 ────────────────────────────────────────────────────
_LIFF_BASE = "https://liff.line.me/2009711933-tXV7CqW9"

_COMMON_TAB1 = [
    ("相談する",   {"type": "message", "text": "相談する"}),
    ("探す",       {"type": "uri",     "uri":  f"{_LIFF_BASE}/map"}),
    ("知る",       {"type": "message", "text": "知る"}),
    ("つながる",   {"type": "message", "text": "つながる"}),
    ("友達に紹介", {"type": "uri",     "uri":  f"{_LIFF_BASE}/invite"}),
]

FREE_TAB1 = _COMMON_TAB1 + [
    ("会員登録", {"type": "uri", "uri": f"{_LIFF_BASE}/mypage"}),
]

PAID_TAB1 = _COMMON_TAB1 + [
    ("AI相談", {"type": "message", "text": "AIに直接相談"}),
]

TAB2_BUTTONS = [
    ("ニュース",    {"type": "uri", "uri": f"{_LIFF_BASE}/news"}),
    ("動画",        {"type": "uri", "uri": "https://www.youtube.com"}),
    ("天気",        {"type": "uri", "uri": f"{_LIFF_BASE}/weather"}),
    ("乗り換え",    {"type": "uri", "uri": "https://www.google.com/maps?travelmode=transit"}),
    ("スケジュール",{"type": "uri", "uri": f"{_LIFF_BASE}/calendar"}),
    ("旅行相談",    {"type": "uri", "uri": f"{_LIFF_BASE}/travel"}),
]


# ── フォント ──────────────────────────────────────────────────────

def _load_font(size: int) -> ImageFont.FreeTypeFont:
    paths = [
        "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _split(text: str) -> list:
    """7文字超を2行に分割する。"""
    if len(text) <= 6:
        return [text]
    mid = len(text) // 2
    return [text[:mid], text[mid:]]


# ── アイコン ──────────────────────────────────────────────────────

def _load_icon(name: str) -> Optional[Image.Image]:
    """icons/{name}.png を読み込む。なければ None を返す。"""
    icon_path = os.path.join(ICON_DIR, f"{name}.png")
    if os.path.exists(icon_path):
        try:
            return Image.open(icon_path).convert("RGBA")
        except Exception:
            pass
    return None


def _paste_icon(img: Image.Image, icon_img: Image.Image, cx: int, cy: int, size: int) -> None:
    """アイコン画像をリサイズして中央に貼り付ける。"""
    icon_resized = icon_img.resize((size, size), Image.LANCZOS)
    ix = cx - size // 2
    iy = cy - size // 2
    img.paste(icon_resized, (ix, iy), icon_resized)


def _draw_placeholder(draw: ImageDraw.Draw, cx: int, cy: int, size: int) -> None:
    """アイコンのプレースホルダー（角丸枠）を描画する。"""
    r = size // 8
    x0, y0 = cx - size // 2, cy - size // 2
    x1, y1 = cx + size // 2, cy + size // 2
    try:
        draw.rounded_rectangle([x0, y0, x1, y1], radius=r,
                                fill=PLACEHOLDER_BG, outline=PLACEHOLDER_BORDER,
                                width=DIVIDER_W)
    except AttributeError:
        draw.rectangle([x0, y0, x1, y1],
                       fill=PLACEHOLDER_BG, outline=PLACEHOLDER_BORDER,
                       width=DIVIDER_W)
    # 中央に × 印（プレースホルダー表示）
    pad = size // 5
    draw.line([(x0 + pad, y0 + pad), (x1 - pad, y1 - pad)],
              fill=PLACEHOLDER_BORDER, width=4)
    draw.line([(x1 - pad, y0 + pad), (x0 + pad, y1 - pad)],
              fill=PLACEHOLDER_BORDER, width=4)


# ── 画像生成 ──────────────────────────────────────────────────────

def make_image(buttons: list, output_path: str, active_tab: int = 1) -> str:
    """2タブバー付きリッチメニュー画像を生成する。
    active_tab=1 → タブ1が白くハイライト
    active_tab=2 → タブ2が白くハイライト
    """
    f_btn  = _load_font(FONT_BTN)
    f_sm   = _load_font(FONT_SMALL)
    f_tab  = _load_font(FONT_TAB)

    img  = Image.new("RGB", (W, H), BG_COLOR)
    draw = ImageDraw.Draw(img)

    # ── タブバー描画 ────────────────────────────────────────────
    draw.rectangle([0, 0, W - 1, TAB_H - 1], fill=TAB_BG)

    for t in (1, 2):
        x0 = 0 if t == 1 else TAB_W
        x1 = TAB_W - 1 if t == 1 else W - 1
        label = TAB_LABELS[t - 1]
        tab_cx = x0 + TAB_W // 2

        if t == active_tab:
            # アクティブ: 内側に白い角丸ボックス
            margin = 24
            try:
                draw.rounded_rectangle(
                    [x0 + margin, margin, x1 - margin, TAB_H - margin],
                    radius=28, fill=TAB_ACTIVE_BG,
                )
            except AttributeError:
                draw.rectangle(
                    [x0 + margin, margin, x1 - margin, TAB_H - margin],
                    fill=TAB_ACTIVE_BG,
                )
            draw.text((tab_cx, TAB_H // 2), label,
                      font=f_tab, fill=TAB_ACTIVE_TEXT, anchor="mm")
        else:
            # 非アクティブ: えんじ背景に金文字
            draw.rectangle([x0, 0, x1, TAB_H - 1], fill=TAB_INACTIVE_BG)
            draw.text((tab_cx, TAB_H // 2), label,
                      font=f_tab, fill=TAB_INACTIVE_TEXT, anchor="mm")

    # タブ間の縦区切り線
    draw.line([(TAB_W, 0), (TAB_W, TAB_H)], fill=TAB_INACTIVE_TEXT, width=DIVIDER_W)
    # タブバーとボタンエリアの境界線
    draw.line([(0, TAB_H), (W, TAB_H)], fill=DIVIDER_COLOR, width=DIVIDER_W * 2)

    # ── ボタンエリア区切り線 ────────────────────────────────────
    for c in range(1, COL):
        x = c * CW
        draw.line([(x, TAB_H), (x, H)], fill=DIVIDER_COLOR, width=DIVIDER_W)
    for r in range(1, ROW):
        y = TAB_H + r * CH
        draw.line([(0, y), (W, y)], fill=DIVIDER_COLOR, width=DIVIDER_W)
    draw.rectangle([0, 0, W - 1, H - 1], outline=DIVIDER_COLOR, width=DIVIDER_W)

    # ── ボタン描画 ───────────────────────────────────────────────
    icon_area_h = int(CH * ICON_RATIO)
    text_area_h = CH - icon_area_h

    for idx, (label, _) in enumerate(buttons):
        col = idx % COL
        row = idx // COL
        cx  = col * CW + CW // 2
        by  = TAB_H + row * CH

        icon_cy = by + icon_area_h // 2
        text_y  = by + icon_area_h + text_area_h // 2

        # アイコン
        icon_img = _load_icon(label)
        if icon_img:
            _paste_icon(img, icon_img, cx, icon_cy, ICON_SIZE)
        else:
            _draw_placeholder(draw, cx, icon_cy, ICON_SIZE)

        # ラベルテキスト
        lines = _split(label)
        font  = f_sm if len(lines) > 1 else f_btn
        if len(lines) == 1:
            draw.text((cx, text_y), lines[0], font=font, fill=TEXT_COLOR, anchor="mm")
        else:
            draw.text((cx, text_y - LINE_GAP // 2), lines[0],
                      font=font, fill=TEXT_COLOR, anchor="mm")
            draw.text((cx, text_y + LINE_GAP // 2), lines[1],
                      font=font, fill=TEXT_COLOR, anchor="mm")

    img.save(output_path, "JPEG", quality=95)
    print(f"  画像生成: {output_path} ({W}x{H}, tab{active_tab}アクティブ)")
    return output_path


# ── LINE API ──────────────────────────────────────────────────────

def create_rich_menu(buttons: list, menu_name: str,
                     alias_tab1: str, alias_tab2: str) -> str:
    """リッチメニューを作成して richMenuId を返す。"""
    areas = [
        # タブ1エリア → タブ1に切り替え（タブバー左半分）
        {
            "bounds": {"x": 0, "y": 0, "width": TAB_W, "height": TAB_H},
            "action": {"type": "richmenuswitch", "richMenuAliasId": alias_tab1, "data": "tab=1"},
        },
        # タブ2エリア → タブ2に切り替え（タブバー右半分）
        {
            "bounds": {"x": TAB_W, "y": 0, "width": TAB_W, "height": TAB_H},
            "action": {"type": "richmenuswitch", "richMenuAliasId": alias_tab2, "data": "tab=2"},
        },
    ]
    for idx, (_, action) in enumerate(buttons):
        col = idx % COL
        row = idx // COL
        # 最終列はピクセルの端数を吸収
        col_w = CW if col < COL - 1 else W - col * CW
        areas.append({
            "bounds": {
                "x": col * CW,
                "y": TAB_H + row * CH,
                "width": col_w,
                "height": CH,
            },
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
    if not resp.ok:
        print(f"  メニュー作成エラー ({resp.status_code}): {resp.text}")
        resp.raise_for_status()
    mid = resp.json()["richMenuId"]
    print(f"  メニュー作成: {mid} ({menu_name})")
    return mid


def upload_image(rich_menu_id: str, image_path: str) -> None:
    with open(image_path, "rb") as f:
        resp = requests.post(
            f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content",
            headers={**HEADERS_AUTH, "Content-Type": "image/jpeg"},
            data=f,
        )
    resp.raise_for_status()
    print(f"  画像アップロード完了: {rich_menu_id}")


def create_alias(alias_id: str, rich_menu_id: str) -> None:
    resp = requests.post(
        "https://api.line.me/v2/bot/richmenu/alias",
        headers=HEADERS_JSON,
        data=json.dumps({"richMenuAliasId": alias_id, "richMenuId": rich_menu_id}),
    )
    if resp.status_code not in (200, 201):
        print(f"  エイリアス作成失敗: {alias_id} → {resp.status_code} {resp.text}")
    else:
        print(f"  エイリアス作成: {alias_id} → {rich_menu_id}")


def set_default(rich_menu_id: str) -> None:
    resp = requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
        headers=HEADERS_AUTH,
    )
    resp.raise_for_status()
    print(f"  デフォルト設定: {rich_menu_id}")


def delete_existing_aliases() -> None:
    resp = requests.get(
        "https://api.line.me/v2/bot/richmenu/alias/list",
        headers=HEADERS_AUTH,
    )
    if resp.status_code != 200:
        print(f"  エイリアス一覧取得スキップ: {resp.status_code}")
        return
    for alias in resp.json().get("aliases", []):
        aid = alias["richMenuAliasId"]
        r = requests.delete(
            f"https://api.line.me/v2/bot/richmenu/alias/{aid}",
            headers=HEADERS_AUTH,
        )
        print(f"  エイリアス削除: {aid} → {r.status_code}")


def delete_existing_menus() -> None:
    resp = requests.get(
        "https://api.line.me/v2/bot/richmenu/list",
        headers=HEADERS_AUTH,
    )
    resp.raise_for_status()
    for menu in resp.json().get("richmenus", []):
        mid = menu["richMenuId"]
        requests.delete(
            f"https://api.line.me/v2/bot/richmenu/{mid}",
            headers=HEADERS_AUTH,
        )
        print(f"  メニュー削除: {mid}")


# ── メイン ────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== LINE リッチメニュー セットアップ（2タブ構成）===\n")

    # アイコンディレクトリ確認
    os.makedirs(ICON_DIR, exist_ok=True)
    icon_files = [f for f in os.listdir(ICON_DIR) if f.endswith(".png")]
    if icon_files:
        print(f"【アイコン】{len(icon_files)} 個のPNGを検出: {', '.join(icon_files)}\n")
    else:
        print("【アイコン】icons/ にPNGが見つかりません。プレースホルダーを使用します。\n")

    # ① 既存エイリアス・メニューを削除
    print("【削除】")
    delete_existing_aliases()
    delete_existing_menus()

    # ② 画像を生成（4種類）
    print("\n【画像生成】")
    img_free_t1  = make_image(FREE_TAB1,     "rich_menu_free_tab1.jpg",  active_tab=1)
    img_free_t2  = make_image(TAB2_BUTTONS,  "rich_menu_free_tab2.jpg",  active_tab=2)
    img_paid_t1  = make_image(PAID_TAB1,     "rich_menu_paid_tab1.jpg",  active_tab=1)
    img_paid_t2  = make_image(TAB2_BUTTONS,  "rich_menu_paid_tab2.jpg",  active_tab=2)

    # ③ リッチメニューを作成
    print("\n【メニュー作成】")
    id_free_t1 = create_rich_menu(FREE_TAB1,    "無料会員-タブ1", ALIAS_FREE_TAB1, ALIAS_FREE_TAB2)
    id_free_t2 = create_rich_menu(TAB2_BUTTONS, "無料会員-タブ2", ALIAS_FREE_TAB1, ALIAS_FREE_TAB2)
    id_paid_t1 = create_rich_menu(PAID_TAB1,    "有料会員-タブ1", ALIAS_PAID_TAB1, ALIAS_PAID_TAB2)
    id_paid_t2 = create_rich_menu(TAB2_BUTTONS, "有料会員-タブ2", ALIAS_PAID_TAB1, ALIAS_PAID_TAB2)

    # ④ 画像をアップロード
    print("\n【画像アップロード】")
    upload_image(id_free_t1, img_free_t1)
    upload_image(id_free_t2, img_free_t2)
    upload_image(id_paid_t1, img_paid_t1)
    upload_image(id_paid_t2, img_paid_t2)

    # ⑤ エイリアスを作成
    print("\n【エイリアス作成】")
    create_alias(ALIAS_FREE_TAB1, id_free_t1)
    create_alias(ALIAS_FREE_TAB2, id_free_t2)
    create_alias(ALIAS_PAID_TAB1, id_paid_t1)
    create_alias(ALIAS_PAID_TAB2, id_paid_t2)

    # ⑥ 無料タブ1をデフォルトに設定
    print("\n【デフォルト設定】")
    set_default(id_free_t1)

    print("\n=== 完了 ===")
    print(f"RICH_MENU_FREE_TAB1_ID={id_free_t1}")
    print(f"RICH_MENU_FREE_TAB2_ID={id_free_t2}")
    print(f"RICH_MENU_PAID_TAB1_ID={id_paid_t1}")
    print(f"RICH_MENU_PAID_TAB2_ID={id_paid_t2}")
    print("\nRender の環境変数に上記4つを設定してください。")
