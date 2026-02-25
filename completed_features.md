# Watery AI Agent — 已完成功能速查表

> 本文件是 worklog.md 的精华浓缩版，记录每个 Phase 的最终交付物。  
> 阅读本文件即可快速掌握当前系统全貌，无需翻阅大量历史日志。

---

## Phase 1 — 基础设施 ✅
**目标**：FastAPI 骨架 + 多模型路由 + 前端聊天界面

| 交付物 | 说明 |
|--------|------|
| `app/main.py` | FastAPI 入口，前端静态服务 |
| `app/core/config.py` | Pydantic Settings 环境变量管理 |
| `app/services/model_router.py` | 动态路由火山引擎 / Gemini |
| `app/api/routes.py` | `POST /chat`、`GET /models` |
| `app/web/index.html` | 多会话聊天 UI（localStorage 持久化） |
| `docker-compose.yml` + `Dockerfile` | 容器化，端口 18000 |

---

## Phase 2 — 任务编排 + 代理层 ✅
**目标**：Manager→Worker DAG 任务流 + ChromaDB 记忆 + 内置代理

| 交付物 | 说明 |
|--------|------|
| `app/core/db.py` | SQLite 初始化 + 平滑 Schema 迁移 |
| `app/models/database.py` | `Task`、`SkillMetadata` 表（含 timestamps） |
| `app/services/orchestrator.py` | DAG 任务队列、状态机、级联 fail、启动恢复 |
| `app/services/manager.py` | 意图→任务拆解（UUID ID 防冲突，异步后台执行） |
| `app/services/worker.py` | 3 个并发 Worker，RAG→Executor→LLM 链路 |
| `app/services/executor.py` | 跨语言脚本执行（python/shell/nodejs） |
| `app/services/memory_retriever.py` | ChromaDB run_in_executor 异步封装 |
| `app/services/proxy_manager.py` | Clash 节点订阅、热重载、trust_env=False |
| 新 API | `POST /intention`、`GET /tasks`、`GET /knowledge`、`GET /errors` |
| 前端扩展 | 任务看板（实时轮询）、双模式切换、知识库模态框 |

---

## Phase 3 — 全系统审计修复 ✅
**目标**：10 项 P0/P1/P2 问题全部修复，系统达到生产级稳定

| 问题 | 修复 |
|------|------|
| Worker 未真实执行 | 重写 worker.py，RAG→SkillExecutor→LLM 全链路 |
| 下游任务死锁 | orchestrator 级联 fail 递归逻辑 |
| Skills CRUD 缺失 | `POST/DELETE /skills`，SQLite+ChromaDB 双写 |
| GC 取消后台任务 | `_background_tasks` Set + done_callback 保护 |
| ChromaDB 阻塞主线程 | run_in_executor 全面异步化 |
| 配置散落 os.getenv | 全部归并 Pydantic Settings |
| Task 无时间戳 | `created_at`/`updated_at` + ALTER TABLE 迁移 |
| lifespan 废弃写法 | @asynccontextmanager lifespan 优雅关闭 |

---

## Phase 3.5 — Skills 协议 + 模型更新 ✅
**目标**：标准化 SKILL.md 协议，技能自动加载，错题集自动摄入

| 交付物 | 说明 |
|--------|------|
| `app/services/skill_loader.py` | 解析 SKILL.md YAML frontmatter → SkillCreate |
| `skills/hello_world/` | 示例技能（问候脚本） |
| `skills/run_python_snippet/` | 示例技能（Python 代码执行器） |
| 新 API | `POST /skills/load-dir`、`GET /skills/{id}`、`POST /skills/{id}/run` |
| 错题集自动摄入 | 启动时解析 error_ledger.md → ChromaDB 幂等入库 |
| `POST /errors/ingest` | 手动触发重新摄入 |
| 模型列表更新 | Gemini 3.x 系列 + 火山引擎最新模型 |

---

## Phase 4 — PDF-to-Skills 流水线 ✅
**目标**：AI 读 PDF → 语义分块 → LLM 提炼 → 自动注册技能

| 交付物 | 说明 |
|--------|------|
| `app/services/pdf_processor.py` | 提取→分块→摘要→生成全流水线 |
| `PDFDocument` 数据模型 | SQLite 表 + DB 迁移 |
| `POST /pdf/upload` | 文件上传，返回 doc_id |
| `POST /pdf/to-skills` | 触发流水线（异步，fire-and-forget） |
| `GET /pdf/status/{id}` | 查询处理进度和生成技能列表 |
| `PUT /skills/{id}` | 技能更新端点 |
| `skills/pdf_extract_text/` | PDF 文本提取技能 |
| `skills/pdf_to_skills/` | PDF 转技能流水线技能 |
| `skills/skill_crud/` | 元技能（技能的 CRUD 操作） |
| Worker 知识缺口检测 | L2 距离 > 1.5 → 触发 `_attempt_self_amendment` |

