# Phase 10 — Agent Loop 自治工作流 + 对话能力扩展 详细任务分解

> **[ARCHITECT] 模式产出**  
> 日期：2026-03-02  
> 目标：使 AI 在网页对话中具备"搜索→研究→学习→注册技能→执行→通知"的完整自治工作流能力，  
> 实现用户只需下达一句高级指令，AI 即可自主规划、多步执行、动态扩展自身能力并交付结果。
>
> **完成状态：组 A ✅ | 组 B 🔲 | 组 C 🔲 | 组 D 🔲**

---

## 核心需求回顾

**用户期望的使用场景**：

```
用户: "帮我总结今天的对话内容，尝试更新需要更新的 skills 和错题库，
       然后通过飞书机器人发送到我的飞书账号。我还没有配置飞书机器人，
       先从网络上查询并告诉我怎么配置。"
```

**当前状态**（Phase 9 截止）：

1. ✅ Tool Calling 基础链路已打通（LLM → tools → execute → 返回）
2. ✅ 4 个新技能已创建：`error_ledger_crud`、`web_search`、`conversation_summary`、`feishu_webhook`
3. ✅ SkillExecutor 已升级为主动空闲检测机制（idle_timeout + max_timeout）
4. ✅ `feishu_webhook_url` / `feishu_webhook_secret` 配置已加入 Settings

**但存在 4 个结构性缺陷，阻止上述场景在对话中自治完成**：

| # | 缺陷 | 严重度 | 说明 |
|---|------|--------|------|
| 1 | 工具列表请求开始时锁死 | 🔴 P0 | AI 在对话中创建新技能后，后续轮次无法调用它 |
| 2 | 5 轮循环硬上限不够 | 🔴 P0 | 复杂多步任务（搜索→学习→创建→执行）至少需要 8-15 轮 |
| 3 | 无 Agent 规划层 | 🔴 P0 | LLM 被动反应式调用，无法预规划、无法回溯、无法预算管理 |
| 4 | deep_research 被隔离 | 🟡 P1 | 强大的多轮研究引擎无法在对话中直接调用 |

**翻译为工程需求**：

1. 每轮 Tool Calling 循环前动态刷新工具列表
2. 最大轮次上限从 5 提升至 30，并注入剩余轮次感知
3. 注入 ReAct Agent System Prompt，引导 LLM 结构化推理
4. 将 deep_research 封装为对话可调用的 Skill

---

## 功能分组 & 优先级

| 组 | 名称 | 优先级 | 阻塞关系 |
|---|------|--------|---------|
| **A** | 新技能注册 + SkillExecutor 升级 + Settings | P0 | 已完成 ✅ |
| **B** | Tool Calling 循环增强（动态刷新 + 轮次提升） | P0 | 无阻塞，可独立开发 |
| **C** | ReAct Agent System Prompt 注入 | P0 | 依赖 B（需要轮次感知生效后才有意义） |
| **D** | deep_research 技能封装 | P1 | 无阻塞，可独立开发 |

**建议实施顺序**：A(done) → B → C → D

---

## 组 A — 新技能 + 基础设施（✅ 已完成）

### 已交付清单

| 交付物 | 说明 |
|--------|------|
| `skills/error_ledger_crud/` | 错题库 CRUD 技能（create/list/get/delete/search） |
| `skills/web_search/` | 联网搜索技能（EXA 优先 → SerpAPI fallback） |
| `skills/conversation_summary/` | 对话历史摘要技能（按日期过滤 → LLM 日报生成） |
| `skills/feishu_webhook/` | 飞书机器人推送技能（text/rich_text/interactive card） |
| `app/services/executor.py` | 主动空闲检测机制（idle_timeout 30s + max_timeout 300s） |
| `app/core/config.py` | 新增 `feishu_webhook_url` / `feishu_webhook_secret` |

---

## 组 B — Tool Calling 循环增强

### 当前状态

```python
# routes.py L62
_MAX_TOOL_ROUNDS = 5

# routes.py L136 — 工具列表在请求开头一次性获取
tools = tool_registry.get_tool_definitions()

# routes.py L146-240 — 循环中始终使用同一份 tools
for round_num in range(_MAX_TOOL_ROUNDS):
    last_response = await model_router.generate(
        ...
        tools=tools if tools else None,
    )
```

### 目标

1. 轮次上限提升至 30
2. 每轮循环开始前重新获取工具列表（支持对话中动态注册新技能后立即可用）
3. 向 LLM 注入剩余轮次信息，使其具备预算感知能力

---

### 任务 B-1：轮次上限提升至 30

**改动文件**：`app/api/routes.py`

**改动点**：

```python
# 旧
_MAX_TOOL_ROUNDS = 5

# 新
_MAX_TOOL_ROUNDS = 30
```

