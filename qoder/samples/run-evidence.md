# 寻章 · 运行验证记录

验证时间：2026-10-28 15:0x CST · Linux 6.8.0-147-generic · Python 3.12.3

| 组件 | 版本 |
| --- | --- |
| seekdb（`pylibseekdb` 嵌入式） | 1.4.0.post2，`SELECT VERSION()` → `5.7.25-OceanBase seekdb-v1.4.0.0` |
| PowerContext | 1.4.1（`powercontext server run`，默认 SQLite） |
| FastAPI / uvicorn | 0.162.0 / 0.54.0 |
| 向量化 | `concept-10d`（离线概念向量，未调用外部 Embedding API） |

初始数据：5 篇样例文档、12 个带页码片段，写入 `chunks` 表（`FULLTEXT ... WITH PARSER ngram` + `VECTOR INDEX ... hnsw/l2`）。记忆 scope 首次启动写入 3 条阅读画像。

```bash
cd qoder/server
./.venv/bin/python app.py                  # 终端 A：seekdb 检索服务，:8787
./.venv/bin/python verify.py               # 终端 B：验证脚本（PowerContext 需已在 :8000）
```

## 1. verify.py 完整输出

```text
== 环境 ==
seekdb      5.7.25-OceanBase seekdb-v1.4.0.0 · 12 个片段
向量化      concept-10d
PowerContext scope scp_60z55950c7k62tya9jqa0ctc0c · 初始条目 3

== A. 关闭研究记忆 ==
| 查询 | Top1 | Top3 | 关键词命中 |
| --- | --- | --- | --- |
| 长文档切块边界怎么处理 | E p.2 | E p.2 / A p.4 / A p.3 | 5 |
| RAG 评估看哪些指标 | C p.1 | C p.1 / D p.2 / C p.4 | 3 |
| 向量召回和关键词召回怎么融合 | B p.5 | B p.5 / B p.7 / B p.2 | 8 |
| 组会说的 baseline 是什么 | D p.1 | D p.1 / D p.2 / E p.1 | 1 |

  PASS  关闭记忆时不读写 PowerContext
  PASS  关闭记忆时所有结果都来自 seekdb
== B. 开启研究记忆（首轮） ==
| 查询 | Top1 | Top3 | 记忆扩展 | 记忆加成 |
| --- | --- | --- | --- | --- |
| 长文档切块边界怎么处理 | E p.1 | E p.1 / A p.4 / E p.2 | chunk 分块 片段 overlap | 4 条 |
| RAG 评估看哪些指标 | C p.1 | C p.1 / C p.4 / A p.6 | evaluation 忠实度 指标 metric | 4 条 |
| 向量召回和关键词召回怎么融合 | B p.2 | B p.2 / B p.5 / B p.7 | hybrid 混合 fusion recall | 1 条 |
| 组会说的 baseline 是什么 | D p.1 | D p.1 / D p.2 / E p.1 | 基线 对照 会议 笔记 | 3 条 |

  PASS  开启记忆时读取 PowerContext
  PASS  记忆扩展改变关键词信号
== C. 重复同一查询（跨会话记忆） ==
召回历史：['检索「组会说的 baseline 是什么」→ 命中 D p.1、D p.2、E p.1']
| 片段 | 首轮分数 | 重复后分数 | 召回加成 |
| --- | --- | --- | --- |
| D p.1 | 0.763 | 0.793 | 0.03 |
| D p.2 | 0.249 | 0.279 | 0.03 |
| E p.1 | 0.119 | 0.149 | 0.03 |

  PASS  PowerContext 召回上一轮写入的检索记录
  PASS  召回记录参与排序加成
== 写回情况 ==
scope 内共 8 条记忆，最近三条：
  [search] 检索「RAG 评估看哪些指标」→ 命中 C p.1、C p.4、A p.6
  [search] 检索「组会说的 baseline 是什么」→ 命中 D p.1、D p.2、E p.1
  [search] 检索「长文档切块边界怎么处理」→ 命中 E p.1、A p.4、E p.2

  PASS  检索结果写回 PowerContext
结果：全部通过
```

## 2. HTTP 接口证据

```bash
$ curl -s http://127.0.0.1:8787/api/health
{
  "engine": "seekdb",
  "version": "5.7.25-OceanBase seekdb-v1.4.0.0",
  "chunks": 12,
  "embedder": "concept-10d",
  "memory": {"reachable": true, "scope_id": "scp_1xsfsbabkpx6r1fzx20xn17srt", "entry_count": 5}
}
```

```bash
$ curl -s -X POST http://127.0.0.1:8787/api/search \
    -H 'Content-Type: application/json' \
    -d '{"query":"长文档切块边界怎么处理","memory":false,"limit":2}'
{
  "stats": {"candidates": 10, "keyword_hits": 5, "elapsed_ms": 7.01, "sem_weight": 0.55, "kw_weight": 0.45},
  "results": [
    {"chunk_id": "E-2", "doc_title": "实验记录：chunk size 与重叠对召回的影响", "page": 2,
     "dist": 0.5169, "kwscore": 1.8290, "sem_norm": 0.6207, "kw_norm": 1.0, "score": 0.7914},
    {"chunk_id": "A-4", "doc_title": "长文档问答中的分块边界与上下文割裂", "page": 4,
     "dist": 0.3791, "kwscore": 0.5443, "sem_norm": 0.7218, "kw_norm": 0.2976, "score": 0.5309}
  ]
}
```

`dist` 为 seekdb 的 `l2_distance`，`kwscore` 为 `MATCH ... AGAINST` 原始分数，`sem_norm` / `kw_norm` 由 SQL 窗口函数按本次候选集归一化。

```bash
$ curl -s -X POST http://127.0.0.1:8000/v1/memory/entries/list \
    -H 'Content-Type: application/json' \
    -d '{"scope_id":"scp_1xsfsbabkpx6r1fzx20xn17srt","limit":20}'
[profile] 关注点：切块边界 / 召回评估
[profile] 研究方向：RAG 检索增强生成
[profile] 已读 3 / 5 篇，常用方法：混合检索 / 重排
[search] 检索「组会说的 baseline 是什么」→ 命中 D p.1、D p.2、E p.1
```

## 3. 说明

- 该次运行使用临时 seekdb 目录与全新 scope，因此 `verify.py` 每次执行都能得到上表结果；`scope_id` 每次不同。
- `qoder/server/.venv` 与 `data/` 已在 `.gitignore` 中排除，按 `qoder/README.md` 的步骤可重建。
- 站点托管版不含后端，页头会标注「内置样例语料」；上表数值只能在本地服务上复现。
