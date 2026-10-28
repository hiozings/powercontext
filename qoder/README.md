# 寻章 · 文献检索与阅读记忆助手

方向一（文献与资料检索）参赛作品。把论文、课程笔记与实验记录切块后收进 **seekdb**，用一条 SQL 同时做向量召回、全文打分与元数据过滤；**PowerContext** 保存研究主题与检索历史，检索前扩展查询、检索后写回命中页码。

- 在线站点：<https://xunzhang-hgghh06yzck.qoder.website>（托管版为浏览器内演示，页头会标明引擎）
- 代码分支：`2026-competition-ecosystem`，目录 `qoder/`

## 1. 目录

```
qoder/
├── site/            站点源文件（HTML / CSS / JS，无构建步骤）
│   └── assets/corpus.js   样例语料，同时被服务端读取
├── server/          seekdb 检索服务 + PowerContext 客户端
│   ├── app.py       FastAPI：/api/search、/api/memory、/api/health，并托管 site/
│   ├── service.py   查询扩展、融合排序、记忆读写编排
│   ├── store.py     seekdb 嵌入式实例与混合检索 SQL
│   ├── memory.py    PowerContext HTTP 客户端
│   ├── embedding.py 向量化（默认 concept-10d，可切 fastembed）
│   ├── verify.py    可复现的验证脚本
│   └── requirements.txt
├── dist/            发布产物（Qoder Sites 上传目录）
└── README.md
```

## 2. 运行

需要 Python 3.12。以 `qoder/server` 为工作目录。

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

### 2.1 启动 PowerContext（记忆）

```bash
mkdir -p data/powercontext
POWERCONTEXT_HOME=$PWD/data/powercontext \
  ./.venv/bin/powercontext server run --host 127.0.0.1 --port 8000
```

本作品未配置推理模型，记忆检索固定使用 `mode=fts`（全文），写入与列表接口均为官方 `/v1/*` 接口：

| 用途 | 接口 |
| --- | --- |
| 创建 / 获取 scope | `POST /v1/scopes`（固定 `idempotency_key`） |
| 列出记忆条目 | `POST /v1/memory/entries/list` |
| 检索记忆 | `POST /v1/memory/search`（`mode: "fts"`） |
| 写回记忆 | `POST /v1/memory/remember` |

### 2.2 启动检索服务（seekdb）

```bash
./.venv/bin/python app.py
```

打开 <http://127.0.0.1:8787>：站点与服务同源，页头显示 `seekdb v1.4.0.0 · 12 个片段 · PowerContext 已连接` 时表示链路就绪。若改了 `PORT`，用 `http://127.0.0.1:<port>/?live=1` 打开以启用真实检索。

环境变量（均有默认值）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SEEKDB_DIR` | `server/data/seekdb` | 嵌入式数据库目录，首次启动自动建库并导入样例语料 |
| `POWERCONTEXT_BASE_URL` | `http://127.0.0.1:8000` | PowerContext 地址 |
| `XUNZHANG_SCOPE_KEY` | `xunzhang-reader-demo` | 记忆 scope 的幂等键，换一个即换一份独立记忆 |
| `EMBEDDER` | `concept` | 设为 `fastembed` 时使用本地 `BAAI/bge-small-zh-v1.5`（需另装 `fastembed`） |
| `HOST` / `PORT` | `127.0.0.1` / `8787` | 监听地址与端口 |

## 3. 验证样例

```bash
# PowerContext 需已在 8000 端口运行
./.venv/bin/python verify.py
```

脚本使用临时数据库与全新 scope，覆盖三个阶段：关闭记忆、开启记忆、重复检索（跨会话记忆）。完整输出见 [`samples/run-evidence.md`](samples/run-evidence.md)，实测对照：

