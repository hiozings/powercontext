# SpecLens：面向软件持续演进的 SDD 上下文管家

SpecLens 把一个开发任务变成可核验的上下文：从 **seekdb** 检索规格、代码和测试，交给 **MiniMax** 完成一次真实代码修改，跑目标与回归测试，再把任务状态写进 **PowerContext**，重启后继续使用。

本目录对应赛题「规有所依——面向软件持续演进的 SDD 上下文管理系统」。

## 1. 作品与任务

演示业务工程是一个小的 FastAPI 图书服务（`demo_project/`）。本次任务：

> 给 `GET /books` 增加 `min_price`、`max_price` 过滤；`min_price > max_price` 返回 422；已有 genre 过滤、分页、详情行为必须保持。

基线代码不含价格过滤，目标测试因此失败；修改后目标测试和回归测试全部通过。

## 2. 技术分工

| 技术 | 实际职责 | 证据 |
| --- | --- | --- |
| **MiniMax** | 读取 SpecLens 选出的上下文，生成对 `app/` 的代码编辑；开发记录与提交 diff 对应 | `evidence/<run>/request.json`、`response-*.json`、`patch.diff` |
| **seekdb** | 存储规格条款、代码符号、测试函数，并提供全文检索；查询结果直接进入上下文 | `engineering_chunks` 表、`evidence/seekdb-queries.jsonl`、`ingest` 日志 |
| **PowerContext** | 保存任务阶段、选中来源、补丁哈希、测试结果；新进程重启后读取并注入 `context`/`status` | `evidence/<run>/status.log`、PowerContext Memory entry |

Dify/其他平台未参与；本项目是命令行上下文模块 + 可运行业务工程。

## 3. 目录

```
minimax/
  README.md                 本文件
  ONE-PAGER.md              一页技术说明
  Makefile                  常用命令
  requirements.txt          固定依赖（含 pylibseekdb wheel）
  context_module/
    cli.py                  ingest / context / run / status / db-stats
    ingest.py               规格、代码、测试 → chunk
    seekdb_store.py         嵌入式 seekdb 建表、写入、全文查询
    selection.py            任务 → 规格/代码/测试上下文
    state.py                PowerContext 状态读写
    minimax_agent.py        MiniMax 调用、编辑应用、测试、replay
  demo_project/             业务工程（FastAPI + pytest + specs）
  snapshots/before/         修改前代码快照
  patches/                  MiniMax 生成并经 promote 的补丁（replay 用）
  scripts/verify.py         端到端验证
  evidence/                 运行证据
```

## 4. 快速开始

### 4.1 安装

要求 Linux x86_64、Python 3.12、`uv`。

```bash
cd minimax
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
```

`pylibseekdb` 使用比赛固定 wheel；如果网络受限，请按 `requirements.txt` 中的 URL 预下载。

### 4.2 启动 PowerContext（任务状态）

另开终端：

```bash
uv tool install --force "powercontext[cli,server]==1.2.0"

export POWERCONTEXT_HOME=/tmp/spec-lens-powercontext
export POWERCONTEXT_SERVER_HTTP_HOST=127.0.0.1
export POWERCONTEXT_SERVER_HTTP_PORT=8010
powercontext server run
```

默认 loopback 不启用鉴权；如需 token，设置 `POWERCONTEXT_TOKEN` 并让两个进程使用同一个值。

### 4.3 初始化与上下文

```bash
cd minimax
make ingest                 # 重建 seekdb 工程索引
make context                # 输出 JSON；写入 evidence/context-latest.json
make db-stats               # seekdb 各类 chunk 数量
```

`context` 输出包含：

- `specs[]`：规格 ID、文件、行号、正文；
- `code[]`：需要修改的文件、符号、完整源码；
- `tests[]`：目标测试与回归测试的 nodeid、源码；
- `state`：PowerContext 中的任务阶段、已完成/待办、验证结果；
- `selection.sources`：seekdb 返回并实际使用的 chunk 来源。

### 4.4 验证代码修改

```bash
make test                   # 当前最终代码：8 passed
make replay                 # 从 before 快照恢复基线 → 目标测试失败 → 应用补丁 → 全部通过
make verify                 # 端到端检查并写入 evidence/verify-*/
make status                 # 重启进程后读取 PowerContext 任务状态
```

`make verify` 当前应输出全部 `true`（14 项检查），包括基线目标测试失败、最终测试通过、seekdb 有数据、规格更新后上下文变化、补丁有效、状态可复用。

### 4.5 用 MiniMax 完成一次真实修改（需要 Key）

