"""
FAQ の embedding を一括生成して Supabase に保存するスクリプト

使い方:
  python vectorize_faq.py           # embedding が NULL の行だけ処理
  python vectorize_faq.py --all     # 全行を強制再生成

事前準備:
  1. Supabase SQL Editor で以下を実行済みであること
       CREATE EXTENSION IF NOT EXISTS vector;
       ALTER TABLE faq ADD COLUMN IF NOT EXISTS embedding vector(1536);
  2. .env に OPENAI_API_KEY / SUPABASE_URL / SUPABASE_KEY を設定

モデル: text-embedding-3-small (1536次元)
バッチ: 100件ずつ処理（レートリミット対策）
"""

import argparse
import os
import sys
import time
from pathlib import Path

from supabase import create_client
import openai

# .env を手動で読み込む（python-dotenv 不要）
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _key, _, _val = _line.partition("=")
            os.environ.setdefault(_key.strip(), _val.strip())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

for var, name in [(SUPABASE_URL, "SUPABASE_URL"), (SUPABASE_KEY, "SUPABASE_KEY"), (OPENAI_API_KEY, "OPENAI_API_KEY")]:
    if not var:
        print(f"ERROR: {name} が設定されていません。.env に追加してください。")
        sys.exit(1)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
openai_client = openai.OpenAI(api_key=OPENAI_API_KEY)

BATCH_SIZE = 100
EMBED_MODEL = "text-embedding-3-small"


def fetch_rows(force_all: bool) -> list[dict]:
    """処理対象の FAQ 行を取得する。force_all=False なら embedding が NULL のみ。"""
    q = supabase.table("faq").select("id, question")
    if not force_all:
        q = q.is_("embedding", "null")
    result = q.execute()
    return result.data or []


def embed_batch(texts: list[str]) -> list[list[float]]:
    """OpenAI に texts を送り、embedding のリストを返す。"""
    resp = openai_client.embeddings.create(model=EMBED_MODEL, input=texts)
    return [item.embedding for item in resp.data]


def update_embeddings(rows: list[dict]) -> None:
    """rows の embedding を OpenAI で生成して Supabase に保存する。"""
    total = len(rows)
    processed = 0

    for i in range(0, total, BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        texts = [row["question"] for row in batch]

        try:
            embeddings = embed_batch(texts)
        except openai.RateLimitError:
            print(f"  レートリミット。60秒待機します...")
            time.sleep(60)
            embeddings = embed_batch(texts)

        for row, embedding in zip(batch, embeddings):
            supabase.table("faq").update({"embedding": embedding}).eq("id", row["id"]).execute()

        processed += len(batch)
        print(f"  {processed}/{total} 完了")

        # バッチ間で短い待機（レートリミット対策）
        if i + BATCH_SIZE < total:
            time.sleep(0.5)


def main() -> None:
    parser = argparse.ArgumentParser(description="FAQ embedding 生成スクリプト")
    parser.add_argument("--all", action="store_true", help="全件を強制再生成する")
    args = parser.parse_args()

    print(f"対象: {'全件' if args.all else 'embeddingがNULLの行のみ'}")
    rows = fetch_rows(force_all=args.all)

    if not rows:
        print("処理対象の行がありません。")
        return

    print(f"{len(rows)} 件を処理します（{BATCH_SIZE}件バッチ）")
    update_embeddings(rows)
    print("完了しました。")


if __name__ == "__main__":
    main()
