# Phase 6 — 详细任务分解 ✅ 已归档

> **[ARCHITECT] 模式产出**  
> 日期：2026-02-24  
> 目标：将 Phase 6 候选功能拆解为可逐步执行的工程任务，每个任务包含明确的输入/输出/改动文件/验证方法。
>
> **归档日期：2026-02-24**  
> **完成状态：组 A ✅ | 组 B ✅ | 组 C ✅ | 组 D 🔲 延后 | 组 E 🔲 延后**

---

## 核心需求回顾

用户核心诉求：**"我想要这个 AI 能在对话过程中就全自动地改进自己"**

翻译为工程需求：
1. 对话时 AI 能**自动调用已有工具/技能**完成任务（而非纯 LLM 文本生成）
2. 遇到未知领域时能**自主研究并创建新技能**（自我改进闭环）
3. 上述行为在前端聊天界面中**可见可控**（用户能看到"调用了什么工具"）

---

## 功能分组 & 优先级

| 组 | 名称 | 优先级 | 阻塞关系 |
|---|------|--------|---------|
| **A** | Chat Tool Calling（对话工具调用） | P0 | 无阻塞，可独立开发 |
| **B** | Worker 自我改进升级 | P0 | 依赖 A 部分基础设施（SkillDef 构建函数） |
| **C** | 前端增强（工具调用可视化 + research/code 看板） | P1 | 依赖 A 的后端 |
| **D** | 技能质量自动评估 & 淘汰 | P1 | 依赖 A（执行反馈数据） |
| **E** | 技能版本管理 | P2 | 依赖 D |

**建议实施顺序**：A → B → C → D → E

---

## 组 A — Chat Tool Calling（对话工具调用）

### 当前状态

- `POST /chat` 直接调用 `model_router.generate()` → LLM 纯文本回复，无工具调用能力
- `POST /intention` 走 Manager→Worker 异步管线，结果在任务看板中，不回到聊天气泡
- `model_router.py` 的 `generate()` 不传 `tools` 参数
- 已注册技能存储在 SQLite `SkillMetadata` + ChromaDB `skills_vector` 中

### 目标

用户在聊天框输入 → LLM 判断是否需要调用工具 → 自动执行 → 将运行结果注入上下文 → LLM 生成最终回复 → 前端展示（含工具调用标注）

### 任务 A-1：构建 Skills → OpenAI Function Definitions 转换层

**目标**：将 SQLite 中的 SkillMetadata 转为 OpenAI API 的 `tools` 参数格式

**改动文件**：`app/services/tool_registry.py`（新建）

**输出格式**：
```python
# OpenAI Tool Calling 要求的格式
[
    {
        "type": "function",
        "function": {
            "name": "hello_world",
            "description": "问候技能，返回一句问候语",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "用户名"}
                },
                "required": ["name"]
            }
        }
    },
    ...
]
```

**实现要点**：
- 从 SQLite 读取所有 SkillMetadata，将 `parameters_schema` 转为 OpenAI function parameters
- 对 `id` 做安全化处理（OpenAI function name 限制：`^[a-zA-Z0-9_-]{1,64}$`）
- 加缓存：技能列表不频繁变动，可用 TTL 缓存避免每次请求查库
- 提供 `get_tool_definitions()` → `List[Dict]`
- 提供 `get_tool_by_name(name) → Optional[SkillMetadata]`

**验证**：单元测试 — 注册一个技能后调用 `get_tool_definitions()` 返回正确格式

---

### 任务 A-2：升级 ModelRouter 支持 Tool Calling

**目标**：`model_router.generate()` 新增可选 `tools` 参数，处理 LLM 返回的 `tool_calls`

**改动文件**：`app/services/model_router.py`

**改动点**：

1. `generate()` 签名变更：
```python
async def generate(
    self,
    messages: List[Message],
    model: Optional[str] = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    tools: Optional[List[Dict]] = None,           # 新增
    tool_choice: Optional[str] = "auto",           # 新增
) -> ChatResponse:
```

2. `_call_volcengine()` / `_call_gemini()` 在 `tools` 非空时传给 `client.chat.completions.create()`

3. `ChatResponse` 扩展（见 A-3）：新增 `tool_calls` 字段，原样透传 LLM 返回的 tool_calls

**注意事项**：
- 火山引擎的 OpenAI 兼容 API 支持 tool calling（`ark-code-latest` 支持）
- Gemini 通过 OpenAI compatibility layer 也支持 tool calling（`gemini-2.5-flash` 及以上）
- 需要处理 `response.choices[0].message.tool_calls` 非空的情况
- 当 LLM 决定调用工具时，`content` 可能为 `null`，需要兼容

