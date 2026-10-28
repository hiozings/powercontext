"""seekdb 检索层：嵌入式实例 + 单条 SQL 的向量 / 全文 / 标量混合检索。"""

import threading
from pathlib import Path

import pymysql
import pylibseekdb

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
  id VARCHAR(16) PRIMARY KEY,
  doc_code VARCHAR(8) NOT NULL,
  doc_title VARCHAR(255) NOT NULL,
  doc_year INT NOT NULL,
  doc_venue VARCHAR(128),
  doc_kind VARCHAR(32),
  tags VARCHAR(255),
  page INT NOT NULL,
  content TEXT NOT NULL,
  vec VECTOR({dim}) NOT NULL
)
"""

COLUMNS = (
    "id, doc_code, doc_title, doc_year, doc_venue, doc_kind, tags, page, content"
)


class SeekdbStore:
    def __init__(self, db_dir: Path, data: dict, embedder):
        self._lock = threading.Lock()
        self._embedder = embedder
        Path(db_dir).mkdir(parents=True, exist_ok=True)
        self._instance = pylibseekdb.open(str(db_dir))
        options = self._instance.connection_options()
        conn = pymysql.connect(database="test", autocommit=True, **options)
        with conn.cursor() as cur:
            cur.execute("CREATE DATABASE IF NOT EXISTS xunzhang")
        conn.close()
        self._conn = pymysql.connect(
            database="xunzhang",
            autocommit=True,
            cursorclass=pymysql.cursors.DictCursor,
            **options,
        )
        self._ingest(data)

    # ---------- 初始化 ----------

    def _ingest(self, data):
        from corpus import chunks

        with self._lock, self._conn.cursor() as cur:
            cur.execute(SCHEMA.format(dim=self._embedder.dim))
            cur.execute("SELECT COUNT(*) AS n FROM chunks")
            if cur.fetchone()["n"]:
                return
            cur.execute("CREATE FULLTEXT INDEX ft_content ON chunks(content) WITH PARSER ngram")
            cur.execute(
                "CREATE VECTOR INDEX vidx ON chunks(vec) WITH (distance=l2, type=hnsw)"
            )
            rows = [
                (
                    c["id"],
                    c["doc_code"],
                    c["doc_title"],
                    c["doc_year"],
                    c["doc_venue"],
                    c["doc_kind"],
                    " ".join(c["tags"]),
                    c["page"],
                    c["text"],
                    self._embedder.doc_vector(c["concepts"]),
                )
                for c in chunks(data)
            ]
            cur.executemany(
                "INSERT INTO chunks (id, doc_code, doc_title, doc_year, doc_venue,"
                " doc_kind, tags, page, content, vec) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                rows,
            )

    # ---------- 检索 ----------

    def search(self, query, query_vec, limit=5, candidates=10, keyword_limit=10,
               year_from=None, kinds=None):
        where = ["1=1"]
        if year_from:
            where.append(f"doc_year >= {int(year_from)}")
        if kinds:
            allowed = ",".join("'" + k.replace("'", "") + "'" for k in kinds)
            where.append(f"doc_kind IN ({allowed})")
        sql = f"""
        WITH cand AS (
            SELECT {COLUMNS}, l2_distance(vec, %(qv)s) AS dist
            FROM chunks
            WHERE {' AND '.join(where)}
            ORDER BY dist APPROXIMATE
            LIMIT %(cand)s
        ), kw AS (
            SELECT id, MATCH(content) AGAINST(%(q)s IN NATURAL LANGUAGE MODE) AS kwscore
            FROM chunks
            WHERE MATCH(content) AGAINST(%(q)s IN NATURAL LANGUAGE MODE) > 0
            LIMIT %(kws)s
        )
        SELECT cand.*, COALESCE(kw.kwscore, 0) AS kwscore,
               1 - cand.dist / NULLIF(MAX(cand.dist) OVER (), 0) AS sem_norm,
               COALESCE(kw.kwscore, 0) / NULLIF(MAX(COALESCE(kw.kwscore, 0)) OVER (), 0) AS kw_norm
        FROM cand LEFT JOIN kw ON cand.id = kw.id
        """
        params = {
            "qv": query_vec,
            "q": query,
            "cand": max(int(candidates), int(limit)),
            "kws": max(int(keyword_limit), int(limit)),
        }
        with self._lock, self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        for row in rows:
            row["tags"] = row["tags"].split() if row["tags"] else []
            row["dist"] = float(row["dist"])
            row["kwscore"] = float(row["kwscore"])
            row["sem_norm"] = float(row["sem_norm"] or 0.0)
            row["kw_norm"] = float(row["kw_norm"] or 0.0)
            row["chunk_id"] = row.pop("id")
        return rows

    # ---------- 元信息 ----------

    def version(self):
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT VERSION() AS v")
            return cur.fetchone()["v"]

    def count(self):
        with self._lock, self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM chunks")
            return cur.fetchone()["n"]

    def close(self):
        self._conn.close()
        self._instance.close()