| 输入 | 关闭记忆 · 前三条 | 开启记忆 · 前三条 | 变化 |
| --- | --- | --- | --- |
| 长文档切块边界怎么处理 | E p.2 / A p.4 / A p.3 | E p.1 / A p.4 / E p.2 | 扩展 chunk / 分块 / overlap 后 E p.1 关键词信号升到 1.00 |
| RAG 评估看哪些指标 | C p.1 / D p.2 / C p.4 | C p.1 / C p.4 / A p.6 | C p.4 取代 D p.2 进入前三 |
| 向量召回和关键词召回怎么融合 | B p.5 / B p.7 / B p.2 | B p.2 / B p.5 / B p.7 | B p.2 关键词信号满值，升到第一 |
| 组会说的 baseline 是什么 | D p.1 / D p.2 / E p.1 | D p.1 / D p.2 / E p.1 | E p.1 命中「召回」标签获得 0.04 加成；重复检索时三条各 +0.03 |

断言全部通过时退出码为 0，任何一项失败退出码为 1。

## 4. 实现要点

### 4.1 一条 SQL 完成三路召回

`store.py` 中向量与全文各取候选，再由窗口函数归一化，服务层叠加记忆加成：

```sql
WITH cand AS (
  SELECT id, doc_code, doc_title, page, content,
         l2_distance(vec, %(qv)s) AS dist
  FROM chunks
  WHERE 1=1                          -- 年份 / 类型过滤在此追加
  ORDER BY dist APPROXIMATE
  LIMIT 10
), kw AS (
  SELECT id, MATCH(content) AGAINST(%(q)s IN NATURAL LANGUAGE MODE) AS kwscore
  FROM chunks
  WHERE MATCH(content) AGAINST(%(q)s IN NATURAL LANGUAGE MODE) > 0
  LIMIT 10
)
SELECT cand.*, COALESCE(kw.kwscore, 0) AS kwscore,
       1 - cand.dist / NULLIF(MAX(cand.dist) OVER (), 0) AS sem_norm,
       COALESCE(kw.kwscore, 0) / NULLIF(MAX(COALESCE(kw.kwscore, 0)) OVER (), 0) AS kw_norm
FROM cand LEFT JOIN kw ON cand.id = kw.id
```

- 表：`chunks(id, doc_code, doc_title, doc_year, doc_venue, doc_kind, tags, page, content, vec VECTOR(10))`
- 索引：`FULLTEXT ... WITH PARSER ngram`（中文二元切分）+ `VECTOR INDEX ... WITH (distance=l2, type=hnsw)`
- 融合：`score = 0.55 × 语义 + 0.45 × 关键词 + 记忆加成`，加成上限 0.09

### 4.2 记忆闭环

1. 首次检索前，把阅读画像（主题、关注点、进度）写入 scope；
2. 检索前 `memory/search` 召回历史检索记录，其中的页码获得 +0.03 加成；画像触发的同义词扩展参与关键词与向量两路；
3. 检索后把「查询 → 命中页码」写回 scope，下一次相同或相近查询即可召回。

### 4.3 向量化

默认 `concept-10d`：把文本映射到长文档、切块、边界、评估、混合检索等 10 个主题维度。这是可离线复现的替代实现，不调用任何 Embedding API；`EMBEDDER=fastembed` 可换成 `BAAI/bge-small-zh-v1.5` 的本地 ONNX 推理，表维度随模型变化，首次启动需重建数据库目录。

## 5. 边界说明

- 样例语料（A–E 五篇）为虚构内容，仅用于演示检索与出处回溯，不代表真实论文。
- 托管站点无法运行本地 seekdb 与 PowerContext，因此在浏览器内用同一套权重近似计算，页头标注「内置样例语料」；真实链路由本地服务提供。
- 未配置真实 Embedding API 与 PowerContext 推理模型，记忆检索使用全文模式；生产环境应替换为真实向量模型。

## 6. 复用与许可

| 组件 | 来源 | 许可 | 本作品改动 |
| --- | --- | --- | --- |
| seekdb / pylibseekdb | <https://github.com/oceanbase/seekdb> | Apache-2.0 | 嵌入式方式调用，自建 schema 与混合检索 SQL |
| PowerContext | <https://github.com/oceanbase/powercontext> | Apache-2.0 | 通过 HTTP API 接入，scope 与记忆条目为本作品定义 |
| Qoder Sites | Qoder 平台 | — | 用于站点发布与托管 |