**验证**：Docker 内调用带 `tools` 的请求，确认 LLM 返回 `tool_calls`

---

### 任务 A-3：扩展 ChatResponse Schema

**改动文件**：`app/models/schemas.py`

**改动**：
```python
class ToolCallFunction(BaseModel):
    name: str
    arguments: str  # JSON string

class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: ToolCallFunction

class ChatResponse(BaseModel):
    id: str
    model: str
    content: Optional[str]  # 改为 Optional（tool calling 时可能为 null）
    usage: Dict[str, Any]
    provider: str
    tool_calls: Optional[List[ToolCall]] = None  # 新增
    tool_results: Optional[List[Dict[str, Any]]] = None  # 新增：工具执行结果
    finish_reason: Optional[str] = None  # 新增：stop / tool_calls / length
```

---

### 任务 A-4：实现 Tool Calling 对话循环（核心）

**目标**：在 `/chat` 端点实现完整的 Tool Calling 循环

**改动文件**：`app/api/routes.py`（改写 `chat_endpoint`）

**流程**：
```
用户消息 → LLM(tools=registry) 
  ├→ finish_reason=stop → 直接返回
  └→ finish_reason=tool_calls → 
      遍历 tool_calls:
        解析 function.name → 找到 SkillMetadata
        解析 function.arguments → JSON → params
        调用 skill_executor.run(language, entrypoint, params)
        收集结果
      → 将 tool_calls + tool results 追加到 messages
      → 再次调用 LLM(messages_with_results)
      → 返回最终回复（附带 tool_results 供前端展示）
```

**实现伪代码**：
```python
@router.post("/chat", response_model=ChatResponse)
async def chat_endpoint(request: ChatRequest):
    from app.services.tool_registry import tool_registry
    
    tools = tool_registry.get_tool_definitions()
    messages = list(request.messages)
    
    MAX_TOOL_ROUNDS = 5  # 防止无限循环
    all_tool_results = []
    
    for round in range(MAX_TOOL_ROUNDS):
        response = await model_router.generate(
            messages=messages,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            tools=tools if tools else None,
        )
        
        if not response.tool_calls:
            # LLM 决定不调用工具，直接返回
            response.tool_results = all_tool_results or None
            return response
        
        # 处理工具调用
        # 将 assistant 的 tool_calls 消息加入 messages
        messages.append(Message(
            role="assistant",
            content=response.content or "",
            tool_calls=response.tool_calls,
        ))
        
        for tc in response.tool_calls:
            skill = tool_registry.get_tool_by_name(tc.function.name)
            params = json.loads(tc.function.arguments)
            
            if skill:
                result = await skill_executor.run(
                    language=skill.language,
                    entrypoint=skill.entrypoint,
                    params=params,
                    timeout=60,
                )
            else:
                result = {"status": "error", "message": f"Tool '{tc.function.name}' not found"}
            
            all_tool_results.append({
                "tool_call_id": tc.id,
                "tool_name": tc.function.name,
                "result": result,
            })
            
            # 将 tool result 追加到 messages（OpenAI 格式要求 role=tool）
            messages.append(Message(
                role="tool",
                content=json.dumps(result, ensure_ascii=False),
                tool_call_id=tc.id,
            ))
    
    # 超过最大循环次数，返回当前结果
    return response
```

**关键决策**：
- `MAX_TOOL_ROUNDS = 5` — 防止 LLM 无限循环调用工具
- `role="tool"` 消息需要 `tool_call_id` 对应（OpenAI API 要求）
- `Message` schema 需要扩展 `tool_calls` 和 `tool_call_id` 可选字段

**验证**：
1. 注册 `hello_world` 技能 → 聊天 "用hello_world技能打个招呼" → 观察到 tool calling
2. 聊天普通问题 → 确认不触发 tool calling（直接回复）
3. 注册不存在的技能 → 确认错误处理正确

---

### 任务 A-5：扩展 Message Schema 支持 Tool Calling 字段

**改动文件**：`app/models/schemas.py`

```python
class Message(BaseModel):
    role: str  # system / user / assistant / tool
    content: Optional[str] = None  # tool calling 时 assistant content 可能为 null
    tool_calls: Optional[List[ToolCall]] = None  # assistant 消息含工具调用
    tool_call_id: Optional[str] = None  # tool 角色消息需关联 tool_call_id
```

