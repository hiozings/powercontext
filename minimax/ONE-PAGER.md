# 一页技术说明：SpecLens

## 问题

AI 开发助手要改一处代码，需要同时知道：这条任务受哪些规格约束、哪些代码和测试相关、哪些行为不能破坏、上一次做到哪里。SpecLens 把这几件事做成一条可复现的链路，而不是让模型在整库上猜。

## 结构

```text
demo_project/specs + app + tests
        │  ingest（AST / Markdown clause 切分）
        ▼
seekdb: engineering_chunks（spec/code/test + FULLTEXT）
        │  context：任务关键词 → MATCH ... AGAINST → 规则加权
        ▼
context JSON：specs[] / code[] / tests[] / state
        │
        ├──→ MiniMax（OpenAI 兼容 API）→ 结构化编辑 → 应用 → pytest
        │                                            │
        │                                            ▼
        └──────────────────────────────→ PowerContext: sdd-task-state
                                                     │
                                          新进程 status/context 读取并复用
```

## 上下文选择

1. 规格按显式 `<!-- id: ... -->` 切成 clause；代码按 AST 提取函数/类；测试按 AST 提取 `test_*`。
2. 任务描述分词后做 seekdb 全文查询；每条结果按“关键词命中 + spec 中的 `min_price`/`max_price`/422 + 代码路径 + 测试内容”加权。
3. 规格取相关 clause（本 demo 5 条），代码取相关文件全文（最多 3 个），测试取目标 + 回归（本 demo 8 个）。
4. 输出每条来源的真实 `path`、`line`、`source_id`；`verify.py` 检查文件存在。
5. 规格更新后重新 ingest，查询结果来自新内容，不返回固定字符串。

## 三项技术分工

- **seekdb**：唯一存放规格/代码/测试 chunk 的检索库；查询结果直接决定上下文内容。
- **PowerContext**：只存任务状态（阶段、选中来源、验证结果、补丁信息），重启后读取；不存工程资料。
- **MiniMax**：只负责根据上下文产出代码编辑；不参与检索和状态存储。

## 验证

- 基线目标测试失败（4 个价格相关测试），回归测试通过；
- MiniMax 补丁应用后 8 个测试全部通过；
- `make verify` 逐项检查规格/代码/测试来源、seekdb 查询、规格更新、补丁效果与 PowerContext 状态复用；
- 测试文件在补丁中完全未改动，补丁只触及 `app/`。
