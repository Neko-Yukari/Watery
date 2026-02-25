# Watery AI Error Ledger (错题集)

本文件用于记录在开发、部署及运行过程中遇到的问题、发现途径及解决方案。
Manager Agent 会在任务拆解时“按需”参考此类经验，以避免重复犯错。

---

## [2026-02-22] 基础设施与环境部署类

### 1. 容器内缺失新依赖 (ModuleNotFoundError)
- **问题描述**: 启动 Docker 容器时，后端报错 `ModuleNotFoundError: No module named 'chromadb'`。
- **发现途径**: 运行 `docker-compose logs` 查看容器实时日志。
- **原因分析**: 更新了 `requirements.txt` 但未重新构建 Docker 镜像，导致容器内环境与代码版本不匹配。
- **解决方案**: 运行 `docker-compose up --build -d` 强制重新构建并覆盖旧镜像。
- **预防建议**: 每次修改 `requirements.txt` 或 `Dockerfile` 后，必须执行带 `--build` 的启动命令。

### 2. NumPy 2.0 不兼容导致 ChromaDB 崩溃
- **问题描述**: 报错 `AttributeError: np.float_ was removed in the NumPy 2.0 release`。
- **发现途径**: 容器启动日志。
- **原因分析**: `chromadb` (v0.4.24) 依赖于 NumPy 的旧版别名，而镜像安装了刚发布不久的 `numpy 2.0+`。
- **解决方案**: 在 `requirements.txt` 中将 NumPy 版本限制在 `numpy<2.0.0` (如 `1.26.4`)。
- **预防建议**: 对于底层依赖较重的库（如向量数据库），在生产环境中固化次版本号。

### 3. Pydantic 架构定义缺失 (ImportError)
- **问题描述**: 后端模块加载失败，报错 `ImportError: cannot import name 'SkillManifest' from 'app.models.schemas'`。
- **发现途径**: 容器启动日志。
- **原因分析**: 编写 `memory_retriever.py` 时直接使用了未定义的模型 `SkillManifest`，未在 `schemas.py` 中先行补齐。
- **解决方案**: 在 `app/models/schemas.py` 中补齐 `SkillManifest`, `ErrorLog`, `IntentionRequest` 等核心 Pydantic 类定义。
- **预防建议**: 遵循“架构先行”原则，所有跨模块调用的数据模型需统一在 `schemas.py` 定义后再引用。

## [2026-02-22] 代理与网络通信类

### 1. Shadowsocks 2022 协议兼容性问题 (预判防错)
- **问题描述**: 用户的订阅节点使用了 `2022-blake3-aes-256-gcm` 加密协议，标准版 Clash (Dreamacro) 无法解析和连接此类节点。
- **发现途径**: 架构设计阶段分析用户提供的订阅 YAML 样本。
- **原因分析**: 原版 Clash 已停更，不支持新型 SS-2022 协议。
- **解决方案**: 必须使用 Clash Meta (Mihomo) 内核的 Docker 镜像 (`metacubex/mihomo`) 替代原版 Clash。
- **预防建议**: 在处理现代机场订阅时，默认采用 Mihomo 内核以保证最大协议兼容性。

## [2026-02-22] 代码修改与上下文同步类

### 1. 工具调用时的上下文不匹配 (Replacement Failure)
- **问题描述**: 在修改 `index.html` 插入 JS 逻辑时，`replace_string_in_file` 连续失败。
- **发现途径**: 代理报错 `The string was not found in the file`。
- **原因分析**: 对文件结构的假设（如变量声明顺序）与实际代码不符（例如 `historyList` 实际定义在 `chatMessages` 之后）。
- **解决方案**: 在执行大块替换前，先使用 `read_file` 精确读取目标区域的当前行号和内容。
- **预防建议**: 不要依赖记忆中的文件结构，尤其是多次修改后的文件。始终遵循“先读后写”的原则。

## [2026-02-22] PowerShell 本地基准测试类

