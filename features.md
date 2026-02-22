# Watery AI Agent - 当前功能清单 (Feature List)

本文档记录了 Watery AI Agent 系统截至目前（Phase 2）已实现的核心功能。

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
- **任务队列 (Orchestrator)**: 
  - 基于 SQLite (`sqlmodel`) 实现了任务状态追踪（PENDING, RUNNING, COMPLETED, FAILED）。
  - 基于 `asyncio.Queue` 实现了内存中的任务分发队列。
  - 支持任务依赖管理，只有前置任务完成后，后续任务才会被放入执行队列。
- **后台执行 (Worker Agent)**: 
  - 系统启动时自动运行后台 Worker 进程。
  - Worker 持续轮询任务队列，认领并“执行”任务（目前执行逻辑为模拟的占位符）。

## 4. 记忆与 RAG 系统 (Memory & RAG)
- **向量数据库**: 集成了 ChromaDB 作为本地向量搜索引擎。
- **技能库 (Skills Vector)**: 建立了技能集合，支持存储和检索不同语言（Python, Shell 等）的技能描述。
- **错题集 (Error Ledger)**: 建立了错误账本集合，用于记录历史错误和纠正方案。
- **按需检索**: Manager Agent 在拆解任务前，会根据用户意图自动从 ChromaDB 检索相关的技能和防错经验，作为上下文注入到 Prompt 中，以提高任务拆解的准确性并节省 Token。
- **知识沉淀**: 建立了人工可读的 `error_ledger.md` 文档，用于记录开发和部署过程中的重大问题。

## 5. 监控与诊断
- **健康检查**: 提供 `/health` 接口用于监控服务状态。
- **任务看板 API**: 提供 `/api/v1/tasks` 接口，可查询当前系统中所有任务的执行状态和依赖关系。
