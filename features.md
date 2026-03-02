# Watery AI Agent - 当前功能清单 (Feature List)

本文档记录了 Watery AI Agent 系统截至目前（Phase 6）已实现的核心功能。

## 1. 基础架构与部署 (Infrastructure)
- **Docker 容器化**: 后端服务完全容器化，通过 `docker-compose` 一键启动，支持跨平台部署。
- **热重载 (Hot Reload)**: 开发环境下，代码修改后容器内服务自动重启。
- **持久化存储**: 数据库文件和向量数据通过 Docker Volume 挂载到宿主机的 `./data` 目录，保证数据不丢失。
- **异步框架**: 基于 FastAPI 和 Python 3.11 的全异步架构，提供高性能的 API 服务。

## 2. 模型路由与对话 (Model Routing & Chat)
- **多模型支持**: 实现了 `ModelRouter`，目前默认接入火山引擎 (Volcengine) 的 `ark-code-latest` 模型。
- **统一接口**: 提供标准的 `/api/v1/chat` 接口，兼容 OpenAI 格式的请求和响应。
- **前端 UI**:
  - 提供了一个类似 Gemini/DeepSeek 的现代化 Web 界面 (`app/web/index.html`)。
  - 支持多会话管理（新建、切换、删除对话）。
  - 对话历史记录持久化保存在浏览器的 `localStorage` 中。
  - 支持 Markdown 渲染和代码高亮。

## 3. 任务编排与执行 (Orchestration & Execution) - Phase 2 核心
- **意图解析 (Manager Agent)**:
  - 提供 `/api/v1/intention` 接口，接收用户的复杂意图。
  - Manager Agent 能够将复杂意图拆解为有向无环图 (DAG) 形式的子任务列表。
  - **接口异步化**: 意图接口立即返回"已受理"，Manager 在后台异步执行，不阻塞用户操作。
  - **UUID 化 Task ID**: 模型输出的临时 ID（`task_1` 等）在入库前全部替换为 UUID，彻底消除重复提交时的主键冲突问题。
- **任务队列 (Orchestrator)**:
  - 基于 SQLite (`sqlmodel`) 实现了任务状态追踪（PENDING, RUNNING, COMPLETED, FAILED）。
  - 基于 `asyncio.Queue` 实现了内存中的任务分发队列。
  - 支持任务依赖管理，只有前置任务完成后，后续任务才会被放入执行队列。
  - **启动恢复 (Recovery)**: 服务启动时自动扫描 SQLite，将 RUNNING 任务重置为 PENDING，并将依赖已满足的 PENDING 任务重新推入队列，彻底解决热重载/重启后任务卡死的问题。
- **后台执行 (Worker Agent)**:
  - 系统启动时自动并发运行 **3 个** Worker（Worker-01 ~ Worker-03），支持并行处理 DAG 中无相互依赖的任务分支。
  - Worker 持续轮询任务队列，认领并通过 LLM 执行任务描述，完成后解锁下游依赖任务。

## 4. 记忆与 RAG 系统 (Memory & RAG)
- **向量数据库**: 集成了 ChromaDB 作为本地向量搜索引擎。
- **技能库 (Skills Vector)**: 建立了技能集合，支持存储和检索不同语言（Python, Shell 等）的技能描述。
- **错题集 (Error Ledger)**: 建立了错误账本集合，用于记录历史错误和纠正方案。
- **按需检索**: Manager Agent 在拆解任务前，会根据用户意图自动从 ChromaDB 检索相关的技能和防错经验，作为上下文注入到 Prompt 中，以提高任务拆解的准确性并节省 Token。
- **知识沉淀**: 建立了人工可读的 `error_ledger.md` 文档，持续记录开发和部署过程中的重大问题（含本次 Orchestrator 修复）。

## 5. 前端界面与监控 (Frontend & Monitoring)
- **双模式对话界面**:
  - **对话模式**（默认）：标准聊天，调用 `/api/v1/chat`。
  - **任务模式**：通过"开启任务模式"切换，输入意图后触发 Manager + Worker 全自动执行链路。
- **任务看板 (Task Dashboard)**: 右侧面板实时（每 5 秒）轮询任务列表，以颜色标签直观展示 PENDING / RUNNING / COMPLETED / FAILED 状态。
- **知识库/技能库/错题集查阅**:
  - 左侧边栏的"📚 知识库"、"🛠️ 技能库"、"📓 错题集"点击后以模态框形式展示后端真实数据。
  - 后端对应新增 `/api/v1/knowledge`、`/api/v1/skills`、`/api/v1/errors` 三个只读接口。