**注意**：前端发送的 messages 仍然是简单的 `{role, content}` 格式，tool_calls 相关字段只在后端循环中使用。需要确保序列化/反序列化兼容。

---

### 任务 A-6：内置工具注册（非 Skill 的系统级工具）

**目标**：除了用户注册的 Skills 外，还需要一些内置系统工具

**改动文件**：`app/services/tool_registry.py`

**内置工具列表**（候选）：
| 工具名 | 说明 | 对应现有能力 |
|--------|------|-------------|
| `deep_research` | 触发深度研究 | `ms_agent_service.run_deep_research()` |
| `code_generate` | 触发代码生成 | `ms_agent_service.run_code_genesis()` |
| `search_knowledge` | 搜索知识库 | `memory_retriever.retrieve_context()` |
| `list_skills` | 列出可用技能 | SQLite SkillMetadata 查询 |
| `run_python` | 执行 Python 代码片段 | `run_python_snippet` skill |

**实现方式**：
- 内置工具用硬编码的 function definition 注册
- 执行时走专门的处理函数（不经过 SkillExecutor 子进程）
- 与 Skill 工具合并到同一个 `tools` 列表传给 LLM

**决策点**（执行前需确认）：
- 内置工具列表是否需要这么多，还是先只做 Skill 工具调用？
- `deep_research` 是异步长时任务（5-30分钟），作为 tool call 需要特殊处理（立即返回 task_id，不等结果）

---

### 组 A 验证清单

- [ ] 注册 `hello_world` 技能 → 聊天问"调用hello_world" → 返回中包含 tool_results
- [ ] 聊天普通问题（如"1+1等于几"） → 不触发 tool calling
- [ ] 连续 tool calling（A 工具结果 → 触发 B 工具） → 正确循环
- [ ] 前端气泡展示工具调用过程
- [ ] 不注册任何技能时 → tools=[] → 纯文本回复（兼容现有行为）

---

## 组 B — Worker 自我改进升级

### 当前状态

- `_attempt_self_amendment()` 在 `worker.py` L169-225
- 当前实现：LLM 凭空生成技能 JSON → 调用 skill_crud 注册 → **质量很低**
- 知识缺口检测已实现（L2 距离 > 1.5）

### 目标

知识缺口 → 调用 `ms_agent_service.run_deep_research()` 深度研究 → 解析报告 → 提炼为高质量技能 → 自动注册

### 任务 B-1：实现异步研究 + 回调模式

**问题**：deep_research 耗时 5-30 分钟，不能阻塞 Worker 当前任务执行

**方案**：将 self-amendment 改为"触发后继续"模式

**改动文件**：`app/services/worker.py`

**流程**：
```
知识缺口检测 → 
  1. 立即触发 ms_agent_service.run_deep_research(query=task_description)
  2. 将 task_id 记录到 SQLite（新表 SelfAmendmentTask 或复用 Task 表加 type 字段）
  3. 不等待结果，Worker 继续用 LLM fallback 完成当前任务
  4. 后台定期轮询 research 状态 → 完成后解析报告 → 注册技能
```

**新增后台任务**：`_poll_amendment_tasks()` — 定时检查未完成的自修正研究任务

---

### 任务 B-2：研究报告 → 技能转化器

**目标**：将 deep_research 产出的 `final_report.md` 转化为可注册的技能

**改动文件**：`app/services/worker.py`（新增方法）

**流程**：
```
读取 final_report.md 
  → LLM 分析报告：提取可操作知识点
  → 为每个知识点生成 SkillCreate JSON
  → 调用 POST /skills 注册
  → 可选：将报告本身作为一个 knowledge skill（纯文档型技能，无脚本）
```

**实现**：
```python
async def _distill_report_to_skills(self, task_id: str, report_content: str, original_query: str):
    """将研究报告蒸馏为技能。"""
    prompt = f"""你是知识工程师。以下是对 "{original_query}" 的深度研究报告。
    
请从中提取所有可操作的知识点，为每个知识点生成一个技能定义。

报告内容：
{report_content[:8000]}

输出 JSON 数组，每个元素：
{{"id": "kebab-case", "name": "中文名", "description": "详细描述（会被向量化）", 
  "language": "python", "entrypoint": "scripts/main.py",
  "script_content": "# Python 脚本内容（如有）", "parameters_schema": {{}}}}
"""
```