### 1. PowerShell Invoke-RestMethod 编码问题
- **问题描述**: 使用 PowerShell 调用 `/api/v1/intention` 时，模型返回“用户意图为乱码”。
- **发现途径**: 后端日志显示 `Processing intention: ????? python ??` 以及模型响应。
- **原因分析**: PowerShell 5.1 在发送 POST 请求时，默认不使用 UTF-8 编码，导致中文丢失。
- **解决方案**: 在 `Invoke-RestMethod` 中显式指定 `-ContentType "application/json; charset=utf-8"` 并手动转换 Byte 数组。
- **预防建议**: 在非中文系统或默认编码不确定的环境下，API 请求必须显式声明字符集。

### 2. Docker Desktop 管道连接失败
- **问题描述**: `docker ps` 或 `docker-compose` 报错 `open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified.`。
- **发现途径**: 命令行尝试启动服务。
- **原因分析**: Docker Desktop 服务已安装但未启动（状态为 `Stopped`）。
- **解决方案**: 需在本地启动 Docker Desktop 应用程序或通过管理员权限启动服务 `com.docker.service`。
- **预防建议**: 系统重启后需检查容器引擎状态。

## [2026-02-22] 端口处理与容器化策略类

### 1. 端口占用与固定化策略 (Port Conflict & Hardcoding)
- **问题描述**: 运行 `uvicorn` 或 `docker-compose up` 时报错 `[Errno 13] error while attempting to bind on address ('0.0.0.0', 8000)`。
- **发现途径**: 命令行启动日志。
- **原因分析**: 宿主机上已有其他服务（或残留的 uvicorn 进程，如系统中的 `Manager` 进程）占用了 8000 端口。部分候选端口（如 7786）位于 Windows Hyper-V 的排除端口范围内。
- **解决方案**: 为避开宿主机所有可能的冲突及 Windows 保留端口，系统决定将默认服务端口统一固定为 **18000**。此更改已在 `Dockerfile` (EXPOSE/CMD) 和 `docker-compose.yml` (ports/command) 中同步硬编码，作为项目的正式部署标准。
- **预防建议**: 部署前使用 `netstat -ano | findstr :18000` 检查。若仍有冲突，应采用类似的避让策略并在文档中备案。

### 2. Docker Hub 镜像拉取超时 (Timeout)
- **问题描述**: `docker-compose up` 遇到 `failed to resolve source metadata: context deadline exceeded`。
- **发现途径**: 镜像构建日志。
- **原因分析**: 网络环境限制导致无法直接连接 Docker 官方 Registry。
- **解决方案**: 在 Docker Desktop 中配置系统级别的代理（HTTP/HTTPS Proxy），或修改 `config.json` 加入 `proxies` 映射。本项目中已尝试通过宿主机代理 `127.0.0.1:7897` 辅助拉取。
- **预防建议**: 在国内环境下，优先配置稳定可靠的 Docker 镜像加速器或全局代理。

### 3. 容器间通信受宿主机代理环境变量干扰 (502 Bad Gateway)
- **问题描述**: `api` 容器在调用 `clash` 容器的 REST API (`http://clash:9090`) 时，返回 `502 Bad Gateway`。
- **发现途径**: 容器日志显示 `httpx` 请求报错，且前端代理状态一直显示异常。
- **原因分析**: `api` 容器继承了宿主机的 `HTTP_PROXY` 环境变量（指向宿主机的 7897 端口）。当 `httpx` 尝试访问 `clash:9090` 时，错误地将请求发给了宿主机的代理，而宿主机代理无法解析 Docker 内部网络域名 `clash`。
- **解决方案**: 在 Python 代码中，对于明确的内部容器间调用，初始化 `httpx.AsyncClient` 时显式传入 `trust_env=False`，以忽略系统代理环境变量。
- **预防建议**: 在 Docker 编排中，内部服务互调必须警惕全局代理环境变量的污染。

## [2026-02-22] 任务编排系统 (Orchestrator) 类

