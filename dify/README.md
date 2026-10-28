# 午夜记账酒馆（Dify × PowerContext）

一个只在午夜出现的酒馆。老板老白什么都记得：你上次把花生米倒进钢琴、欠下的二十块、吹过的牛、道过的歉。你离开再回来，他翻着账本跟你算旧账。

作品用 **Dify Chatflow** 编排对话，用 **PowerContext** 保存跨会话记忆；本作品选择 PowerContext 一项指定技术，不使用 seekdb（赛题允许二选一）。

## 1. 玩法

- 推门进来，老白按「账本」决定态度：第一次见、又来了、还是来还钱的。
- 你可以点单、吹牛、恶作剧、道歉、还债；每轮结束，值得记住的事会被写进 PowerContext。
- 说「翻账本」可以直接查看已经记下的内容，不经过模型，作为记忆生效的确定性证据。
- **开一个新会话**再回来说「我回来了」，老白会主动翻旧账，证明记忆跨会话存在。
- 问到账本里没有的事，老白会说「账本上没写」，不编造。

## 2. 技术分工

| 组成 | 在本作品中的作用 |
| --- | --- |
| Dify Chatflow | 输入处理、意图分支、HTTP 调用、Code 解析、LLM 角色回复、LLM 记忆抽取、答案输出 |
| PowerContext | 创建稳定的酒馆 Scope；每轮检索/列出记忆；把新事实写回 Memory；跨会话复用 |
| Dify 会话记忆 | 只负责当前会话最近 4 轮对话，不承担跨会话 |

记忆读写是一个完整闭环，不是旁路演示：

1. `POST /v1/scopes` 用固定 `idempotency_key` 获取同一间酒馆；
2. `POST /v1/memory/entries/list` 读出账本，`POST /v1/memory/search` 按当前输入补充相关旧账；
3. 把合并后的账本注入老白的 system prompt；
4. 回复生成后，低温度 LLM 抽取一条本轮事实，`POST /v1/memory/remember` 写回；
5. 下一次会话重复第 2 步，旧账进入新会话的第一次回复。

## 3. 运行环境

| 组件 | 版本/要求 |
| --- | --- |
| Dify | **1.18.0**（自托管 Docker Compose；DSL 在该版本导出/导入验证） |
| PowerContext | **1.2.0**（`powercontext[cli,server]`，默认 SQLite；本仓库提供容器镜像） |
| 模型 | DeepSeek 官方 API，模型 `deepseek-flash`；Dify 插件 `langgenius/deepseek` **0.0.26**；LLM 节点关闭 `thinking`，避免推理过程进入回答 |
| 容器 | Docker 19.03+、Compose 2.24+；建议 ≥ 2 核 / 4 GiB |
| 网络 | PowerContext 在 Dify 的 `ssrf_proxy_network` 内，Dify 通过 `http://powercontext:8000` 访问 |

不需要 Embedding / Rerank 模型；PowerContext 默认 SQLite 全文检索即可跑通。需要语义召回时，再按 PowerContext 文档配置 embedding 并把检索 `mode` 改为 `hybrid`。

## 4. 部署步骤

以下命令假设 Dify 克隆在 `~/dify`，本提交目录在 `~/powercontext-submission`。

### 4.1 启动 Dify

```bash
git clone --branch 1.18.0 --depth 1 https://github.com/langgenius/dify.git ~/dify
cd ~/dify/docker
cp .env.example .env
```

编辑 `.env`，至少设置（如果 Dify 已在运行，改完后执行
`docker compose up -d --force-recreate ssrf_proxy api worker` 让代理重新读取）：

```bash
# 允许 HTTP Request 节点访问 Docker 私网中的 PowerContext
SSRF_PROXY_ALLOW_PRIVATE_IPS=172.16.0.0/12

# 演示用的 PowerContext Bearer token；用 openssl rand -hex 24 生成
POWERCONTEXT_DEMO_TOKEN=<替换为随机值>
```

### 4.2 把 PowerContext 加入同一个 Compose

```bash
cp -r ~/powercontext-submission/dify/powercontext ~/dify/docker/powercontext
cp ~/powercontext-submission/dify/docker-compose.override.yaml ~/dify/docker/
docker compose up -d
```

说明：

- `docker-compose.override.yaml` 通过 `ssrf_proxy_network` 接入 Dify 网络；如果 Dify 的目录不是 `docker`，用 `docker network ls | grep ssrf` 查到的实际网络名替换文件里的 `docker_ssrf_proxy_network`。
- PowerContext 同时接入 `ssrf_proxy_network`（Dify 内部调用）和 `default`（把 8000 映射到宿主机）；`ssrf_proxy_network` 是 internal 网络，单独使用无法发布宿主端口。
- 数据保存在 `~/dify/docker/powercontext-data/`；删除它并重建容器即可清空。
- 8000 被占用时，把 override 里的 `127.0.0.1:8000:8000` 改成别的宿主端口，并同步 `seed/inspect` 的 `--base-url`；Dify 内部地址仍是 `http://powercontext:8000`。

检查：

```bash
curl -s http://127.0.0.1:8000/health/live && echo
```

### 4.3 初始化 Dify

1. 打开 `http://localhost/install`，创建管理员账号。
2. 进入 **Settings → Model Provider**，安装 **DeepSeek** 插件，填入 DeepSeek API Key。
3. 确认模型列表中有 `deepseek-flash`。