**验证**：手动触发一次 deep_research → 等报告完成 → 调用蒸馏 → 检查新注册技能

---

### 任务 B-3：纯文档型技能支持

**目标**：对于无法转化为脚本的知识（如概念解释、最佳实践），支持"文档型技能"

**改动文件**：`app/models/database.py`（SkillMetadata 扩展）

**新增字段**：
```python
class SkillMetadata(SQLModel, table=True):
    ...
    skill_type: str = Field(default="executable", description="executable | knowledge")
    knowledge_content: Optional[str] = Field(default=None, description="文档型技能的纯文本知识内容")
```

**行为变化**：
- `skill_type="executable"` → 现有行为，调用 SkillExecutor 执行脚本
- `skill_type="knowledge"` → Worker 检索到后直接将 `knowledge_content` 注入 LLM 上下文作为参考资料（不执行脚本）

---

### 组 B 验证清单

- [ ] 手动创建知识缺口（提交一个当前技能库无法匹配的任务）→ 触发 self-amendment → 观察 deep_research 启动
- [ ] deep_research 完成后 → 观察新技能自动注册到系统
- [ ] 再次提交同类任务 → 不再触发知识缺口（新技能生效）
- [ ] 文档型技能被 Worker 检索到时正确注入上下文

---

## 组 C — 前端增强

### 任务 C-1：工具调用可视化

**改动文件**：`app/web/index.html`

**改动**：
- `sendMessage()` 处理 `ChatResponse.tool_results` 字段
- 当 `tool_results` 非空时，在 AI 回复气泡中插入**工具调用卡片**：

```
┌─────────────────────────────────────────┐
│ 🔧 使用了工具: hello_world              │
│ 参数: {"name": "用户"}                   │
│ 结果: {"status": "success", ...}         │
└─────────────────────────────────────────┘

最终 AI 回复文本...
```

- CSS 样式：卡片使用浅灰底色 + 左侧彩色指示条（成功=绿色，失败=红色）

---

### 任务 C-2：Research/Code 任务看板

**改动文件**：`app/web/index.html`

**新增 UI 区域**：右侧面板扩展，新增两个 Tab

- **研究任务 Tab**：
  - 列出所有 `GET /research` 返回的任务
  - 每个卡片显示：query 摘要、状态指示灯、创建时间
  - 点击展开 → 显示 `GET /research/{id}` 的 report 内容
  - 新建入口：简易表单 → `POST /research/deep`

- **代码生成 Tab**：
  - 列出 `GET /code` 任务
  - 显示 7 阶段进度条
  - 点击展开 → 显示产出文件列表
  - 新建入口：简易表单 → `POST /code/generate`

**注意**：这是纯前端改动，不涉及后端 API 变更（API 已在 Phase 5 就绪）

---

### 组 C 验证清单

- [ ] 调用 tool 的对话 → 前端正确显示工具卡片
- [ ] 纯文本对话 → 不显示工具卡片（无回归）
- [ ] 研究任务看板 → 能显示列表、新建、查看报告
- [ ] 代码生成看板 → 能显示列表、新建、查看产物

---

## 组 D — 技能质量自动评估 & 淘汰

### 任务 D-1：SkillMetadata 增加质量追踪字段

**改动文件**：`app/models/database.py`

```python
class SkillMetadata(SQLModel, table=True):
    ...
    total_calls: int = Field(default=0, description="总调用次数")
    success_calls: int = Field(default=0, description="成功次数")
    fail_calls: int = Field(default=0, description="失败次数")
    avg_exec_time_ms: float = Field(default=0.0, description="平均执行耗时(ms)")
    quality_score: float = Field(default=3.0, ge=0.0, le=5.0, description="综合评分")
    last_used_at: Optional[str] = Field(default=None, description="最后使用时间")
```

**迁移**：`db.py` 添加 ALTER TABLE 迁移逻辑（与现有迁移模式一致）

---

### 任务 D-2：Worker 执行后自动更新质量指标

**改动文件**：`app/services/worker.py`

**改动点**：在 `execute_task()` 中，Skill 执行完成后：
```python
# Step 3 Skill 执行后
if exec_result["status"] == "success":
    await self._update_skill_quality(skill_meta.id, success=True, exec_time_ms=...)
else:
    await self._update_skill_quality(skill_meta.id, success=False, exec_time_ms=...)
```

**评分算法**：
```
quality_score = (success_rate * 3.0) + (recency_bonus * 1.0) + (base * 1.0)
success_rate = success_calls / total_calls  (0~1)
recency_bonus = 1.0 if used_in_last_7_days else 0.5
base = 1.0 (注册就给底分)
```