### 1. Manager Agent 生成固定 Task ID 导致 UNIQUE 约束冲突
- **问题描述**: 调用 `/api/v1/intention` 时，任务入库报错 `sqlite3.IntegrityError: UNIQUE constraint failed: task.id`，任务完全无法写入，任务面板始终为空。
- **发现途径**: API 响应体返回 `{"status": "error", "message": "Parse error: ..."}` 并可在后端日志中看到 `IntegrityError`。
- **原因分析**: Manager Agent 的 Prompt 设计中要求模型输出 `task_1`、`task_2` 等字面 ID。当用户对同一个意图提交多次（或同类意图）时，这些固定 ID 与数据库中已存在的记录产生 PRIMARY KEY 冲突。
- **解决方案**: 在 Python 侧对模型输出的任务分配 UUID（`str(uuid.uuid4())`），并维护一张 `旧ID -> 新UUID` 映射表，在完成替换后再将依赖字段中的引用统一换为新 UUID，最终写入数据库。
- **预防建议**: LLM 生成的 ID 仅作为本次会话内的临时引用，绝不作为数据库主键。主键生成必须由代码侧负责。

### 2. asyncio.Queue 为纯内存结构，容器热重载后 PENDING 任务永远挂起
- **问题描述**: 容器因代码热重载（`uvicorn --reload`）重启后，已写入 SQLite 的 PENDING/RUNNING 任务全部卡死，Worker 无法再认领，任务面板无任何进展。
- **发现途径**: 前端任务看板观察到任务始终停在 `pending` 状态不变化；后端日志无任何 Worker 认领日志。
- **原因分析**: `asyncio.Queue` 仅在内存中存在，进程重启后队列为空。之前由 `orchestrator.add_tasks()` 推入队列的任务 ID 已丢失，而数据库中对应记录仍保持 `PENDING` 状态，形成了"数据库有任务，队列没有"的死锁状态。另外，RUNNING 状态的任务因为 Worker 进程中断也永远无法被标记为完成。
- **解决方案**: 在 `startup_event` 中新增 `orchestrator.recover_pending_tasks()` 调用：(1) 将所有状态为 `RUNNING` 的任务重置为 `PENDING`（因为上一个 Worker 已经中断）；(2) 扫描所有 `PENDING` 任务，若其所有依赖均已 `COMPLETED`，则重新推入 `asyncio.Queue`。
- **预防建议**: 凡是将 DB 作为持久层、内存队列作为调度层的架构，必须在启动时实现队列恢复逻辑，否则任何进程重启都会导致任务积压。

---

## [2026-02-23] 代理与网络通信类（续）

### 4. Gemini API 报 400 "User location not supported" — 代理配置未正确生效
- **问题描述**: 通过前端调用 Gemini 模型时，API 返回 `Error code: 400 - User location is not supported for the API use.`，代理连接形同虚设，请求以中国大陆 IP 直连 Google。
- **发现途径**: 前端界面选择 Gemini 模型后出现红色错误提示，后端日志显示 400 响应。
- **原因分析**:
  1. `model_router.py` 中通过 `os.getenv("GEMINI_PROXY_URL")` 读取代理地址，而非从 Pydantic Settings 统一读取（违反 P1-3 原则）。
  2. 使用了 `httpx.AsyncHTTPTransport(proxy=...)` 包装后再传入 `transport=` 参数，这种方式对 HTTPS 目标（`generativelanguage.googleapis.com`）的 CONNECT 隧道支持不稳定。
  3. `httpx.AsyncClient` 未设置 `trust_env=False`，宿主机 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量可能与显式代理设置冲突。
- **解决方案**: 将 Gemini 客户端的 httpx 代理配置改为 `httpx.AsyncClient(proxy=settings.proxy_url, trust_env=False)`，直接在 Client 层面指定代理，同时移除对 `os.getenv` 的依赖，统一归并到 `settings.proxy_url`（对应 `PROXY_URL` 环境变量，默认 `http://clash:7890`）。
- **预防建议**: 严格遵循「配置统一入口」原则：所有可配置参数必须通过 Pydantic Settings 管理，业务代码中禁止直接调用 `os.getenv()`。使用 httpx 代理时首选 `AsyncClient(proxy=..., trust_env=False)` 模式而非 Transport 包装。

