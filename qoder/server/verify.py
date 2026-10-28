"""验证样例：同一条流水线在「关闭记忆 / 开启记忆 / 重复检索」下的可复现结果。

用法：
    ./venv/bin/python verify.py            # 用临时 seekdb 目录与全新 PowerContext scope
    ./venv/bin/python verify.py --keep-db  # 复用 data/seekdb

退出码 0 表示全部断言通过。
"""

import argparse
import os
import sys
import tempfile
import uuid
from pathlib import Path

QUERIES = [
    "长文档切块边界怎么处理",
    "RAG 评估看哪些指标",
    "向量召回和关键词召回怎么融合",
    "组会说的 baseline 是什么",
]
REPEAT_QUERY = QUERIES[3]


def fmt(results):
    return " / ".join(f"{r['doc_code']} p.{r['page']}" for r in results)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-db", action="store_true")
    parser.add_argument("--scope-key", default=None)
    args = parser.parse_args()

    os.environ.setdefault("XUNZHANG_SCOPE_KEY", args.scope_key or f"xunzhang-verify-{uuid.uuid4().hex[:8]}")
    db_dir = (
        Path(__file__).resolve().parent / "data" / "seekdb"
        if args.keep_db
        else Path(tempfile.mkdtemp()) / "seekdb"
    )

    from service import RetrievalService

    svc = RetrievalService(db_dir)
    health = svc.health()
    if not health["memory"]["reachable"]:
        print("PowerContext 未连接，请先启动：powercontext server run --host 127.0.0.1 --port 8000")
        return 2

    print("== 环境 ==")
    print(f"seekdb      {health['version']} · {health['chunks']} 个片段")
    print(f"向量化      {health['embedder']}")
    print(f"PowerContext scope {health['memory']['scope_id']} · 初始条目 {health['memory']['entry_count']}")
    print()

    failures = []

    def check(name, ok):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            failures.append(name)

    print("== A. 关闭研究记忆 ==")
    print("| 查询 | Top1 | Top3 | 关键词命中 |")
    print("| --- | --- | --- | --- |")
    off = {}
    for query in QUERIES:
        data = svc.search(query, use_memory=False)
        off[query] = data
        print(
            f"| {query} | {data['results'][0]['doc_code']} p.{data['results'][0]['page']} | "
            f"{fmt(data['results'][:3])} | {data['stats']['keyword_hits']} |"
        )
    print()
    check("关闭记忆时不读写 PowerContext", all(d["memory"]["source"] == "off" for d in off.values()))
    check("关闭记忆时所有结果都来自 seekdb", all(d["results"] for d in off.values()))

    print("== B. 开启研究记忆（首轮） ==")
    print("| 查询 | Top1 | Top3 | 记忆扩展 | 记忆加成 |")
    print("| --- | --- | --- | --- | --- |")
    on = {}
    for query in QUERIES:
        data = svc.search(query, use_memory=True)
        on[query] = data
        boosts = sum(1 for r in data["results"] if r["boost"] > 0)
        print(
            f"| {query} | {data['results'][0]['doc_code']} p.{data['results'][0]['page']} | "
            f"{fmt(data['results'][:3])} | {' '.join(data['memory']['expansions'][:4]) or '—'} | "
            f"{boosts} 条 |"
        )
    print()
    check("开启记忆时读取 PowerContext", all(d["memory"]["source"] == "powercontext" for d in on.values()))
    check("记忆扩展改变关键词信号", any(d["memory"]["expansions"] for d in on.values()))

    print("== C. 重复同一查询（跨会话记忆） ==")
    first = on[REPEAT_QUERY]
    again = svc.search(REPEAT_QUERY, use_memory=True)
    recalled = again["memory"]["recalled"]
    print(f"召回历史：{recalled or '（空）'}")
    print("| 片段 | 首轮分数 | 重复后分数 | 召回加成 |")
    print("| --- | --- | --- | --- |")
    for row in again["results"][:3]:
        before = next((r["score"] for r in first["results"] if r["chunk_id"] == row["chunk_id"]), 0)
        print(f"| {row['doc_code']} p.{row['page']} | {before:.3f} | {row['score']:.3f} | {row['boost_detail']['recall']:.2f} |")
    print()
    check("PowerContext 召回上一轮写入的检索记录", bool(recalled))
    check("召回记录参与排序加成", any(r["boost_detail"]["recall"] > 0 for r in again["results"]))

    entries = svc.memory_entries()["entries"]
    print(f"== 写回情况 ==\nscope 内共 {len(entries)} 条记忆，最近三条：")
    for entry in entries[-3:]:
        print(f"  [{entry['kind']}] {entry['text']}")
    print()
    check("检索结果写回 PowerContext", any(e["kind"] == "search" for e in entries))

    svc.store.close()
    print(f"结果：{'全部通过' if not failures else '失败 ' + str(len(failures)) + ' 项'}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