- **代理状态监控**: 左下角实时显示 Clash 代理健康状态（已就绪 / 正在载入 / 超时 / 异常），已修复 `trust_env` 导致的误报。
- **健康检查**: 提供 `/health` 接口用于监控服务状态。

## 6. Phase 3 新增功能 (2026-02-22)

### 6.1 Skills CRUD 全链路
- **注册技能**: `POST /api/v1/skills` — 同时写入 SQLite SkillMetadata 表和 ChromaDB `skills_vector` 向量集合；支持随请求体内嵌 `script_content` 自动写入脚本文件。
- **列出技能**: `GET /api/v1/skills` — 返回全部已注册技能。
- **删除技能**: `DELETE /api/v1/skills/{skill_id}` — 从 SQLite 和 ChromaDB 同步删除。
- **新增 Schema**: `SkillCreate`（id, name, description, language, entrypoint, parameters_schema, script_content）。

### 6.2 Worker 完整执行链路
- **Step 1 — RAG 技能匹配**: Worker 在执行任务前先通过 `memory_retriever.retrieve_context()` 语义检索最相关技能。
- **Step 2 — SkillExecutor**: 若找到匹配技能，调用 `SkillExecutor.run(language, entrypoint, params, timeout=60)` 直接执行脚本，不消耗 LLM token。
- **Step 3 — LLM 兜底**: 技能未命中或执行失败时回退 LLM，同时将历史错误警告注入 Prompt。
- **Step 4 — 汇报**: 执行结果通过 `orchestrator.complete_task()` / `fail_task()` 写回 SQLite。

### 6.3 失败任务级联处理
- `orchestrator.fail_task()` 递归标记所有下游 PENDING 依赖任务为 FAILED，防止任务树死锁。
- 失败事件自动异步写入 ChromaDB `error_ledger_vector`，供后续任务 RAG 检索使用。

### 6.4 稳定性强化
- **asyncio GC 保护**: `_background_tasks` Set + `add_done_callback(discard)` 防止后台任务被垃圾回收器静默取消（`main.py` + `routes.py`）。
- **ChromaDB 非阻塞**: 所有 ChromaDB 调用通过 `run_in_executor` 推入线程池，不再阻塞 asyncio 事件循环。
- **lifespan 优雅关闭**: 从废弃的 `@app.on_event` 迁移至 ASGI 标准的 `@asynccontextmanager lifespan`；shutdown 取消并等待所有 Worker 和后台任务。
- **Executor 超时**: `SkillExecutor` 新增 `timeout` 参数（默认 60s），超时后强制 `process.kill()`。

### 6.5 数据库与配置
- **Task 时间戳**: `Task` 模型新增 `created_at` 和 `updated_at` 字段；DB 启动时自动检测并 `ALTER TABLE` 升级已有数据库（无损迁移）。
- **配置集中化**: `clash_api_url`、`proxy_url`、`subscription_url`、`proxy_region_filter`、`clash_config_path` 全部统一到 Pydantic Settings；`proxy_manager.py` 移除 `os.getenv()` 直接调用。
- **pyyaml 显式依赖**: `requirements.txt` 补齐 `pyyaml>=6.0.1`。

## 7. Phase 3.5 新增功能 (2026-02-24)

### 7.1 Anthropic Agent Skills 协议支持
- **SkillLoader 服务** (`app/services/skill_loader.py`)：解析 SKILL.md YAML frontmatter + Markdown 正文。
- **示例技能**：`hello_world`（问候脚本）、`run_python_snippet`（安全执行 Python 代码片段）。
- **批量导入**: `POST /api/v1/skills/load-dir` — 扫描目录下所有 SKILL.md 子目录，自动注册。
- **单技能查询**: `GET /api/v1/skills/{skill_id}` — 返回完整元数据。
- **直接执行**: `POST /api/v1/skills/{skill_id}/run` — 直接调用技能脚本，支持 JSON body 传参。
- **错题集自动摄入**: 启动时自动解析 `error_ledger.md` 并写入 ChromaDB `error_ledger_vector`。

### 7.2 模型列表更新 (2026-02-23)
- **Gemini**: 新增 `gemini-3.1-pro-preview`、`gemini-3-flash-preview`、`gemini-2.5-pro`、`gemini-2.5-flash-lite`；移除已废弃 1.x 系列。
- **火山引擎 Coding Plan**: 新增 `doubao-seed-code-preview-251028`、`doubao-seed-1-8-251228`、`glm-4-7-251222`、`deepseek-v3-2-251201`、`kimi-k2-thinking-251104`。

