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

---

## 后续记录模版
### [日期] 错误类别简述
- **问题描述**: ...
- **发现途径**: ...
- **原因分析**: ...
- **解决方案**: ...
- **预防建议**: ...
