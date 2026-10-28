"""检索编排：seekdb 召回 + PowerContext 记忆加权。"""

import re
import time
from pathlib import Path

from corpus import load_corpus
from embedding import build_embedder
from memory import PowerContextMemory
from store import SeekdbStore

FOCUS_TAGS = ["切块", "边界", "评估", "召回"]
SEM_WEIGHT = 0.55
KW_WEIGHT = 0.45
TAG_BOOST = 0.02
RECALL_BOOST = 0.03
BOOST_CAP = 0.09

_HIT_CODE = re.compile(r"\b([A-E]) p\.(\d+)")


class RetrievalService:
    def __init__(self, db_dir: Path):
        data = load_corpus()
        self.profile = data["profile"]
        self.expansions = data["expansions"]
        self.embedder = build_embedder(data)
        self.store = SeekdbStore(db_dir, data, self.embedder)
        self.memory = PowerContextMemory()
        self._profile_seeded = False
        self._ensure_memory_profile()

    def _ensure_memory_profile(self):
        if self._profile_seeded or not self.memory.reachable():
            return
        self.memory.ensure_profile(self.profile)
        self._profile_seeded = True

    def expand(self, query):
        query_l = query.lower()
        terms = []
        for key, values in self.expansions.items():
            if key in query_l:
                terms.extend(values)
        return list(dict.fromkeys(terms))

    def search(self, query, use_memory=True, limit=5, year_from=None, kinds=None):
        started = time.perf_counter()
        if use_memory:
            self._ensure_memory_profile()
        expanded = self.expand(query) if use_memory else []
        keyword_query = " ".join([query, *expanded])
        query_vec = self.embedder.query_vector(query, expanded)
        rows = self.store.search(
            keyword_query, query_vec, limit=limit, year_from=year_from, kinds=kinds
        )

        recalled = self.memory.search(query, limit=5) if use_memory else []
        recalled_ids = {
            f"{code}-{page}"
            for text in recalled
            for code, page in _HIT_CODE.findall(text)
        }

        for row in rows:
            tag_hits = sum(1 for tag in FOCUS_TAGS if tag in row["tags"]) if use_memory else 0
            tag_boost = min(TAG_BOOST * tag_hits, BOOST_CAP)
            recall_boost = RECALL_BOOST if row["chunk_id"] in recalled_ids else 0.0
            boost = min(tag_boost + recall_boost, BOOST_CAP)
            row["boost"] = round(boost, 4)
            row["boost_detail"] = {
                "tag": round(tag_boost, 4),
                "recall": round(recall_boost, 4),
            }
            row["score"] = round(
                min(1.0, SEM_WEIGHT * row["sem_norm"] + KW_WEIGHT * row["kw_norm"] + boost), 4
            )
        rows.sort(key=lambda r: r["score"], reverse=True)
        results = rows[:limit]

        memory_state = {
            "enabled": bool(use_memory),
            "source": "powercontext" if (use_memory and self.memory.reachable()) else (
                "local-profile" if use_memory else "off"
            ),
            "scope_id": self.memory.ensure_scope() if use_memory else None,
            "expansions": expanded,
            "recalled": recalled[:3],
            "profile": self.profile if use_memory else None,
        }
        stats = {
            "candidates": len(rows),
            "keyword_hits": sum(1 for r in rows if r["kwscore"] > 0),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "sem_weight": SEM_WEIGHT,
            "kw_weight": KW_WEIGHT,
        }

        if use_memory and results:
            self._remember_search(query, results)

        return {"query": query, "memory": memory_state, "stats": stats, "results": results}

    def _remember_search(self, query, results):
        top = "、".join(f"{r['doc_code']} p.{r['page']}" for r in results[:3])
        self.memory.remember(f"检索「{query}」→ 命中 {top}", kind="search", reason="寻章检索记录")

    def memory_entries(self):
        entries = [
            {"text": e.get("text", ""), "kind": e.get("kind", "")}
            for e in self.memory.entries()
            if e.get("text")
        ]
        status = self.memory.status()
        return {
            "source": "powercontext" if status["reachable"] else "local-profile",
            "scope_id": status["scope_id"],
            "entries": entries,
            "profile": self.profile,
        }

    def health(self):
        return {
            "engine": "seekdb",
            "version": self.store.version(),
            "chunks": self.store.count(),
            "embedder": self.embedder.name,
            "memory": self.memory.status(),
        }