```bash
# Key 只放在本机 .secrets/（已在 .gitignore 中），不要提交
mkdir -p .secrets
printf '%s' '<你的 Key>' > .secrets/minimax.key
chmod 600 .secrets/minimax.key

export MINIMAX_API_KEY="$(cat .secrets/minimax.key)"
export MINIMAX_PROTOCOL=anthropic
export MINIMAX_BASE_URL=https://api.minimax.cn/anthropic
export MINIMAX_MODEL=MiniMax-M3.1-Flash             # 以 Token Plan 实际可用模型为准
make run-live
```

M Plan / Token Plan 的订阅 Key 与按量计费 API Key 不通用。订阅 Key 走 MiniMax 的
Anthropic 兼容端点（`ANTHROPIC_BASE_URL=https://api.minimax.cn/anthropic`）；本 demo
默认使用该协议。若使用按量计费 Key，可设 `MINIMAX_PROTOCOL=openai` 和
`MINIMAX_BASE_URL=https://api.minimax.io/v1`。如果返回 HTTP 402
`insufficient_balance_error`，请在 MiniMax 控制台确认 M Plan/Token Plan 已开通，并且
使用的是套餐页的订阅 Key（它和按量计费 API Key 不通用）。

`run-live` 会：

1. 从 `snapshots/before` 还原基线，先跑测试（目标测试应失败）；
2. 把 SpecLens 选出的规格、代码、测试发给 MiniMax；
3. 应用 MiniMax 返回的结构化编辑，运行目标 + 回归测试；
4. 最多 3 轮，把失败输出反馈给模型；
5. 把请求、响应、最终补丁和测试日志写入 `evidence/<timestamp>-live/`，并写入 PowerContext 状态。

如果 MiniMax 的补丁通过了测试，把它提升为最终代码：

```bash
make promote RUN=evidence/<timestamp>-live
```

`promote` 会把该次 live 运行中的 `app/` 复制到 `demo_project/app/`，重新生成 `patches/0001-books-price-filter.patch`，再执行 `make test` / `make verify` 确认最终代码与记录一致。

提交前应确认 `demo_project` 的最终代码与 `patch.diff` 一致。记录中只保留请求/响应正文和补丁，不保存私有思维链和 API Key。

## 5. 必做项对照

| 验收项 | 本作品如何满足 |
| --- | --- |
| 能够运行 | 固定 Python/依赖；`make install/ingest/context/test/replay/verify`；PowerContext 启动命令；无需未交付文件 |
| MiniMax 实际参与 | `make run-live` 记录任务、上下文、响应与最终 diff；`demo_project` 的修改可由该 diff 复核 |
| 上下文正确 | `context` 包含 `SPEC-BOOKS-FILTER-3`、`app/api/books.py`/`app/repository.py`、目标与回归测试；`SPEC-BOOKS-FILTER-4` 在 spec 更新后出现 |
| 数据库实际参与 | ingest 写入 seekdb `engineering_chunks`；`MATCH ... AGAINST` 查询结果进入 `selection.chunk_ids`；`db-stats` 可核对 |
| 状态复用 | `run`/`replay` 写入 PowerContext；新进程 `status`/`context` 读取同一任务状态 |
| 代码修改有效 | 基线目标测试失败（`test_filter_by_price_range` 等 4 个）；最终 8 个测试全部通过；测试文件未被修改 |

## 6. 规格更新与重置

```bash
make spec-update            # 用 v2 覆盖当前 spec，重新 ingest，输出 after-spec-update context
make spec-reset             # 恢复 v1 并重新 ingest
```

v2 新增 `SPEC-BOOKS-FILTER-4`（过滤后的分页 total），并把 422 detail 固定为
`min_price must be less than or equal to max_price`。`make verify` 会断言新条款进入上下文。

重置任务状态：换一个 PowerContext Scope key（`POWERCONTEXT_SCOPE_KEY`，默认 `sdd-books-demo`），或删除 `POWERCONTEXT_HOME` 后重启；seekdb 可 `rm -rf data/` 后重新 `make ingest`。

## 7. 已知限制

- seekdb 使用嵌入式 `pylibseekdb`，固定 Linux x86_64；其他平台需改为 seekdb 服务或 OceanBase。
- 检索使用全文匹配，中文长句召回有限；演示任务的规格/代码标识符为英文，命中稳定。
- MiniMax 的编辑是受约束 JSON（`old`/`new` 精确锚点），不是任意 diff；复杂补丁可能需要多轮。
- 业务工程是内存数据；本题只要求一次局部修改，不涉及并发写。
- `patches/` 是本次 MiniMax 运行并经 `promote` 提升的补丁；`replay` 不调用模型，用于无 Key 时复现。