---

## Phase 5 — ms-agent 深度能力集成 ✅
**目标**：集成 ms-agent deep_research / code_genesis / doc_research 三大能力

| 交付物 | 说明 |
|--------|------|
| `app/services/ms_agent_service.py` | CLI 子进程封装，fire-and-forget，状态轮询 |
| `projects/deep_research/` | ms-agent deep_research v2（Researcher+Searcher+Reporter） |
| `projects/code_genesis/` | ms-agent code_genesis（7阶段 DAG 代码生成） |
| `projects/doc_research/` | ms-agent doc_research（文档深度分析，依赖包内置） |
| `ms_agent/skill/` | ms-agent skill 引擎源码（本地副本） |
| 新 API（research） | `POST /research/deep`、`GET /research/{id}`、`GET /research` |
| 新 API（code） | `POST /code/generate`、`GET /code/{id}`、`GET /code` |
| skill_loader.py 升级 | 双格式支持：META.yaml（ms-agent 原生）优先 + SKILL.md frontmatter（legacy）兼容 |
| `app/core/config.py` 扩展 | 新增 `exa_api_key`、`serpapi_api_key`、`modelscope_api_key` |
| `docker-compose.yml` 扩展 | 新增 `./data/outputs` + `./projects` volume 挂载 |
| `requirements.txt` 扩展 | ms-agent>=1.5.0 + faiss-cpu + sentence-transformers + rank_bm25 + omegaconf |
| `.env.example` | 新增 EXA_API_KEY / SERPAPI_API_KEY 配置说明 |

**新技能目录格式（ms-agent 原生）**：
```
skill-name/
├── META.yaml      ← 优先（ms-agent 格式）
├── SKILL.md       ← 纯 Markdown 技能文档
├── scripts/
│   └── main.py
└── requirements.txt
```

---

## 当前系统全貌（截至 2026-02-24）

```
POST /chat                    → 直连对话（Volcengine / Gemini）
POST /intention               → 意图触发（Manager→Worker DAG）
GET  /tasks                   → 任务状态查询

POST /skills                  → 注册技能
GET  /skills                  → 列表
GET  /skills/{id}             → 详情
POST /skills/{id}/run         → 执行技能
PUT  /skills/{id}             → 更新技能
DELETE /skills/{id}           → 删除技能
POST /skills/load-dir         → 批量从目录加载

POST /pdf/upload              → 上传 PDF
POST /pdf/to-skills           → 触发 PDF→技能流水线
GET  /pdf/status/{id}         → 查询流水线进度
GET  /pdf                     → 列出所有 PDF

POST /research/deep           → 触发深度研究（ms-agent）
GET  /research/{id}           → 查询研究进度 + 读取报告
GET  /research                → 列出所有研究任务

POST /code/generate           → 触发代码生成（ms-agent）
GET  /code/{id}               → 查询生成进度 + 产物列表
GET  /code                    → 列出所有代码生成任务

GET  /knowledge               → 知识库浏览
GET  /errors                  → 错题集浏览
POST /errors/ingest           → 重新摄入 error_ledger.md
GET  /models                  → 可用模型列表
GET  /proxy/status            → 代理节点状态
```

---

## 待实现功能（Phase 6 候选）

| 优先级 | 功能 | 说明 |
|--------|------|------|
| P0 | Worker 自我改进升级 | `_attempt_self_amendment` → 调用 `ms_agent_service.run_deep_research()` 研究知识缺口 → 提炼 → 注册技能，替代当前 LLM 凭空生成的低质量方案 |
| P1 | 前端 research/code 任务界面 | 展示深度研究报告 + 代码生成产物，支持进度轮询 |
| P1 | 技能质量自动评估 | Worker 执行技能后自动更新 `quality_score`，淘汰低分技能 |
| P2 | 技能版本管理 | 同一技能支持多版本，回滚能力 |
| P2 | OCR 支持（扫描型 PDF） | Phase 5 预留，可选 tesseract 插件 |
| P3 | 前端全面升级 | 研究看板 + 代码生成 UI + 技能管理界面 |
