# 端阅 · 端侧知识库

在浏览器里完成文本向量化，用 [seekdb](https://github.com/oceanbase/seekdb) 做向量 + 全文混合检索的迷你知识库。对应 2026 Qoder × OceanBase 竞赛「seekdb × 阶跃星辰：端侧 AI 推理性能优化与 seekdb 混合检索」赛题。

- 文档在浏览器内切块，用 **BGE-small-zh-v1.5** 的 INT8 ONNX 模型在本地生成 512 维向量。
- 向量、原文与元数据写入本地 **seekdb**；查询向量同样在浏览器内生成。
- 检索只发一次请求，由 seekdb 的 `DBMS_HYBRID_SEARCH` 完成全文召回、向量召回与 RRF 融合，浏览器不做二次拼合。
- 优先请求 **WebGPU**，不可用时自动降级到 **WASM**，右上角明确显示当前后端与降级原因。
- 全程不调用云端推理 API，不预置向量或检索结果。

## 快速开始

要求 Node.js >= 22，Chrome / Edge 最新版，Linux x64（seekdb 原生包当前随 `@oceanbase/seekdb-node` 安装）。

```bash
cd stepfun
npm install
npm start
```

打开 <http://127.0.0.1:8788>。首次启动会在 `server/data/seekdb` 初始化 seekdb 数据目录，并自动创建「知识库 1」，约需数秒。

`npm start` 会先构建前端再启动 API。开发模式：

```bash
npm run dev   # API :8788 + Vite :5173（/api、/samples 已代理）
```

模型与 ONNX Runtime 静态资源已随仓库放在 `web/public/`，离线可运行。若需重新获取模型：

```bash
npm run fetch:model         # 23 MB INT8 模型 + 分词器，写入 manifest.json
npm run fetch:model:fp32    # 额外下载 91 MB FP32 基线模型
```

FP32 跑法：下载后打开 <http://127.0.0.1:8788/?dtype=fp32>。默认 `/?dtype=q8` 或根路径使用 INT8。

## 使用

1. 拖入 `.md` / `.txt`，或粘贴文本，点「导入知识库」；也可点「导入示例语料」加载 12 篇中文技术笔记。
2. 在检索框输入问题。结果按 seekdb 的 RRF 融合分数排序，并显示出处、片段序号和原文。
3. 点「运行检索评测」会用 `samples/eval.json` 的 12 条标注查询计算 Recall@5 / nDCG@5，可下载 JSON。
4. 新建 / 切换知识库会持久化在 `server/data/session.json`。

## 目录

```
stepfun/
├─ web/                     Vite 前端
│  ├─ src/embedder.js       Transformers.js + ONNX Runtime Web，WebGPU→WASM
│  ├─ src/main.js           切块、批量向量化、导入、检索、评测
│  ├─ public/models/        BGE ONNX 模型、分词器、manifest.json
│  └─ public/ort/           ONNX Runtime Web WASM 二进制
├─ server/
│  └─ src/
│     ├─ db.js              启动 seekdb、集合 schema、入库、混合检索
│     └─ index.js           Express API 与静态站点
├─ samples/
│  ├─ corpus.json           12 篇示例语料
│  ├─ eval.json             12 条标注查询
│  └─ results/              INT8 / FP32 实测评测 JSON
├─ scripts/fetch-models.mjs 从 ModelScope 拉取并校验模型
├─ TECHNICAL-NOTE.md        一页技术说明（架构、量化、性能、质量）
└─ package.json             根工作区脚本
```

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 引擎、seekdb 版本、维度、当前知识库 |
| GET | `/api/kbs` | 知识库列表与片段数 |
| POST | `/api/kbs` | 新建知识库 |
| POST | `/api/session` | 切换当前知识库 |
| POST | `/api/ingest` | 写入文档片段与向量；`deferRefresh` 可延迟刷新 |
| POST | `/api/refresh` | 刷新全文 / 向量索引 |
| POST | `/api/search` | 一次混合检索，返回 RRF 排序结果 |

服务端会校验每个向量为 512 维有限数值；客户端只提交文本与向量，不提交检索结果。

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PORT` | `8788` | Web / API 端口 |
| `SEEKDB_PORT` | `2887` | 本地 seekdb TCP 端口 |
| `SEEKDB_DIR` | `server/data/seekdb` | seekdb 数据目录 |

删除 `server/data/` 即可清空知识库并重新初始化。

## 实测结果

测试环境：Linux x64、16 核、Chrome 150（Qoder Electron 43），无可用 GPU 适配器，因此自动降级到 WASM（4 线程，`crossOriginIsolated=true`）。完整逐条数据见 `samples/results/bench-int8.json` 与 `bench-fp32.json`。

| 指标 | INT8 | FP32 |
| --- | --- | --- |
| 模型文件 | 22.9 MB | 90.5 MB |
| 压缩比 | 3.95× | 1× |
| 加载 + 预热 | 0.60 s | 0.95 s |
| 向量化吞吐（批 8，420 字片段） | 9.0 片段/s | 5.6 片段/s |
| 12 篇 / 17 片段入库（向量化 + 写入 + 刷新） | 2.56 s | 3.56 s |
| Recall@5（12 条查询） | 1.000 | 1.000 |
| nDCG@5 | 0.990 | 0.959 |
| 查询：向量化 / seekdb / 端到端（热态） | 12 ms / 4 ms / 20 ms | — |

索引刷新做过一次合并优化：12 篇文档原先每篇刷新一次累计 7.44 s，改为写入完成后统一 `refresh_index()` 后为 0.51 s，整批导入从 9.3 s 降到 2.6 s。批大小 1/4/8/16 的吞吐基本持平（约 9 片段/s），说明 WASM 单会话推理是算力瓶颈；合批的价值在于减少调用次数而不是线性加速。

## 约束与来源

- 推理在浏览器内完成：模型、分词器、ORT WASM 均由本站静态资源加载，`allowRemoteModels=false`，无云端推理调用。
- 存储与检索都在 seekdb：集合同时建 ngram 全文索引与 HNSW 余弦索引，检索调用 `collection.hybridSearch()`，由 seekdb 内部完成 RRF。
- 未预置任何向量或检索结果；评测查询的向量在运行时由浏览器生成。
- 复用与许可：
  - OceanBase seekdb / seekdb-js / `@oceanbase/seekdb-node`：Apache-2.0。
  - [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5)：MIT；ONNX 导出与 INT8 预量化文件来自 [Xenova/bge-small-zh-v1.5](https://huggingface.co/Xenova/bge-small-zh-v1.5)，通过 ModelScope 镜像获取，未修改权重。查询前缀按模型卡加入。
  - [Transformers.js](https://github.com/huggingface/transformers.js)：Apache-2.0；ONNX Runtime Web：MIT。
  - `samples/corpus.json` 与 `samples/eval.json` 为本项目原创示例。