**设计决策**：
- 30 轮足以覆盖"搜索→研究→学习→创建技能→执行→通知"全链路（典型 8-15 轮）
- 剩余空间留给重试和多步骤子任务
- max_timeout 由 SkillExecutor 的 idle_timeout 保护，不会因为轮次增多导致无限挂起
- 每轮 LLM 调用的 Token 消耗由 `_truncate_messages()` 控制，不会因轮次多而爆上下文

**风险与缓解**：
- 风险：LLM 陷入无限工具调用循环 → 缓解：组 C 的 Agent Prompt 会引导 LLM 在合适时机发出 final answer

---

### 任务 B-2：每轮动态刷新工具列表

**改动文件**：`app/api/routes.py`

**改动点**：

将工具列表的获取从循环外移至循环内，并在检测到新技能注册后主动清缓存：

```python
# 旧：循环外一次性获取
tools = tool_registry.get_tool_definitions()
for round_num in range(_MAX_TOOL_ROUNDS):
    last_response = await model_router.generate(
        ...
        tools=tools if tools else None,
    )

# 新：每轮循环开头重新获取
for round_num in range(_MAX_TOOL_ROUNDS):
    tools = tool_registry.get_tool_definitions()
    last_response = await model_router.generate(
        ...
        tools=tools if tools else None,
    )
```

**设计决策**：
- `tool_registry.get_tool_definitions()` 内部有 30s TTL 缓存，频繁调用不会每次都查 SQLite
- `skill_crud` 技能注册新 Skill 时调用 `POST /api/v1/skills`，该端点已有 `tool_registry.invalidate_cache()`
- 因此：Round N 注册新技能 → 缓存清除 → Round N+1 重新拉取 → 新技能出现在工具列表中

**验证**：
- 对话中让 AI 用 `skill_crud` 创建技能 → 下一轮能看到并调用该技能
- 缓存命中率监控：增加 DEBUG 日志记录缓存刷新次数

---

### 任务 B-3：轮次预算注入

**改动文件**：`app/api/routes.py`

**改动点**：

在每轮 Tool Calling 循环中，当 LLM 返回 `tool_calls` 时，在追加结果消息后额外注入一条系统提示：

```python
# 在 tool 结果消息之后追加
remaining = _MAX_TOOL_ROUNDS - round_num - 1
if remaining <= 10:
    messages.append(
        Message(
            role="system",
            content=f"[系统提示] 你还剩 {remaining} 轮工具调用机会。"
                    f"请合理规划剩余步骤，避免浪费。"
                    f"如果任务已完成，直接给出最终回复即可。"
        )
    )
```

**设计决策**：
- 只在剩余 ≤ 10 轮时提醒，避免前期干扰 LLM 正常推理
- system 角色消息不影响对话流，LLM 会将其视为指导信息
- 当剩余轮次很少（≤ 3）时，LLM 倾向于尽快收束给出最终回复

---

## 组 C — ReAct Agent System Prompt

### 当前状态

`/chat` 端点没有注入任何引导 LLM 使用工具的 system prompt。LLM 完全依靠自己对 `tools` 列表的理解来决定是否/如何调用工具。这导致：

1. LLM 不会"先想后做"，经常第一反应就调用工具而不考虑全局
2. 无法回溯或修正计划
3. 多步任务时不知道自己在第几步

### 目标

注入 ReAct（Reasoning + Acting）风格的 Agent System Prompt，引导 LLM：
- 先思考再行动
- 有意识地规划多步工作流
- 在每步执行后评估结果
- 知道自己可以动态创建新技能

---

### 任务 C-1：Agent System Prompt 模板

**改动文件**：`app/api/routes.py`

**改动点**：

在 `/chat` 端点的消息列表最前面（在用户消息之前）注入 Agent System Prompt：

```python
_AGENT_SYSTEM_PROMPT = """你是 Watery AI Agent，一个拥有自主工具调用能力的智能助手。

## 你的核心能力

你可以通过 Tool Calling 调用注册在系统中的技能（Skills）来执行真实操作，包括但不限于：
- 🔍 **联网搜索** (`web_search`) — 搜索实时互联网信息
- 📝 **错题库管理** (`error_ledger_crud`) — 创建/查询/删除错误经验记录
- 💬 **对话摘要** (`conversation_summary`) — 读取指定日期的对话历史并生成日报
- 📢 **飞书推送** (`feishu_webhook`) — 发送消息到飞书群
- 🛠️ **技能管理** (`skill_crud`) — 创建/更新/删除技能（你可以自我进化！）
- 🐍 **执行代码** (`run_python_snippet`) — 运行 Python 代码片段
- 📄 **PDF 处理** (`pdf_extract_text`, `pdf_to_skills`) — 提取和转化 PDF 知识
- ...以及更多你可以自行创建的技能

## 工作准则

1. **先想后做**：收到复杂任务时，先在回复中简要列出你的执行计划（几步、每步用什么工具），
   然后再开始调用工具。
2. **逐步执行**：每步调用一个工具，评估结果后再决定下一步。
3. **自我进化**：如果你发现自己缺少某个能力，可以：
   a. 先用 `web_search` 搜索相关信息
   b. 用 `skill_crud` 将新学到的知识注册为技能
   c. 然后直接使用新创建的技能
4. **知识沉淀**：如果过程中发现了有价值的经验教训，用 `error_ledger_crud` 记录下来。
5. **合理收束**：当任务完成后，给出清晰的最终回复，不要多余地调用工具。

## 注意事项

- 每次对话最多可调用 {max_rounds} 次工具，请合理规划。
- 工具列表会动态更新 —— 你新创建的技能在下一轮就可以使用。
- 如果某个工具调用失败，分析原因后可以换一种方式重试。
"""
```

