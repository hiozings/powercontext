"""读取站点内置的样例语料，保证服务端与前端演示用的是同一份数据。"""

import json
import re
from pathlib import Path

CORPUS_PATH = Path(__file__).resolve().parents[1] / "site" / "assets" / "corpus.js"

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def load_corpus(path: Path = CORPUS_PATH) -> dict:
    raw = path.read_text(encoding="utf-8")
    raw = _BLOCK_COMMENT.sub("", raw)
    _, _, body = raw.partition("window.XZ =")
    return json.loads(body.strip().rstrip(";"))


def chunks(data: dict):
    for doc in data["docs"]:
        for chunk in doc["chunks"]:
            yield {
                "id": f"{doc['code']}-{chunk['page']}",
                "doc_code": doc["code"],
                "doc_title": doc["title"],
                "doc_year": doc["year"],
                "doc_venue": doc["venue"],
                "doc_kind": doc["kind"],
                "tags": doc["tags"],
                "page": chunk["page"],
                "text": chunk["text"],
                "concepts": chunk["concepts"],
            }
