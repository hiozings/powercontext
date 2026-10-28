# 实际运行记录（DeepSeek + PowerContext）

运行环境：Dify 1.18.0（Docker Compose）、PowerContext 1.2.0（SQLite 容器）、DeepSeek 插件 `langgenius/deepseek` 0.0.26、模型 `deepseek-flash`（两个 LLM 节点均设置 `thinking: false`）。

本记录来自验证 Scope `midnight-tavern-verify-2`，用于证明「新会话 A 写入 → 新会话 B 读取」的跨会话链路。原始 SSE 运行日志包含 `Authorization` 请求头，已只在本地保留；以下为去除凭据后的回答与节点输出摘录。

## 1. 会话 A（新对话，空账本）

输入：

```text
我第一次来，来杯最便宜的。
```

回答：

```text
账本上空得很，名字先记下。最便宜的？我这儿连水都得算账，先说说你拿什么付。

---
📜 老白的账本
（账本还是空的）

（老白又记下一笔：客人第一次来店里，点了一杯最便宜的。）
```

节点证据（Dify 运行详情）：

| 节点 | 状态 | HTTP / 结果 |
| --- | --- | --- |
| 确保 Scope | succeeded | 201；返回 scope_id |
| 列出账本 | succeeded | 200；`{"memory":null,"entries":[]}` |
| 检索旧账 | succeeded | 200；`{"memory":null,"mode":null,"hits":[]}` |
| 记一笔 | succeeded | 200；返回 `memory.revision = 1` 与 entry citation |

## 2. 会话 B（**新对话**，同一 Scope）

输入：

```text
我回来了。
```

回答：

```text
又来了。上回你只点了最便宜的那杯，坐角落半天没吭声。今晚还是老样子？

---
📜 老白的账本
- 客人第一次来店里，点了一杯最便宜的。
```

节点证据：

| 节点 | 状态 | HTTP / 结果 |
| --- | --- | --- |
| 确保 Scope | succeeded | 201；返回同一个 scope_id |
| 列出账本 | succeeded | 200；`entries` 含会话 A 写入的 `mem_ent_fc765…` |
| 检索旧账 | succeeded | 200；`mode = "fts"`，`hits` 含同一条记忆 |

这说明会话 A 写入的内容在会话 B 中实际被读出并进入了回复，而不是依赖 Dify 的会话历史（B 是新建对话，Dify 历史为空）。

## 3. 翻账本（确定性分支，不经过 LLM）

输入：

```text
翻账本。
```

回答：

```text
📜 老白的账本
- 客人第一次来店里，点了一杯最便宜的。
```

## 4. 不编造账本外的事

输入：

```text
你记得我妹妹吗？
```

回答：

```text
又来了。账本上只写着你头一回来，点了杯最便宜的。妹妹？账本上没写。

---
📜 老白的账本
- 客人第一次来店里，点了一杯最便宜的。
```

## 5. 新事实写回

输入：

```text
我把上次欠的二十块还你。
```

回答：

```text
又来了。账本上只写了你第一次来点了杯最便宜的，可没提欠二十块这回事。钱你先攥着，说清楚是哪笔。

---
📜 老白的账本
- 客人第一次来店里，点了一杯最便宜的。

（老白又记下一笔：客人声称上次欠了二十块，主动来还钱，但账本上没有这笔欠账记录。）
```

`tavern_demo.py inspect`（同一 Scope）输出：

```text
scope: scp_0y63xw5a81zhv07pve02v50gxy
entries: 2
1. [tavern-event] 客人声称上次欠了二十块，主动来还钱，但账本上没有这笔欠账记录。
2. [tavern-event] 客人第一次来店里，点了一杯最便宜的。
```

## 6. 复现方式

1. 按 README 导入 `dsl/midnight-tavern.yml`，设置 `POWERCONTEXT_TOKEN`。
2. 在应用环境变量把 `TAVERN_SCOPE_KEY` 改为新的值（例如 `midnight-tavern-verify-2`），Publish。
3. 按第 1、2 节顺序，在两个**新对话**中分别发送对应输入。
4. 运行：
   ```bash
   python3 dify/scripts/tavern_demo.py inspect \
     --base-url http://127.0.0.1:8000 \
     --token "$POWERCONTEXT_DEMO_TOKEN" \
     --scope-key midnight-tavern-verify-2
   ```
   应与第 5 节的条目一致。

模型输出会有措辞差异，但「B 新会话引用 A 的记忆」「翻账本列出真实条目」「账本外不编造」「新事实写回」应稳定成立。