### 4.4 导入 DSL

1. **Studio → Import DSL**，选择 `dify/dsl/midnight-tavern.yml`。
2. 导入后检查两个 LLM 节点（`老白回复`、`提取记忆`）的模型是否为 `deepseek-flash`；如果 Dify 提示重新绑定，选择已配置的 DeepSeek 模型。
3. 打开应用右侧 **Environment Variables**，填写：

| 变量 | 值 | 说明 |
| --- | --- | --- |
| `POWERCONTEXT_BASE_URL` | `http://powercontext:8000` | 容器内地址；若 PowerContext 不在 Compose 内，改成可达地址 |
| `POWERCONTEXT_TOKEN` | 与 `.env` 中 `POWERCONTEXT_DEMO_TOKEN` 相同 | Bearer token，不要提交真实值 |
| `TAVERN_SCOPE_KEY` | `midnight-tavern-v1` | 换一个值即可重开一间酒馆 |

4. 点击 **Publish**，打开分享链接或 Dify 内置调试页开始体验。

### 4.5 可选：预置旧账与查看账本

```bash
export POWERCONTEXT_DEMO_TOKEN=<与 .env 中相同的值>
python3 dify/scripts/tavern_demo.py seed \
  --base-url http://127.0.0.1:8000 \
  --token "$POWERCONTEXT_DEMO_TOKEN"

python3 dify/scripts/tavern_demo.py inspect \
  --base-url http://127.0.0.1:8000 \
  --token "$POWERCONTEXT_DEMO_TOKEN"
```

`seed` 只在账本为空时写入 3 条初始记忆；`inspect` 打印当前 Scope 的全部条目，可直接作为「记忆已持久化」的证据。脚本只用 Python 标准库。

## 5. 验证样例

详细步骤和预期表现见 [`samples/verification.md`](samples/verification.md)；一次真实 DeepSeek 运行的回答与节点记录见 [`samples/run-evidence.md`](samples/run-evidence.md)。最短验证路径：

1. **新会话 A**：发送「我第一次来，来杯最便宜的。」
   预期：老白像第一次见你；回答带「📜 老白的账本」；账本里出现一条本轮记录。
2. **新会话 B**（点“新建对话”，不要只继续发消息）：发送「我回来了。」
   预期：第一轮就翻出 A 会话/预置的旧账；账本能看到对应记录；没有编造账本外的事。
3. 发送「翻账本。」
   预期：直接输出账本条目，不调用模型。
4. 发送「你记得我妹妹吗？」
   预期：如果账本没有，老白明确表示没记，不编造。
5. 发送「我把二十块还你。」后再「翻账本」，预期出现还款记录；再来一个新会话，老白承认账清了。

模型输出不会逐字一致，验收看行为：是否引用账本、是否跨会话、是否不编造、翻账本是否列出真实条目。

## 6. 运行证据

建议在提交材料中附：

- Dify **Logs → 选择会话 → 节点详情**：`确保 Scope`、`列出账本`、`检索旧账` 的 200 响应；`记一笔` 的 200 响应。
- PowerContext 侧：`tavern_demo.py inspect` 的输出，或 `POST /v1/memory/entries/list` 的响应。
- 两次会话的截图：A 会话首次到访、B 会话新会话翻旧账。
- 可选：Dify 应用分享链接和一段 30 秒录屏。

## 7. 目录

```
dify/
  README.md                     本文件
  dsl/midnight-tavern.yml       Dify Chatflow DSL（令牌为占位符）
  powercontext/Dockerfile       PowerContext 1.2.0 运行镜像
  docker-compose.override.yaml  把 PowerContext 接入 Dify Compose
  scripts/tavern_demo.py        seed / inspect 脚本（标准库）
  samples/verification.md       验证样例
  samples/run-evidence.md       实际 DeepSeek 运行记录（已去除凭据）
```

## 8. 重置与数据

- **重开一间酒馆**：把应用环境变量 `TAVERN_SCOPE_KEY` 改成新值（如 `midnight-tavern-v2`），重新 Publish。旧数据仍留在旧 Scope，但新会话不再读取。
- **清空全部**：停止容器，删除 `~/dify/docker/powercontext-data/`，重新 `docker compose up -d`；或删除 Scope 对应的数据库文件。
- Scope 是按 `idempotency_key` 幂等创建的：相同 key + 相同 title/summary 总是返回同一个 Scope，不会重复建馆。

## 9. 已知限制

- 默认检索是 SQLite 全文检索，中文长句召回有限；本应用用 `entries/list` 兜底「最近账本」，`search` 只做补充。数据规模变大时应配置 embedding 并使用 `hybrid`。
- 模型可能偶尔忽略账本或生成风格漂移；prompt 已要求「只使用账本事实、账本外说没写」，但不能保证 100% 稳定。
- 演示默认所有访客共用一间酒馆（固定 `TAVERN_SCOPE_KEY`）。需要每人独立存档时，把 key 改为包含 `sys.user_id` 的值，并注意浏览器/用户标识变化会产生新馆。
- 本作品不使用 seekdb，Dify 的默认数据库与 PowerContext 的 SQLite 分开；这是题目允许的选型。
- 不要在 DSL、README 或截图中提交真实 API Key；`POWERCONTEXT_TOKEN` 和 DeepSeek Key 都只放在本地 `.env` / Dify 配置中。