## 8. Phase 4 已实现功能 (2026-02-24)

### 8.1 PDF-to-Skills 智能文档学习系统 ✅
- **PDF 文本提取**: 使用 pypdf + pdfplumber 提取纯文本和表格数据。
- **智能语义分块**: 三级递降算法 — 标题层级 → 段落 → Token 窗口滑动，保留文档原始结构。
- **AI 结构化摘要**: 每个 Chunk 调用 LLM 输出 SkillDraft JSON。
- **SKILL.md 自动生成**: 按 Anthropic Skills 协议输出 YAML frontmatter + Markdown 正文。
- **自动注册**: 生成后自动写入 SQLite + ChromaDB，Worker 立即可用。

### 8.2 技能自我修正系统 ✅
- **skill_crud 元技能**: 允许 Agent 运行时自主创建/更新/删除技能。
- **知识缺口检测**: Worker RAG 检索 L2 距离 > 1.5 时自动触发缺口补充。
- **PUT /skills/{id} 更新接口**: 支持就地更新已有技能（PATCH 语义）。

### 8.3 PDF 文档溯源 ✅
- **PDFDocument 表**: 记录已处理 PDF 的哈希、页数、生成的技能列表、处理状态。
- **SkillMetadata 扩展**: 新增 `source_pdf_id`、`source_pages`、`tags` 溯源字段。

### 8.4 新增 API ✅
- `POST /api/v1/pdf/upload` — 上传 PDF 文件。
- `POST /api/v1/pdf/to-skills` — 触发 PDF→Skills 异步流水线。
- `GET /api/v1/pdf/status/{doc_id}` — 查询流水线处理进度。
- `PUT /api/v1/skills/{skill_id}` — 更新已有技能。

### 8.5 新增技能 ✅
- `pdf_extract_text` — PDF 文本提取 (pypdf + pdfplumber)。
- `pdf_to_skills` — PDF→技能包全流水线。
- `skill_crud` — 技能库自修正元技能（create/update/delete）。

## 9. Phase 5 已实现功能 — ms-agent 深度能力集成 (2026-02-24)

### 9.1 MSAgentService 服务层
- **独立进程管理**: ms-agent 作为 CLI 子进程运行，通过 `.watery_status.json` 跟踪状态。
- **deep_research**: 多轮迭代式深度研究（最多 6 轮），输出完整 Markdown 研究报告。
- **code_genesis**: 7 阶段 DAG 代码生成工作流（设计→编码→精炼→测试）。
- **环境变量映射**: 自动将 Watery 的 API Key 映射为 ms-agent 所需的 `OPENAI_API_KEY`。

### 9.2 新增 API
- `POST /api/v1/ms-agent/research` — 启动深度研究任务。
- `GET /api/v1/ms-agent/tasks/research/{id}` — 查询研究任务状态/报告。
- `POST /api/v1/ms-agent/code` — 启动代码生成任务。
- `GET /api/v1/ms-agent/tasks/code/{id}` — 查询代码任务状态/输出。
- `GET /api/v1/ms-agent/tasks?type=research|code` — 列出任务。

### 9.3 SkillLoader 双格式支持
- 优先读取 `META.yaml`（ms-agent 原生格式）。
- Fallback 读取 `SKILL.md` YAML frontmatter（Watery Legacy 格式）。

## 10. Phase 6 已实现功能 — Chat Tool Calling + 自我改进 + 前端增强 (2026-02-24)

### 10.1 Chat Tool Calling（对话时自动调用工具）✅
- **ToolRegistry 服务**: 将 SQLite 中所有 SkillMetadata 实时转为 OpenAI function calling 格式，30s TTL 缓存。
- **ModelRouter 升级**: `generate()` 支持传入 `tools` 参数，Volcengine + Gemini 双 Provider 均可返回 `tool_calls`。
- **chat_endpoint 重写**: 自动循环（最多 5 轮）—— LLM 请求 → 解析 tool_calls → SkillExecutor 执行 → 追加结果 → 继续 → 最终回复。
- **Schema 扩展**: `ToolCall`、`ToolCallFunction` 模型；`Message.content` Optional；`ChatResponse` 新增 `tool_results`、`finish_reason`。
- **CRUD 缓存同步**: 技能增删改时自动调用 `tool_registry.invalidate_cache()`。

