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
