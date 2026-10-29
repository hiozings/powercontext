# 运行证据

本目录保存 SpecLens 的可复现证据。所有记录来自 2026-10-29 的本机运行；没有提交 API Key、PowerContext token 或私有思维链。

## 已包含

| 文件 | 说明 |
| --- | --- |
| `verify-summary.json` | `make verify` 的 14 项检查，全部 `true` |
| `baseline-tests.log` | 从 `snapshots/before` 还原的基线：4 个目标测试失败，4 个回归测试通过 |
| `final-tests.log` | 最终代码：8 个测试全部通过 |
| `replay.log` | 应用 MiniMax 补丁：`before_exit_code=1`、`after_exit_code=0` |
| `ingest.log` | seekdb 索引构建：5 spec + 5 code + 8 test chunk |
| `seekdb-chunks.json` | 写入 seekdb 的 chunk ID 清单 |
| `seekdb-queries.jsonl` | 每次 context 的查询与返回 chunk ID |
| `seekdb-stats.log` | seekdb `db-stats` 输出 |
| `context-before.json` | 规格 v1 下的上下文：规格、代码、测试、任务状态 |
| `context-after-spec-update.json` | 规格 v2 下的上下文：出现 `SPEC-BOOKS-FILTER-4` |
| `status.log` | 新进程通过 PowerContext 读取任务状态 |
| `20261029T140509Z-live/` | MiniMax 真实开发记录：请求、响应、测试前后、补丁、结果 |

PowerContext 状态写入与读取使用默认 Scope `sdd-books-demo`；`status.log` 中的 `stage=verified`、`before_target_failed=true`、`after_tests_passed=true` 来自实际 replay。

## MiniMax 开发记录

真实运行记录在 `20261029T140509Z-live/`：

- `request.json`：协议 `anthropic`、模型 `MiniMax-M3.1-Flash`、system/user 消息（含 SpecLens 选中的基线代码、规格和测试）；
- `response-1.json`：模型响应，只保留 text/usage，已去除 thinking 块；
- `tests-before.log` / `tests-after-1.log`：修改前 4 个目标测试失败，修改后 8 个测试通过；
- `patch.diff`：MiniMax 生成并实际应用到基线的补丁；
- `result.json`：`before_exit_code=1`、`after_exit_code=0`、一次尝试成功。

`patches/0001-books-price-filter.patch` 已由 `promote` 从这次 live 运行生成，与 `20261029T140509Z-live/patch.diff` 的 SHA-256 一致；`demo_project` 的最终代码就是该补丁应用后的状态。运行记录中的 Key 没有写出。

如果未来重新运行返回 HTTP 402 `insufficient_balance_error`，说明 Key 已通过鉴权但账户没有可用的 Token Plan/M Plan 额度；需要先在 MiniMax 控制台的套餐页面开通或兑换额度。