### 5. .env 变量名与 Pydantic Settings 字段名不匹配 — 订阅地址永远读不到
- **问题描述**: ProxyManager 每次启动都输出 "No PROXY_SUB_URL provided, skipping update."，Clash 配置文件中的 `Gemini-Pool` 代理组始终是空节点，不走代理。
- **发现途径**: 定位 §4 问题时通过日志发现订阅未触发。
- **原因分析**: `config.py` 中字段定义为 `subscription_url`，对应的 env var 应为 `SUBSCRIPTION_URL`；但 `.env` 里写的是 `PROXY_SUB_URL`，Pydantic Settings 无法识别，读为 `None`。
- **解决方案**: 将 `.env` 中的 `PROXY_SUB_URL=...` 改为 `SUBSCRIPTION_URL=...`；同时将 `GEMINI_PROXY_URL` 改为 `PROXY_URL`（匹配 `settings.proxy_url` 字段）。需 `docker-compose up -d` 重建容器使新环境变量生效（`docker restart` 不会重读 env_file）。
- **预防建议**: Pydantic Settings 字段名与 `.env` 变量名必须对应（Settings 字段名大写即为 env key）。新增配置字段时同步检查 `.env` 文件。修改 `.env` 后必须用 `docker-compose up -d` 而非 `docker restart` 使改动生效。

---

## [2026-02-23] AI 工具使用规范类

### 1. 联网搜索模型列表时未核对当前日期，导致信息过期
- **问题描述**: 在用搜索引擎查询可用模型列表时，返回了旧版文档的模型名（如 `gemini-2.0-flash`、`gemini-1.5-flash` 等已对新用户禁用的型号），导致 `model_router.py` 的列表不准确。
- **发现途径**: 用户反馈前端模型列表不全；实测部分模型名返回 404。
- **原因分析**: 网络搜索结果倾向于索引历史受欢迎的文档页面，AI 模型迭代极快（季度级），不先确认当前日期就直接采用搜索结果会引入已废弃的型号。本次操作日期为 **2026-02-23**，而搜索结果中有大量 2024-2025 年的旧文档被混入。
- **解决方案**: 联网查询 API 文档（尤其是模型列表、SDK 版本）前，**必须先明确当前日期**，并在搜索 query 中加入年份限定（如 `site:ai.google.dev gemini models 2026`），或优先通过 API 自身的 `ListModels` 端点实时获取，而不依赖静态文档。
- **预防建议**: 任何涉及"当前可用版本/模型/API"的查询，遵循以下顺序：① 确认今天日期 → ② 调用官方 API 实时列举（如 `GET /v1beta/models`）→ ③ 对照官方 changelog 验证 → ④ 最后才参考第三方搜索结果。

---

## [2026-02-24] Phase 4 架构设计约束与防错经验

### 1. PDF 处理库选型 — 优先纯 Python 方案
- **问题描述**: ms-agent/PDF-Skill 方案引入了 poppler-utils、qpdf、pdftk、tesseract-ocr 等系统级命令行工具，导致 Docker 镜像体积增加 200-400MB，且需要 apt-get 安装系统包。
- **发现途径**: 阅读参考文章技术栈对比后，结合项目 Docker 构建流程分析。
- **原因分析**: 系统级工具（C/C++ 编译的二进制）虽性能强，但大幅增加镜像构建时间和体积，且跨平台兼容性差。对于主流文字型 PDF（书籍、手册、报告），pypdf + pdfplumber 的纯 Python 方案已足够。
- **解决方案**: Phase 4 仅使用 `pypdf>=4.0.0`（基础文本提取）和 `pdfplumber>=0.11.0`（表格识别 + 高级文本提取），不引入 OCR 和系统级工具。扫描型 PDF 支持留待 Phase 5 以可选插件方式引入。
- **预防建议**: 引入新依赖前，始终评估「纯 Python vs 系统级」的代价。Docker 构建中优先选择纯轮子（wheel）依赖，避免需要 `apt-get` 或编译的包。

