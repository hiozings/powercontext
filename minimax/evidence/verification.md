# 测试与验证命令记录

| 阶段 | 命令 | 退出码 | 结果 |
| --- | --- | --- | --- |
| 基线（`snapshots/before`） | `cd demo_project && ../.venv/bin/python -m pytest -q` | 1 | 4 failed（4 个价格目标测试），4 passed（回归） |
| 最终代码 | 同上 | 0 | 8 passed |
| seekdb 重建 | `make ingest` / `python context_module/cli.py ingest` | 0 | 5 spec + 5 code + 8 test chunk |
| 上下文 | `python context_module/cli.py context --label before` | 0 | 含 `SPEC-BOOKS-FILTER-3`、books.py、repository.py、目标/回归测试 |
| 规格更新 | `make spec-update` + `context --label after-spec-update` | 0 | 出现 `SPEC-BOOKS-FILTER-4`，FILTER-3 detail 更新 |
| 补丁回放 | `python context_module/cli.py run --mode replay` | 0 | 基线失败（before_exit=1），补丁后通过（after_exit=0） |
| 状态复用 | 新进程 `python context_module/cli.py status` | 0 | 读到 `stage=verified`、`after_tests_passed=true` |
| MiniMax 实际修改 | `MINIMAX_PROTOCOL=anthropic MINIMAX_BASE_URL=https://api.minimax.cn/anthropic MINIMAX_MODEL=MiniMax-M3.1-Flash make run-live` | 0 | 1 次尝试；基线 4 failed/4 passed，修改后 8 passed；记录在 `20261029T140509Z-live/` |
| 端到端 | `make verify` | 0 | 14 项检查全部 true（见 `verify-summary.json`） |

对应的完整输出分别在 `baseline-tests.log`、`final-tests.log`、`replay.log`、`status.log`、`seekdb-queries.jsonl`。

正式提交的 commit 哈希在提测时记录；本目录中的路径均为相对工作区路径。
