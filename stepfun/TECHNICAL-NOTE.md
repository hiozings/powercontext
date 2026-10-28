# 端阅 · 一页技术说明

## 结论

本项目把「切块 → 向量化 → 入库 → 混合检索」全部放在本机：向量由浏览器内的 INT8 BGE 模型生成，向量与原文只写入 seekdb，检索由 seekdb 在一次 `DBMS_HYBRID_SEARCH` 调用中完成全文 + 向量召回和 RRF 融合，客户端不拼接两路结果。测试环境无可用 GPU，应用按设计自动降级到 WASM 并在界面明示。

## 架构与数据流

```
文档 ──浏览器──> 切块(420字/重叠60) ──> BGE INT8 (ONNX Runtime Web/WASM)
                                             │ 512 维归一化向量 + 原文
                                             ▼
                                   Express API（校验维度）
                                             ▼
                        seekdb 1.4.0（本地 TCP :2887，seekdb-js）
                        collection: ngram 全文索引 + HNSW cosine 向量索引
                                             ▲
查询 ──浏览器──> 查询前缀 + BGE ──> 向量 ──> hybridSearch(whereDocument, knn, rrf)
                                             │
                                   RRF 排序结果（出处/片段/分数）
```

- 前端：Vite 7 + 原生 JS；Transformers.js 3.8.1；`pipeline("feature-extraction", "bge-small-zh-v1.5", { dtype:"q8", device:"webgpu"|"wasm" })`；mean pooling + L2 归一化；查询加模型卡规定的指令前缀，文档不加。
- 后端：Express 5，`127.0.0.1:8788`；启动时拉起 `@oceanbase/seekdb-node` 随包分发的 seekdb 可执行文件（`--nodaemon --port 2887 --base-dir …`），再用 seekdb-js 的 server 模式连接。选择本地 server 而非嵌入式绑定，是因为 seekdb-js 需要 TCP 端点，而该版本的进程内绑定只暴露 Unix socket。
- 集合 schema：`VECTOR(512)` + HNSW（cosine）；全文索引 `ngram_token_size=2`，适配中文无空格分词。
- 增量：同一 `doc_id` 先删旧片段再写入；单篇写入后立即 `refresh_index()`，多篇导入时逐篇写入、最后合并刷新一次。知识库选择持久化到 `server/data/session.json`。

## 端侧推理与量化

- 模型：BAAI/bge-small-zh-v1.5（4 层，hidden 512，输出 512 维，MIT），使用 Xenova 的 ONNX 导出与官方 INT8 预量化文件，权重未改动。
- 设备选择：先 `navigator.gpu.requestAdapter()`；有适配器则 `device:"webgpu"`，否则或初始化失败时改用 `device:"wasm"`，并把原因写入右上角状态胶囊的 tooltip。WASM 开启 SIMD 与 4 线程（服务端设置 COOP/COEP，页面 `crossOriginIsolated=true`）。
- 体积：INT8 24,010,842 B（22.9 MB）对比 FP32 94,851,877 B（90.5 MB），压缩 **3.95×**；`manifest.json` 记录实测字节数与 SHA-256。
- 离线：`env.allowRemoteModels=false`，模型、分词器、ORT WASM 全部由本站静态资源提供，运行期没有任何云端推理请求。

## seekdb 混合检索

```js
const res = await col.hybridSearch({
  query: { whereDocument: { $contains: text }, nResults: limit * 4 },
  knn: { queryEmbeddings: embedding, nResults: limit * 4 },
  rank: { rrf: {} },
  nResults: limit,
  include: ["documents", "metadatas", "distances"],
});
```

seekdb-js 会把上述参数交给 seekdb 的 `DBMS_HYBRID_SEARCH`，两路召回与 RRF 都在数据库进程内完成；返回的 `distances` 即 RRF 融合分数。浏览器只提交查询文本与查询向量，不做任何二次排序或拼合。全文查询串会先去除全文检索的保留符号，避免语法错误。

## 性能实测

环境：Linux x64，16 核，Chrome 150（Electron 43），无 GPU 适配器 → WASM 4 线程；12 篇语料 / 17 个片段。

| 阶段 | 优化前 | 优化后 |
| --- | --- | --- |
| 模型加载 + 预热（INT8 / FP32） | — | 0.60 s / 0.95 s |
| 向量化 17 片段（INT8 / FP32） | 每篇一次 ORT 调用 | 跨文档合批，1.95 s / 3.05 s（9.0 / 5.6 片段每秒） |
| 写入 seekdb | — | 0.10 s |
| 索引刷新（12 篇） | 每篇刷新一次，累计 7.44 s | 合并一次，0.51 s |
| 整批导入 | 9.3 s | **2.56 s** |
| seekdb 混合检索（服务端） | — | p50 3–4 ms，p95 5.1 ms |
| 查询端到端（热态） | — | 约 20 ms（向量化 12 ms + HTTP/检索 8 ms） |

批大小 1 / 4 / 8 / 16 的吞吐分别为 9.0 / 9.0 / 9.2 / 8.9 片段每秒，差异在噪声内：WASM 单会话推理是算力瓶颈，合批主要减少调用次数与空转。首次查询因 ONNX 内核尚未预热，端到端约 70–120 ms，之后稳定在 20 ms 左右。若运行环境提供 WebGPU，会走同一条 `hybridSearch` 链路，仅替换推理后端。

## 检索质量

12 条中文标注查询，文档级去重后按 seekdb 返回顺序计算 Recall@5 / nDCG@5：

| 配置 | Recall@5 | nDCG@5 |
| --- | --- | --- |
| FP32 + WASM | 1.000 | 0.959 |
| INT8 + WASM | 1.000 | 0.990 |

量化后 Recall@5 未下降；nDCG@5 的 +0.031 在这个小规模标注集上应视为噪声，不宣称量化提升质量。逐条结果见 `samples/results/bench-int8.json`、`bench-fp32.json`。该集合只有 12 篇文档，指标用于回归验证，不代表开放域效果。

## 合规边界与局限

- 无云端推理；无预计算向量或检索结果；评测向量在运行时由浏览器现场生成。
- 向量与文档只存 seekdb，检索只走 `hybridSearch`；没有客户端内存检索兜底。
- 当前测试机无 WebGPU 适配器，因此报告中的性能数字均为 WASM；WebGPU 路径已实现但未能在本机实测。
- 本地 seekdb 由 Node 服务拉起，监听回环地址；不适合直接暴露到公网。删除 `server/data/` 可重置数据。
