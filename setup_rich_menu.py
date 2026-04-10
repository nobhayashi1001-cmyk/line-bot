"""
LINE リッチメニュー セットアップスクリプト（2タブ構成・Rich Menu Alias）

使い方:
  export LINE_CHANNEL_ACCESS_TOKEN=your_token
  pip install Pillow requests
  python setup_rich_menu.py

アイコン画像の配置:
  icons/{アイコン名}.png に PNG を置くと自動で読み込まれます。
  ファイルがない場合は昭和レトロ風プレースホルダーを自動生成します。

作成されるメニュー（計4つ）:
  free-main  : 無料会員 タブ1（タブ1アクティブ）
  free-sub   : 無料会員 タブ2（タブ2アクティブ）
  paid-main  : 有料会員 タブ1（タブ1アクティブ）
  paid-sub   : 有料会員 タブ2（タブ2アクティブ）

出力される RICH_MENU_FREE_TAB1_ID 等を
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
W        = 2500          # 幅
AI_BAR_H = 200           # 下部「AIに聞く」バーの高さ
H        = 1686          # 高さ（TAB_H + BTN_H + AI_BAR_H）
TAB_H    = 200           # タブバーの高さ
TAB_W    = W // 2        # タブ幅（2タブで等分）= 1250
BTN_H    = H - TAB_H - AI_BAR_H  # ボタンエリアの高さ = 1286
COL      = 2             # ボタン列数
ROW      = 3             # ボタン行数
CW       = W // COL      # ボタン列幅 = 1250
CH       = BTN_H // ROW  # ボタン行高さ ≈ 428

# ── アイコン設定 ──────────────────────────────────────────────────
ICON_DIR   = "icons"        # アイコン画像ディレクトリ
ICON_RATIO = 0.60           # ボタン高さに対するアイコンエリアの割合
ICON_SIZE  = 240            # アイコン表示サイズ (px)

# ── デザイン定数 ───────────────────────────────────────────────────
BG_COLOR           = "#F5E6A3"   # 和紙イエロー（ボタン背景）
TAB_BG             = "#8B1A1A"   # えんじ（タブバー背景）
TAB_INACTIVE_BG    = "#6B1010"   # 非アクティブタブ背景
TAB_ACTIVE_BG      = "#FFFFFF"   # アクティブタブ背景（白）
TAB_ACTIVE_TEXT    = "#8B1A1A"   # アクティブタブ文字（えんじ）
TAB_INACTIVE_TEXT  = "#FFD700"   # 非アクティブタブ文字（金）
TEXT_COLOR         = "#4A2C0A"   # ボタンテキスト（濃茶）
PLACEHOLDER_BG     = "#F0DFA0"   # プレースホルダー背景
PLACEHOLDER_BORDER = "#8B6914"   # プレースホルダー枠（茶）
DIVIDER_COLOR      = "#8B6914"   # 区切り線（茶）
DIVIDER_W          = 6           # 区切り線の太さ (px)

# フォントサイズ（2500px幅・3行2列基準）
FONT_BTN   = 80   # ボタンラベル（1行）
FONT_SMALL = 65   # ボタンラベル（2行）
FONT_TAB   = 90   # タブラベル
FONT_AI    = 80   # AIバーラベル
LINE_GAP   = 76   # 2行テキストの行間 (FONT_SMALL + 11)

# ── Rich Menu Alias ID（事前定義） ────────────────────────────────
ALIAS_FREE_TAB1 = "free-main"
ALIAS_FREE_TAB2 = "free-sub"
ALIAS_PAID_TAB1 = "paid-main"
ALIAS_PAID_TAB2 = "paid-sub"

TAB_LABELS = ("今日を生きる", "人生を楽しむ")

# ── ボタン定義 ────────────────────────────────────────────────────
# 各ボタン: (表示ラベル, アクション, アイコンファイル名(拡張子なし))
_LIFF_BASE = "https://liff.line.me/2009711933-tXV7CqW9"

_COMMON_TAB1 = [
    ("⛅ 今日の情報",   {"type": "uri",     "uri": f"{_LIFF_BASE}/today"},   "ニュース"),
    ("🏥 健康・からだ", {"type": "message", "text": "健康相談"},              "健康"),
    ("🍳 食事・レシピ", {"type": "message", "text": "食事レシピ"},            "レシピ"),
    ("🗺️ 外出・移動",  {"type": "uri",     "uri": f"{_LIFF_BASE}/map"},      "乗り換え"),
    ("👥 友達に紹介",   {"type": "uri",     "uri": f"{_LIFF_BASE}/invite"},   "友達に紹介"),
]

FREE_TAB1 = [
    ("🤖 AIに何でも相談", {"type": "message", "text": "AIに相談"}, "AI相談"),
] + _COMMON_TAB1

PAID_TAB1 = [
    ("✨ AIに直接相談", {"type": "message", "text": "AIに直接相談"}, "AI相談"),
] + _COMMON_TAB1

TAB2_BUTTONS = [
    ("📻 なつかしい昭和", {"type": "message", "text": "なつかしい昭和"}, "なつかしい昭和"),
    ("✈️ 旅行・お出かけ", {"type": "message", "text": "旅行提案"},       "旅行"),
    ("🎬 動画・音楽",     {"type": "uri",     "uri": "https://www.youtube.com"}, "動画"),
    ("💡 趣味・生きがい", {"type": "message", "text": "趣味生きがい"},   "趣味"),
    ("🗓️ スケジュール",   {"type": "uri",     "uri": f"{_LIFF_BASE}/calendar"}, "スケジュール"),
    ("⚙️ 会員情報・設定", {"type": "uri",     "uri": f"{_LIFF_BASE}/mypage"},   "会員登録"),
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
    pad = size // 5
    draw.line([(x0 + pad, y0 + pad), (x1 - pad, y1 - pad)],
              fill=PLACEHOLDER_BORDER, width=4)
    draw.line([(x1 - pad, y0 + pad), (x0 + pad, y1 - pad)],
              fill=PLACEHOLDER_BORDER, width=4)


# ── 不足アイコン自動生成 ──────────────────────────────────────────

_ICON_SPECS = {
    "健康": {
        "bg": "#D4EDDA", "border": "#28A745",
        "symbol_color": "#28A745",
        "label": "健康",
    },
    "レシピ": {
        "bg": "#FFF3CD", "border": "#FD7E14",
        "symbol_color": "#FD7E14",
        "label": "レシピ",
    },
    "なつかしい昭和": {
        "bg": "#F5DEB3", "border": "#8B6914",
        "symbol_color": "#8B1A1A",
        "label": "昭和",
    },
    "旅行": {
        "bg": "#CCE5FF", "border": "#004085",
        "symbol_color": "#004085",
        "label": "旅行",
    },
    "趣味": {
        "bg": "#E2D9F3", "border": "#6F42C1",
        "symbol_color": "#6F42C1",
        "label": "趣味",
    },
}


def _generate_icon(name: str) -> None:
    """昭和レトロ風の不足アイコンを自動生成して icons/{name}.png に保存する。"""
    spec = _ICON_SPECS.get(name)
    if spec is None:
        return

    size = 512
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 角丸の背景
    margin = 24
    radius = 80
    try:
        draw.rounded_rectangle(
            [margin, margin, size - margin, size - margin],
            radius=radius,
            fill=spec["bg"],
            outline=spec["border"],
            width=12,
        )
    except AttributeError:
        draw.rectangle(
            [margin, margin, size - margin, size - margin],
            fill=spec["bg"],
            outline=spec["border"],
            width=12,
        )

    # 中央にアイコン固有の図形を描く
    cx, cy = size // 2, size // 2
    s_color = spec["symbol_color"]

    if name == "健康":
        # 赤十字風プラス記号
        arm = 90
        thick = 36
        draw.rectangle([cx - thick, cy - arm, cx + thick, cy + arm], fill=s_color)
        draw.rectangle([cx - arm, cy - thick, cx + arm, cy + thick], fill=s_color)

    elif name == "レシピ":
        # フォーク＋皿の簡略形 → 楕円の皿
        draw.ellipse([cx - 90, cy - 20, cx + 90, cy + 80], outline=s_color, width=14)
        # フォーク（縦棒2本）
        for ox in (-30, 30):
            draw.rectangle([cx + ox - 6, cy - 110, cx + ox + 6, cy - 30], fill=s_color)
        draw.rectangle([cx - 36, cy - 30, cx + 36, cy - 18], fill=s_color)

    elif name == "なつかしい昭和":
        # ラジオ風 - 長方形の筐体
        draw.rectangle([cx - 100, cy - 70, cx + 100, cy + 80], outline=s_color, width=14, fill=spec["bg"])
        # スピーカー格子
        for oy in range(cy - 50, cy + 60, 20):
            draw.line([cx - 80, oy, cx - 10, oy], fill=s_color, width=6)
        # ダイヤル円
        draw.ellipse([cx + 10, cy - 40, cx + 80, cy + 30], outline=s_color, width=10)
        # アンテナ
        draw.line([cx + 70, cy - 70, cx + 90, cy - 150], fill=s_color, width=10)

    elif name == "旅行":
        # 飛行機の簡略形
        draw.polygon([
            (cx, cy - 100), (cx + 120, cy + 30), (cx + 60, cy + 20),
            (cx + 40, cy + 80), (cx, cy + 50), (cx - 40, cy + 80),
            (cx - 60, cy + 20), (cx - 120, cy + 30),
        ], fill=s_color)

    elif name == "趣味":
        # 5角形の星
        import math
        star_r_outer = 100
        star_r_inner = 44
        points = []
        for i in range(10):
            angle = math.radians(-90 + i * 36)
            r = star_r_outer if i % 2 == 0 else star_r_inner
            points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
        draw.polygon(points, fill=s_color)

    # ラベルテキスト（下部）
    font = _load_font(72)
    draw.text((cx, size - margin - 50), spec["label"],
              font=font, fill=s_color, anchor="mm")

    out_path = os.path.join(ICON_DIR, f"{name}.png")
    img.save(out_path, "PNG")
    print(f"  アイコン自動生成: {out_path}")


def _ensure_icons(button_sets: list) -> None:
    """不足しているアイコンを自動生成する。"""
    needed = set()
    for buttons in button_sets:
        for _, _, icon_name in buttons:
            needed.add(icon_name)

    for name in sorted(needed):
        path = os.path.join(ICON_DIR, f"{name}.png")
        if not os.path.exists(path):
            if name in _ICON_SPECS:
                _generate_icon(name)
            else:
                print(f"  アイコン不足（自動生成スペックなし）: {name}.png")


# ── 画像生成 ──────────────────────────────────────────────────────

def make_image(buttons: list, output_path: str, active_tab: int = 1) -> str:
    """2タブバー付きリッチメニュー画像を生成する。
    buttons: [(label, action, icon_name), ...]
    active_tab=1 → タブ1が白くハイライト
    active_tab=2 → タブ2が白くハイライト
    """
    f_btn  = _load_font(FONT_BTN)
    f_sm   = _load_font(FONT_SMALL)
    f_tab  = _load_font(FONT_TAB)
    f_ai   = _load_font(FONT_AI)

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
        draw.line([(x, TAB_H), (x, H - AI_BAR_H)], fill=DIVIDER_COLOR, width=DIVIDER_W)
    for r in range(1, ROW):
        y = TAB_H + r * CH
        draw.line([(0, y), (W, y)], fill=DIVIDER_COLOR, width=DIVIDER_W)
    draw.rectangle([0, 0, W - 1, H - 1], outline=DIVIDER_COLOR, width=DIVIDER_W)

    # ── ボタン描画 ───────────────────────────────────────────────
    icon_area_h = int(CH * ICON_RATIO)
    text_area_h = CH - icon_area_h

    for idx, (label, _, icon_name) in enumerate(buttons):
        col = idx % COL
        row = idx // COL
        cx  = col * CW + CW // 2
        by  = TAB_H + row * CH

        icon_cy = by + icon_area_h // 2
        text_y  = by + icon_area_h + text_area_h // 2

        # アイコン
        icon_img = _load_icon(icon_name)
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

    # ── AIに聞くバー（下部固定・全タブ共通） ────────────────────────
    ai_y = TAB_H + BTN_H
    draw.rectangle([0, ai_y, W - 1, H - 1], fill="#FFFDE7")
    draw.line([(0, ai_y), (W, ai_y)], fill=DIVIDER_COLOR, width=DIVIDER_W * 2)
    draw.rectangle([0, H - DIVIDER_W], [W - 1, H - 1], fill=DIVIDER_COLOR)
    pad_v  = 36
    pad_h  = 48
    send_w = 180
    fx0 = pad_h
    fy0 = ai_y + pad_v
    fx1 = W - pad_h - send_w - 24
    fy1 = H - pad_v
    try:
        draw.rounded_rectangle([fx0, fy0, fx1, fy1], radius=70,
                               fill="#FFFFFF", outline=DIVIDER_COLOR, width=5)
    except AttributeError:
        draw.rectangle([fx0, fy0, fx1, fy1],
                       fill="#FFFFFF", outline=DIVIDER_COLOR, width=5)
    draw.text((fx0 + 60, (fy0 + fy1) // 2),
              "AIに聞く・・・",
              font=f_ai, fill="#AAAAAA", anchor="lm")
    sx0 = fx1 + 24
    sx1 = W - pad_h
    try:
        draw.rounded_rectangle([sx0, fy0, sx1, fy1], radius=70, fill="#8B1A1A")
    except AttributeError:
        draw.rectangle([sx0, fy0, sx1, fy1], fill="#8B1A1A")
    draw.text(((sx0 + sx1) // 2, (fy0 + fy1) // 2),
              "▶", font=f_ai, fill="#FFD700", anchor="mm")

    img.save(output_path, "JPEG", quality=95)
    print(f"  画像生成: {output_path} ({W}x{H}, tab{active_tab}アクティブ)")
    return output_path


# ── LINE API ──────────────────────────────────────────────────────

def create_rich_menu(buttons: list, menu_name: str,
                     alias_tab1: str, alias_tab2: str) -> str:
    """リッチメニューを作成して richMenuId を返す。
    buttons: [(label, action, icon_name), ...]
    """
    areas = [
        {
            "bounds": {"x": 0, "y": 0, "width": TAB_W, "height": TAB_H},
            "action": {"type": "richmenuswitch", "richMenuAliasId": alias_tab1, "data": "tab=1"},
        },
        {
            "bounds": {"x": TAB_W, "y": 0, "width": TAB_W, "height": TAB_H},
            "action": {"type": "richmenuswitch", "richMenuAliasId": alias_tab2, "data": "tab=2"},
        },
    ]
    for idx, (_, action, _icon) in enumerate(buttons):
        col = idx % COL
        row = idx // COL
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
    # AIに聞くバー（下部・横幅フル）
    areas.append({
        "bounds": {
            "x": 0,
            "y": TAB_H + BTN_H,
            "width": W,
            "height": AI_BAR_H,
        },
        "action": {"type": "message", "text": "AIに聞く"},
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
    print("=== LINE リッチメニュー セットアップ（2タブ・3行2列）===\n")

    os.makedirs(ICON_DIR, exist_ok=True)

    # ① 不足アイコンを自動生成
    print("【アイコン確認・生成】")
    _ensure_icons([FREE_TAB1, TAB2_BUTTONS])

    icon_files = [f for f in os.listdir(ICON_DIR) if f.endswith(".png")]
    print(f"  合計 {len(icon_files)} 個のPNG: {', '.join(icon_files)}\n")

    # ② 既存エイリアス・メニューを削除
    print("【削除】")
    delete_existing_aliases()
    delete_existing_menus()

    # ③ 画像を生成（4種類）
    print("\n【画像生成】")
    img_free_t1  = make_image(FREE_TAB1,     "rich_menu_free_tab1.jpg",  active_tab=1)
    img_free_t2  = make_image(TAB2_BUTTONS,  "rich_menu_free_tab2.jpg",  active_tab=2)
    img_paid_t1  = make_image(PAID_TAB1,     "rich_menu_paid_tab1.jpg",  active_tab=1)
    img_paid_t2  = make_image(TAB2_BUTTONS,  "rich_menu_paid_tab2.jpg",  active_tab=2)

    # ④ リッチメニューを作成
    print("\n【メニュー作成】")
    id_free_t1 = create_rich_menu(FREE_TAB1,    "無料会員-タブ1", ALIAS_FREE_TAB1, ALIAS_FREE_TAB2)
    id_free_t2 = create_rich_menu(TAB2_BUTTONS, "無料会員-タブ2", ALIAS_FREE_TAB1, ALIAS_FREE_TAB2)
    id_paid_t1 = create_rich_menu(PAID_TAB1,    "有料会員-タブ1", ALIAS_PAID_TAB1, ALIAS_PAID_TAB2)
    id_paid_t2 = create_rich_menu(TAB2_BUTTONS, "有料会員-タブ2", ALIAS_PAID_TAB1, ALIAS_PAID_TAB2)

    # ⑤ 画像をアップロード
    print("\n【画像アップロード】")
    upload_image(id_free_t1, img_free_t1)
    upload_image(id_free_t2, img_free_t2)
    upload_image(id_paid_t1, img_paid_t1)
    upload_image(id_paid_t2, img_paid_t2)

    # ⑥ エイリアスを作成
    print("\n【エイリアス作成】")
    create_alias(ALIAS_FREE_TAB1, id_free_t1)
    create_alias(ALIAS_FREE_TAB2, id_free_t2)
    create_alias(ALIAS_PAID_TAB1, id_paid_t1)
    create_alias(ALIAS_PAID_TAB2, id_paid_t2)

    # ⑦ 無料タブ1をデフォルトに設定
    print("\n【デフォルト設定】")
    set_default(id_free_t1)

    print("\n=== 完了 ===")
    print(f"RICH_MENU_FREE_TAB1_ID={id_free_t1}")
    print(f"RICH_MENU_FREE_TAB2_ID={id_free_t2}")
    print(f"RICH_MENU_PAID_TAB1_ID={id_paid_t1}")
    print(f"RICH_MENU_PAID_TAB2_ID={id_paid_t2}")
    print("\nRender の環境変数に上記4つを設定してください。")
