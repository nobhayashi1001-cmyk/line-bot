"""
LINE リッチメニュー 登録スクリプト（既存画像を使用）

使い方:
  export LINE_CHANNEL_ACCESS_TOKEN=your_token
  export LIFF_ID=your_liff_id          # 例: 2009711933-tXV7CqW9
  pip install requests
  python setup_rich_menu.py

登録される画像（2500×1400, プロジェクトルートに配置済み）:
  rich_menu_free_tab1.jpg  : 無料会員 タブ1（メイン）
  rich_menu_free_tab2.jpg  : 無料会員 タブ2（ツール）
  rich_menu_paid_tab1.jpg  : 有料会員 タブ1（メイン）
  rich_menu_paid_tab2.jpg  : 有料会員 タブ2（ツール）

完了後、出力された4つのIDを Render の環境変数に設定してください。
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

LIFF_BASE      = f"https://liff.line.me/{LIFF_ID}"
HEADERS_JSON   = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
HEADERS_AUTH   = {"Authorization": f"Bearer {TOKEN}"}

# ── 画像レイアウト定数（2500×1400） ───────────────────────────────
W      = 2500   # 画像幅
H      = 1400   # 画像高さ
TAB_H  = 150    # タブバー高さ（上部）
TAB_W  = W // 2 # タブ幅（メイン/ツール各 1250px）
ROWS   = 2      # ボタン行数
COLS   = 3      # ボタン列数
ROW_H  = (H - TAB_H) // ROWS  # 1行の高さ = 625
COL_W  = W // COLS             # 1列の幅  = 833

# ── Rich Menu Alias ID ─────────────────────────────────────────────
ALIAS_FREE_TAB1 = "free-main"
ALIAS_FREE_TAB2 = "free-sub"
ALIAS_PAID_TAB1 = "paid-main"
ALIAS_PAID_TAB2 = "paid-sub"

# ── ボタン定義 ────────────────────────────────────────────────────
# 形式: {"label": "...", "action": {...LINE action...}}

FREE_TAB1_BUTTONS = [
    # Row1
    {"label": "相談する",   "action": {"type": "message", "text": "相談する"}},
    {"label": "探す",       "action": {"type": "message", "text": "探す"}},
    {"label": "知る",       "action": {"type": "message", "text": "知る"}},
    # Row2
    {"label": "つながる",   "action": {"type": "message", "text": "つながる"}},
    {"label": "友達に紹介", "action": {"type": "uri",     "uri": f"{LIFF_BASE}/invite"}},
    {"label": "会員登録",   "action": {"type": "uri",     "uri": f"{LIFF_BASE}/mypage"}},
]

PAID_TAB1_BUTTONS = [
    # Row1（Free と同じ）
    {"label": "相談する",   "action": {"type": "message", "text": "相談する"}},
    {"label": "探す",       "action": {"type": "message", "text": "探す"}},
    {"label": "知る",       "action": {"type": "message", "text": "知る"}},
    # Row2
    {"label": "つながる",   "action": {"type": "message", "text": "つながる"}},
    {"label": "友達に紹介", "action": {"type": "uri",     "uri": f"{LIFF_BASE}/invite"}},
    {"label": "AI相談",     "action": {"type": "message", "text": "AIに相談"}},  # 有料のみ
]

TAB2_BUTTONS = [
    # Row1
    {"label": "ニュース",     "action": {"type": "uri",     "uri": f"{LIFF_BASE}/today"}},
    {"label": "動画",         "action": {"type": "message", "text": "動画・音楽"}},
    {"label": "天気",         "action": {"type": "uri",     "uri": f"{LIFF_BASE}/today"}},
    # Row2
    {"label": "乗り換え",     "action": {"type": "uri",     "uri": "https://transit.yahoo.co.jp/"}},
    {"label": "スケジュール", "action": {"type": "uri",     "uri": f"{LIFF_BASE}/calendar"}},
    {"label": "旅行相談",     "action": {"type": "message", "text": "旅行提案"}},
]


# ── ヘルパー関数 ──────────────────────────────────────────────────

def _button_areas(buttons: list, alias_self: str, alias_other: str) -> list:
    """ボタン定義リストから LINE API の areas 配列を生成する。"""
    areas = [
        # タブ切り替え（左：自分、右：相手に切替）
        {
            "bounds": {"x": 0, "y": 0, "width": TAB_W, "height": TAB_H},
            "action": {"type": "richmenuswitch", "richMenuAliasId": alias_self, "data": "tab=1"},
        },
        {
            "bounds": {"x": TAB_W, "y": 0, "width": TAB_W, "height": TAB_H},
            "action": {"type": "richmenuswitch", "richMenuAliasId": alias_other, "data": "tab=2"},
        },
    ]
    for idx, btn in enumerate(buttons):
        row = idx // COLS
        col = idx % COLS
        # 最終列は端数を吸収（2500 = 833×3 + 1 → 最終列 834）
        col_w = COL_W if col < COLS - 1 else W - col * COL_W
        row_h = ROW_H if row < ROWS - 1 else H - TAB_H - row * ROW_H
        areas.append({
            "bounds": {
                "x": col * COL_W,
                "y": TAB_H + row * ROW_H,
                "width": col_w,
                "height": row_h,
            },
            "action": btn["action"],
        })
    return areas


def create_rich_menu(name: str, buttons: list, alias_self: str, alias_other: str) -> str:
    """リッチメニューを作成して richMenuId を返す。"""
    body = {
        "size":        {"width": W, "height": H},
        "selected":    True,
        "name":        name,
        "chatBarText": "メニューを開く",
        "areas":       _button_areas(buttons, alias_self, alias_other),
    }
    resp = requests.post(
        "https://api.line.me/v2/bot/richmenu",
        headers=HEADERS_JSON,
        data=json.dumps(body, ensure_ascii=False),
    )
    if not resp.ok:
        print(f"  メニュー作成失敗 ({resp.status_code}): {resp.text}")
        resp.raise_for_status()
    mid = resp.json()["richMenuId"]
    print(f"  作成完了: {mid}  ({name})")
    return mid


def upload_image(rich_menu_id: str, path: str) -> None:
    """画像をアップロードする。"""
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


def create_alias(alias_id: str, rich_menu_id: str) -> None:
    """Rich Menu Alias を作成する。"""
    resp = requests.post(
        "https://api.line.me/v2/bot/richmenu/alias",
        headers=HEADERS_JSON,
        data=json.dumps({"richMenuAliasId": alias_id, "richMenuId": rich_menu_id}),
    )
    if resp.status_code in (200, 201):
        print(f"  エイリアス作成: {alias_id}")
    else:
        print(f"  エイリアス作成失敗: {alias_id} → {resp.status_code} {resp.text}")


def set_default(rich_menu_id: str) -> None:
    """全ユーザーのデフォルトメニューを設定する。"""
    resp = requests.post(
        f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}",
        headers=HEADERS_AUTH,
    )
    if resp.ok:
        print(f"  デフォルト設定完了: {rich_menu_id}")
    else:
        print(f"  デフォルト設定失敗 ({resp.status_code}): {resp.text}")


def delete_existing_aliases() -> None:
    """既存エイリアスをすべて削除する（再実行時の衝突回避）。"""
    resp = requests.get("https://api.line.me/v2/bot/richmenu/alias/list", headers=HEADERS_AUTH)
    if resp.status_code != 200:
        return
    for alias in resp.json().get("aliases", []):
        aid = alias["richMenuAliasId"]
        r = requests.delete(f"https://api.line.me/v2/bot/richmenu/alias/{aid}", headers=HEADERS_AUTH)
        print(f"  エイリアス削除: {aid} ({r.status_code})")


def delete_existing_menus() -> None:
    """既存リッチメニューをすべて削除する（再実行時の衝突回避）。"""
    resp = requests.get("https://api.line.me/v2/bot/richmenu/list", headers=HEADERS_AUTH)
    if not resp.ok:
        return
    for menu in resp.json().get("richmenus", []):
        mid = menu["richMenuId"]
        r = requests.delete(f"https://api.line.me/v2/bot/richmenu/{mid}", headers=HEADERS_AUTH)
        print(f"  メニュー削除: {mid} ({r.status_code})")


# ── メイン ────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== LINE リッチメニュー 登録スクリプト ===\n")
    print(f"LIFF_BASE: {LIFF_BASE}\n")

    # 画像ファイルの存在確認
    images = {
        "free-tab1": "rich_menu_free_tab1.jpg",
        "free-tab2": "rich_menu_free_tab2.jpg",
        "paid-tab1": "rich_menu_paid_tab1.jpg",
        "paid-tab2": "rich_menu_paid_tab2.jpg",
    }
    for key, path in images.items():
        if not os.path.exists(path):
            print(f"ERROR: 画像ファイルが見つかりません: {path}")
            sys.exit(1)
    print("画像ファイル: すべて確認済み\n")

    # ① 既存のエイリアス・メニューを削除（再実行時の衝突回避）
    print("【既存メニューを削除】")
    delete_existing_aliases()
    delete_existing_menus()
    print()

    # ② リッチメニューを作成
    print("【メニュー作成】")
    id_free_t1 = create_rich_menu("無料会員-タブ1（メイン）", FREE_TAB1_BUTTONS, ALIAS_FREE_TAB1, ALIAS_FREE_TAB2)
    id_free_t2 = create_rich_menu("無料会員-タブ2（ツール）", TAB2_BUTTONS,       ALIAS_FREE_TAB1, ALIAS_FREE_TAB2)
    id_paid_t1 = create_rich_menu("有料会員-タブ1（メイン）", PAID_TAB1_BUTTONS, ALIAS_PAID_TAB1, ALIAS_PAID_TAB2)
    id_paid_t2 = create_rich_menu("有料会員-タブ2（ツール）", TAB2_BUTTONS,       ALIAS_PAID_TAB1, ALIAS_PAID_TAB2)
    print()

    # ③ 画像をアップロード
    print("【画像アップロード】")
    upload_image(id_free_t1, images["free-tab1"])
    upload_image(id_free_t2, images["free-tab2"])
    upload_image(id_paid_t1, images["paid-tab1"])
    upload_image(id_paid_t2, images["paid-tab2"])
    print()

    # ④ エイリアスを作成（タブ切り替えに必要）
    print("【エイリアス作成】")
    create_alias(ALIAS_FREE_TAB1, id_free_t1)
    create_alias(ALIAS_FREE_TAB2, id_free_t2)
    create_alias(ALIAS_PAID_TAB1, id_paid_t1)
    create_alias(ALIAS_PAID_TAB2, id_paid_t2)
    print()

    # ⑤ 無料タブ1をデフォルトに設定（既存ユーザー全員に適用）
    print("【デフォルト設定】")
    set_default(id_free_t1)
    print()

    # ⑥ 結果を出力
    print("=" * 50)
    print("【完了】Render の環境変数に以下を設定してください:\n")
    print(f"RICH_MENU_FREE_TAB1_ID={id_free_t1}")
    print(f"RICH_MENU_FREE_TAB2_ID={id_free_t2}")
    print(f"RICH_MENU_PAID_TAB1_ID={id_paid_t1}")
    print(f"RICH_MENU_PAID_TAB2_ID={id_paid_t2}")
    print("=" * 50)