---

### 任务 D-3：低质量技能自动淘汰

**改动文件**：`app/services/worker.py` 或独立 `app/services/skill_evaluator.py`

**规则**：
- `total_calls >= 5 AND quality_score < 1.5` → 自动删除（太差了）
- `total_calls >= 10 AND quality_score < 2.0` → 标记 deprecated（不再检索）
- 可配置阈值（通过 config.py）

**触发时机**：每次 Worker 更新评分后检查

---

### 组 D 验证清单

- [ ] Skill 执行成功 → `total_calls++`, `success_calls++`, `quality_score` 上升
- [ ] Skill 执行失败 → `fail_calls++`, `quality_score` 下降
- [ ] 连续失败 5 次 → 自动淘汰
- [ ] `GET /skills` 返回新字段（quality_score 等）

---

## 组 E — 技能版本管理（P2，本阶段不详细展开）

### 概要

- SkillMetadata 增加 `version` 字段
- 同一 `id` 允许存在多个版本（只有 active 版本参与检索）
- SkillUpdate 时自动 bump 版本号
- 提供 `POST /skills/{id}/rollback` 回退到上一版本

### 数据模型草案

```python
class SkillVersion(SQLModel, table=True):
    id: str = Field(primary_key=True)  # skill_id + "-v" + version
    skill_id: str
    version: int
    entrypoint: str
    script_content: Optional[str]
    created_at: str
    is_active: bool = True
```

> 详细分解留到组 D 完成后再展开。

---

## 实施时间线估算

| 任务 | 预计工作量 | 累计 |
|------|----------|------|
| A-1: ToolRegistry 转换层 | 30 min | 30 min |
| A-3: ChatResponse 扩展 | 10 min | 40 min |
| A-5: Message Schema 扩展 | 10 min | 50 min |
| A-2: ModelRouter 升级 | 30 min | 1h 20min |
| A-4: Tool Calling 循环 | 45 min | 2h 05min |
| A-6: 内置工具 | 30 min | 2h 35min |
| ── **组 A 验证 + 修 bug** | 30 min | **3h** |
| B-3: 文档型技能支持 | 20 min | 3h 20min |
| B-1: 异步研究回调 | 40 min | 4h |
| B-2: 报告→技能蒸馏 | 30 min | 4h 30min |
| ── **组 B 验证** | 30 min | **5h** |
| C-1: 工具调用可视化 | 30 min | 5h 30min |
| C-2: Research/Code 看板 | 60 min | 6h 30min |
| ── **组 C 验证** | 20 min | **~7h** |
| D-1/D-2/D-3: 质量评估 | 60 min | 8h |

**建议分 3 个开发周期执行**：
1. 周期一：组 A（对话 Tool Calling） — 约 3 小时
2. 周期二：组 B（自我改进） — 约 2 小时
3. 周期三：组 C + D（前端 + 质量评估） — 约 3 小时

---

## 建议首先实施的任务组

**组 A — Chat Tool Calling**，理由：
1. 直接回应用户核心诉求（"对话中自动使用工具"）
2. 是组 B/C/D 的基础设施
3. 改动范围明确，不影响现有异步任务管线
4. 可通过已注册的 `hello_world`、`run_python_snippet` 立即验证

**实施顺序**：A-1 → A-3 → A-5 → A-2 → A-4 → A-6（先数据模型，后逻辑，最后扩展）

---

## 风险与决策点

| # | 决策点 | 选项 | 建议 |
|---|--------|------|------|
| 1 | Tool Calling 模型兼顾性 | 火山引擎 + Gemini 都走 OpenAI 兼容 tool calling | ✅ 两者都支持，统一走 OpenAI SDK |
| 2 | 内置工具 vs 纯 Skill 工具 | 只做 Skill 工具 / 混合做 | 建议先只做 Skill 工具，内置工具第二轮加 |
| 3 | deep_research 作为工具 | 同步等待（不可行） / 返回 task_id | 必须返回 task_id，并在聊天中提示"研究已启动" |
| 4 | Message 扩展是否会破坏前端 | 新字段 Optional + 前端只发 role/content | ✅ 向后兼容 |
| 5 | 最大工具循环次数 | 3 / 5 / 10 | 建议 5，可配置 |
| 6 | 文档型技能是否需要 B-3 先做 | 是 / 否 | 可以后做，先完成 A 组 |