**设计决策**：
- `{max_rounds}` 会在运行时替换为实际值（30）
- 使用 Markdown 列表清晰枚举能力，帮助 LLM 建立可用工具的心理模型
- "先想后做" 指引激活 LLM 的 Chain-of-Thought 能力
- "自我进化" 流程明确告知 LLM 搜索→注册→使用的三步策略

---

### 任务 C-2：Agent Prompt 注入时机

**改动文件**：`app/api/routes.py`

**改动点**：

在 `/chat` 端点加载完消息后、进入 Tool Calling 循环前，检查消息列表头部是否已有 system prompt，如果没有则注入：

```python
# 在 messages 准备完毕后、进入 Tool Calling 循环前
has_system = any(m.role == "system" for m in messages)
if not has_system and tools:
    # 只在有可用工具时注入 Agent prompt（无工具时退化为普通对话）
    messages.insert(0, Message(
        role="system",
        content=_AGENT_SYSTEM_PROMPT.format(max_rounds=_MAX_TOOL_ROUNDS),
    ))
```

**设计决策**：
- 只在 `tools` 非空时注入 → 无技能时退化为普通聊天，零副作用
- 不覆盖用户手动传入的 system prompt → 检查 `has_system`
- 对于新模式（`conversation_id`），DB 中存储的 system prompt 优先

**验证**：
- 无技能注册时：对话行为与旧版完全一致
- 有技能时：AI 回复风格变为"先思考后行动"
- 用户手动传入 system prompt：Agent prompt 不注入

---

### 任务 C-3：Agent 思考过程可视化（前端增强）

**改动文件**：`app/web/index.html`

**改动点**：

当 AI 的回复中包含工具调用计划（通常是 assistant 消息的 content 部分）时，在前端以特殊样式展示"思考过程"：

```javascript
// 检测 AI 思考内容（工具调用前的 content）
if (msg.tool_calls && msg.tool_calls.length > 0 && msg.content) {
    // 渲染为折叠的"思考过程"卡片
    const thoughtCard = document.createElement('div');
    thoughtCard.className = 'agent-thought-card';
    thoughtCard.innerHTML = `
        <details>
            <summary>💭 Agent 思考过程</summary>
            <div class="thought-content">${marked.parse(msg.content)}</div>
        </details>
    `;
    bubble.appendChild(thoughtCard);
}
```

**设计决策**：
- 使用 `<details>` 折叠，默认收起，不占用对话空间
- 只在有 `tool_calls` 的 assistant 消息中显示
- 让用户能看到 AI "为什么要调用这个工具"的推理过程

---

## 组 D — deep_research 技能封装

### 当前状态

`ms_agent_service.run_deep_research()` 是一个强大的多轮迭代研究引擎：
- 最多 6 轮迭代，自带搜索 + 证据收集 + 报告生成
- 但只能通过 `POST /api/v1/ms-agent/research` 单独调用
- 不是 `/chat` 中的一个 tool
- 结果不会实时反馈到对话

### 目标

将 deep_research 封装为 `skills/deep_research/` 目录下的标准 Skill，  
使 AI 在对话中遇到需要深度研究的问题时可以直接调用。

---

### 任务 D-1：deep_research Skill 脚本

**新增文件**：`skills/deep_research/SKILL.md`、`skills/deep_research/scripts/main.py`

**SKILL.md 定义**：

```yaml
---
id: deep_research
name: 深度研究
description: |
  发起一个多轮迭代的深度研究任务（最多 6 轮），自动搜索互联网、
  收集证据、生成完整的 Markdown 研究报告。适用于需要全面了解
  某个主题的场景（如学习新技术、调研解决方案、对比分析等）。
  注意：此操作耗时较长（通常 2-5 分钟），会在后台执行。
language: python
entrypoint: scripts/main.py
parameters_schema:
  type: object
  properties:
    query:
      type: string
      description: 研究主题/问题
    max_rounds:
      type: integer
      description: 最大研究轮数（默认 3，最大 6）
  required:
    - query
tags:
  - research
  - search
  - deep-analysis
---
```