### 10.2 Worker 自我改进 ✅
- **异步研究触发 (B-1)**: 知识缺口检测后 Fire-and-forget 调用 `ms_agent_service.run_deep_research()`，Worker-01 后台每 60s 轮询结果。
- **报告→技能蒸馏 (B-2)**: 研究完成后自动蒸馏为 1 个 `knowledge` 文档技能 + 最多 3 个 `executable` 技能草案，写入 SQLite + ChromaDB + 脚本文件。
- **知识型技能注入 (B-3)**: `SkillMetadata` 新增 `skill_type` / `knowledge_content` 字段；Worker 遇到 knowledge 技能时将内容注入 LLM system prompt 而非执行脚本。

### 10.3 前端增强 ✅
- **工具调用可视化 (C-1)**: 聊天气泡内嵌工具调用卡片（蓝色成功/红色失败），展示工具名和结果摘要。
- **三 Tab 侧栏看板 (C-2)**: 右侧面板从单一任务看板升级为三 Tab 结构——📋 任务（5s 刷新）| 🔬 研究（30s 刷新，支持新建/详情）| 💻 代码（30s 刷新，支持新建/详情）。

## 11. Phase 7 已实现功能 — 对话 Session 持久化 (2026-03-02)

> **核心目标**：将对话历史从前端 localStorage 迁移至后端 SQLite 持久化存储，实现跨设备会话同步、Token 优化与完整的 Tool Calling 上下文保留。

### 11.1 组 A — 数据模型 + DB 迁移 ✅
- **Conversation 表** (`app/models/database.py`)：`id`(UUID)、`title`(自动命名)、`model`(绑定模型)、`message_count`(冗余计数)、`is_archived`(软删除)、`created_at`、`updated_at`。
- **ConversationMessage 表** (`app/models/database.py`)：`id`(UUID)、`conversation_id`(外键)、`role`(system/user/assistant/tool)、`content`、`tool_calls_json`(完整 Tool Calling 序列化存储)、`tool_call_id`、`token_count`(Token 估算)、`seq`(消息排序序号)、`created_at`。
- **DB 迁移** (`app/core/db.py`)：`_migrate_schema()` 中预留 Phase 7 增量列（`message_count`、`is_archived`、`token_count`），`SQLModel.metadata.create_all()` 自动建新表，已有表无回归。

### 11.2 组 B — 会话 REST API + `/chat` 端点改造 ✅
- **会话 CRUD API**：
  - `POST /api/v1/conversations` — 创建新会话，支持可选 `system_prompt` 自动注入。
  - `GET /api/v1/conversations` — 列出会话（按时间倒序，支持 `?archived=true` 筛选归档）。
  - `GET /api/v1/conversations/{id}` — 获取会话详情 + 全部消息（含 `tool_calls` 反序列化）。
  - `PATCH /api/v1/conversations/{id}` — 更新标题/模型/归档状态（PATCH 语义）。
  - `DELETE /api/v1/conversations/{id}` — 软删除（默认）或 `?hard=true` 硬删除（含级联消息清除）。
  - `POST /api/v1/conversations/{id}/import` — 批量导入历史消息（localStorage 迁移专用）。
- **`/chat` 端点改造**：
  - **双模式支持**：`conversation_id`（新模式，后端加载历史+持久化）与 `messages`（旧模式，完全兼容零回归）二选一。
  - **自动消息持久化** (`_persist_chat_turn`)：每轮对话完成后批量写入 user + tool 中间消息 + final assistant 回复，`seq` 递增、`message_count` 同步更新。
  - **自动标题生成**：首条 user 消息自动截取前 20 字设为会话标题。
  - **ChatResponse 扩展**：新增 `conversation_id` 字段回传前端。
- **Schema 扩展** (`app/models/schemas.py`)：新增 `ConversationCreate`、`ConversationInfo`、`ConversationDetail`、`ConversationUpdate`、`ImportMessagesRequest` 五个 Pydantic 模型；`ChatRequest` 新增 `conversation_id` 可选字段。

### 11.3 组 C — 前端迁移（localStorage → API 驱动）✅
- **会话管理全面 API 化** (`app/web/index.html`)：
  - `renderHistoryList()` → `GET /api/v1/conversations` 渲染侧边栏。
  - `createNewChat()` → `POST /api/v1/conversations` 创建新会话。
  - `loadChat(convId)` → `GET /api/v1/conversations/{convId}` 加载完整消息并渲染（含 Tool Calling 卡片）。
  - `deleteCurrentChat()` → `DELETE /api/v1/conversations/{id}` 删除后自动切换到下一个会话。