### 2. PDF 分块策略 — 切勿按固定页数机械分割
- **问题描述**: 简单的"每 N 页一块"分割会破坏章节完整性，导致 LLM 无法理解分块内容的上下文，生成的技能质量极低。
- **发现途径**: 分析 pdf2skill 文章中的"语义密度分析"理念对比固定分页方案。
- **原因分析**: PDF 的页边界与知识边界不对齐（一个概念可能跨 2-3 页，一页可能包含多个独立概念）。固定分页会在句子/段落中间截断，丢失上下文。
- **解决方案**: 采用三级递降分块算法——① 按标题层级（#/##/###）切分章节优先 → ② 按段落（双换行）切分 → ③ 超长段落按 Token 窗口滑动（带 200 token overlap）。表格保持完整不跨块拆分。每个 Chunk 标注 heading_path 和 source_pages。
- **预防建议**: 任何涉及自然语言文档切分的场景，优先选择语义边界（标题、段落）而非物理边界（页码、字节数）。

### 3. LLM 生成 YAML 的格式可靠性远低于 JSON
- **问题描述**: 若要求 LLM 直接输出 YAML 格式的 SKILL.md frontmatter，缩进错误、冒号转义、多行字符串处理等问题导致高频解析失败。
- **发现途径**: 对比 pdf2skill 的 SkillDraft 输出格式设计。
- **原因分析**: YAML 对缩进敏感，LLM token-by-token 生成时缩进容易出错；JSON 用花括号/方括号显式界定结构，解析容错性高。
- **解决方案**: LLM 输出 SkillDraft 一律使用 JSON 格式，再由代码程序（`generate_skill_md()`）将 JSON 转化为 YAML frontmatter + Markdown。
- **预防建议**: 凡 LLM 结构化输出场景，默认使用 JSON；仅在最终呈现层面（如 SKILL.md 文件）才转换为 YAML。

---

## [2026-02-24] Phase 5 — ms-agent 集成类

### 1. ms-agent 版本约束过高导致构建失败
- **问题描述**: `docker compose up --build` 失败，报 `ERROR: No matching distribution found for ms-agent>=1.6.0`。
- **发现途径**: Docker 构建日志。
- **原因分析**: ms-agent 1.6.0 尚未正式发布（仅有 rc0、rc1 候选版），PyPI 上最新稳定版为 1.5.2。
- **解决方案**: 将 `requirements.txt` 中的版本约束从 `>=1.6.0` 改为 `>=1.5.0`。
- **预防建议**: 为尚处于 RC 阶段的依赖指定版本前，先通过 `pip index versions <package>` 确认最新稳定版本号。

### 2. replace_string_in_file 遗留残余代码导致 SyntaxError
- **问题描述**: 容器启动失败，报 `SyntaxError: unterminated triple-quoted string literal` at `skill_loader.py:289`。
- **发现途径**: `docker compose logs api`。
- **原因分析**: 对 `skill_loader.py` 做大块替换时，旧类定义的结尾 `"""` + 重复的 `skill_loader = SkillLoader()` 未被删除干净，文件末尾混入了孤立的三引号字符串。
- **解决方案**: 用 `read_file` 确认文件末尾内容后，用 `replace_string_in_file` 精确删除重复段落。
- **预防建议**: 做结构性大改（整块替换类定义）时，替换完成后立即用 `read_file` 验证文件末尾，确认无残余孤立符号。文件操作遵循「改→验→提交」三步。

---

## 后续记录模版
### [日期] 错误类别简述
- **问题描述**: ...
- **发现途径**: ...
- **原因分析**: ...
- **解决方案**: ...
- **预防建议**: ...