**scripts/main.py 逻辑**：

```python
# 工作流程:
# 1. 调用 POST /api/v1/ms-agent/research 启动研究任务
# 2. 轮询 GET /api/v1/ms-agent/tasks/research/{id} 等待完成
# 3. 每 15 秒写一次 stderr 心跳（避免 idle_timeout kill）
# 4. 完成后返回报告内容（截断至 max_chars 防止输出过长）
```

**设计决策**：
- 通过 HTTP 调用本地 API（而非直接 import ms_agent_service），保持 Skill 的进程隔离
- stderr 心跳每 15s 一次（`[progress] Researching... round 2/6`），低于 idle_timeout 30s
- 报告内容截断至 8000 字符，避免 Tool Calling 结果过长爆上下文
- 超时保护：脚本内部 max_wait = 600s（10 分钟），超时则返回已有的部分结果

---

### 任务 D-2：deep_research 结果自动蒸馏为技能

**改动文件**：`skills/deep_research/scripts/main.py`

**改动点**：

当研究报告返回后，自动调用 `POST /api/v1/skills` 将报告注册为 `knowledge` 类型技能：

```python
# 研究完成后，将报告自动注册为知识型技能
skill_data = {
    "id": f"research_{query_slug}_{date_str}",
    "name": f"研究报告: {query[:30]}",
    "description": f"关于'{query}'的深度研究报告，生成于 {date_str}",
    "language": "python",
    "entrypoint": "scripts/main.py",  # 占位
    "skill_type": "knowledge",
    "knowledge_content": report_text[:10000],
}
httpx.post(f"{_API_BASE}/skills", json=skill_data, timeout=30)
```

**设计决策**：
- 自动蒸馏为 `knowledge` 类型技能，Worker 和 Chat 均可在后续任务中语义检索命中
- `query_slug` 从 query 中提取关键词作为技能 ID（避免重复）
- 报告截断至 10000 字符，避免 SQLite 行过大

---

## 验证场景：完整自治工作流

完成 A+B+C+D 后，以下场景应可在一次对话中自治完成：

```
用户: "帮我查一下怎么配置飞书自定义机器人，配好后把今天的对话总结发到飞书群。"

Round 1 [Thought]: 用户需要两件事：① 学习飞书机器人配置 ② 总结对话并推送。
       我先搜索飞书配置方法。
Round 2 [Action]: web_search("飞书自定义机器人 webhook 配置教程 2026")
Round 3 [Observation]: 搜到了配置步骤，包括获取 Webhook URL 和签名方法。
Round 4 [Thought]: 信息足够了，先把教程回复给用户，然后总结对话。
Round 5 [Thought]: 把搜索到的飞书配置知识注册为技能，方便以后复用。
Round 6 [Action]: skill_crud(create knowledge skill "feishu_config_guide")
Round 7 [Action]: conversation_summary(date=today)
Round 8 [Observation]: 日报生成成功，包含今天的讨论主题和待办事项。
Round 9 [Thought]: 尝试发送到飞书。如果 FEISHU_WEBHOOK_URL 未配置，
       会返回错误和配置步骤说明。
Round 10 [Action]: feishu_webhook(interactive card, 日报内容)
Round 11 [Observation]: 发送成功 / 或返回"未配置"错误
Round 12 [Done]: 向用户汇报结果。
```

**预期**：整个过程 3-5 分钟，用户全程无需干预。

---

## 变更文件清单

| 文件 | 组 | 变更类型 |
|------|---|---------|
| `app/api/routes.py` | B, C | 修改（轮次 / 动态刷新 / Agent Prompt / 预算注入） |
| `app/web/index.html` | C | 修改（Agent 思考可视化） |
| `skills/deep_research/SKILL.md` | D | 新增 |
| `skills/deep_research/scripts/main.py` | D | 新增 |
| `features.md` | — | 更新 Phase 10 记录 |

**不需要改动**：`executor.py`（已在组 A 升级）、`config.py`（已在组 A 扩展）、`requirements.txt`（无新依赖）、`database.py`（无 Schema 变更）

---

## 与原有系统的兼容性

| 场景 | 影响 |
|------|------|
| 无技能注册时的普通对话 | ✅ 零影响（Agent Prompt 不注入，退化为纯聊天） |
| 前端旧模式（messages 传入） | ✅ 兼容（Agent Prompt 只在有 tools 时注入） |
| Worker DAG 任务链路 | ✅ 无影响（Worker 走独立的 execute_task 路径，不经过 /chat） |
| 已有的 5 个技能 | ✅ 无影响（仅新增能力，不修改已有技能） |