- **`sendMessage()` 重写**：只发单条 user 消息 + `conversation_id`，历史由后端管理，不再维护内存 `chats` 对象或 `saveChats()` 到 localStorage。
- **localStorage 仅保留 `wateryLastConversationId`**：记住上次打开的会话，页面加载时恢复。
- **Tool Calling 历史可视化 (C-3)**：加载历史会话时正确渲染 `tool_calls` 为工具调用卡片。
- **自动迁移 (C-4)**：`migrateLocalStorageToBackend()` 在 `window.onload` 中自动检测旧 `wateryChats` 数据，逐个创建会话并通过 `/import` 端点批量导入消息，迁移完成后清除 localStorage 旧数据。

### 11.4 组 D — 长对话 Token 优化 ✅
- **Token 估算** (`_estimate_tokens`)：UTF-8 字节数 / 3 快速估算（适用于中英混合内容）。
- **智能截断策略** (`_truncate_messages`)：
  - 永远保留 `role=system` 消息。
  - 永远保留最近 4 条消息（约最近 2 轮对话）。
  - 从最早的非 system 消息开始删除，直到总 token 数 < 阈值。
  - 截断点自动插入 `[注意: 更早的 N 条消息已因上下文长度限制被省略]` 提示。
- **多模型上下文窗口适配**：`ark-code-latest` 28K、`gemini-2.0-flash` 100K、`gemini-2.5-pro` 200K，按模型自动选择截断阈值。
- **Token 统计写入 DB**：每条 `ConversationMessage` 的 `token_count` 字段在写入时自动填充。

## 12. Phase 10 已实现功能 — 对话自治能力扩展 + 主动空闲检测 (2026-03-02)

> **核心目标**：补齐 AI 在对话中自主管理错题库、联网搜索、对话摘要、飞书推送的能力缺口，
> 并升级 SkillExecutor 超时机制为基于输出活跃度的主动空闲检测。

### 12.1 SkillExecutor 主动空闲检测机制 ✅
- **双层超时策略**（替代旧的单一 `timeout=60s`）：
  - `idle_timeout`（默认 30s）：子进程 stdout/stderr 持续无输出超过此时间 → 判定"卡死" → kill。
  - `max_timeout`（默认 300s）：绝对安全上限，无论是否有输出，超过即 kill。
- **stderr 心跳协议**：长耗时技能通过 `print("[progress] ...", file=sys.stderr, flush=True)` 发送心跳，
  每次输出刷新空闲计时器。快速技能无需任何改动（30s 内完成即可）。
- **`_monitored_communicate()`**：替代 `process.communicate()`，三个并发协程——
  stdout reader + stderr reader + watchdog，实时监控进程活跃度。

### 12.2 新增技能：错题库管理（`error_ledger_crud`）✅
- **操作**：`create` / `list` / `get` / `delete` / `search`
- **能力**：AI 在对话中可自主记录错误经验、按标签筛选查询、删除过时条目。
- **实现**：调用后端 `POST/GET/DELETE /api/v1/errors/entries` API。

### 12.3 新增技能：联网搜索（`web_search`）✅
- **搜索引擎**：EXA（语义搜索，优先） → SerpAPI（Google 搜索，fallback）。
- **能力**：AI 在对话中遇到需要实时信息的问题时自动联网搜索。
- **参数**：`query`（必填）、`num_results`、`search_type`（auto/keyword/neural）、`include_content`。
- **依赖**：仅使用 httpx（已在项目依赖中），需配置 `EXA_API_KEY` 或 `SERPAPI_API_KEY`。

### 12.4 新增技能：对话历史摘要（`conversation_summary`）✅
- **工作流**：获取会话列表 → 按日期过滤 → 加载完整消息 → LLM 生成结构化日报。
- **输出**：Markdown 格式日报（概览 / 关键主题 / 已完成事项 / 待办 / 错误经验 / 技能改进建议）。
- **Token 安全**：超长内容自动截断至 60K 字符（约 20K tokens）。
- **心跳**：每个 API 调用阶段写 stderr 心跳，避免被空闲检测 kill。

### 12.5 新增技能：飞书机器人推送（`feishu_webhook`）✅
- **消息格式**：纯文本（`text`）/ 富文本（`rich_text`）/ 交互卡片（`interactive`）。
- **签名校验**：支持飞书 HMAC-SHA256 签名验证（`FEISHU_WEBHOOK_SECRET`）。
- **配置**：`FEISHU_WEBHOOK_URL` + `FEISHU_WEBHOOK_SECRET`（可选）写入 `.env`。
- **卡片定制**：支持 12 种头部颜色、Markdown 子集正文。

### 12.6 配置扩展 ✅
- **`app/core/config.py`**：新增 `feishu_webhook_url`、`feishu_webhook_secret` 配置项。
