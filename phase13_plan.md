# Phase 13: Error Ledger Reflection & Consolidation (错题集反思与记忆固化机制)

## 1. 目标 (Objective)
建立错题集的“反思与记忆固化”机制。通过大语言模型（LLM）对积累的底层、零散的错误记录进行定期或手动的归纳总结，提取出项目级深层原因和开发原则（Insights/Principles）。
同时，对已被成功总结的原始错误记录进行“软删除”（归档），从而在降低上下文 Token 消耗（提高信噪比）的同时，赋予 AI 系统更强的“举一反三”和泛化纠错能力。

## 2. 核心架构设计 (Core Architecture Design)

### 2.1 双层记忆结构 (Dual-Layer Memory Structure)
我们将错误知识库重构或扩展为双层概念：
1. **Raw Errors (原始错误)**：即现有的具体错误记录，包含报错信息、具体行号、直接的修复步骤。
2. **Systemic Insights (系统级洞察/开发原则)**：由 LLM 归纳出的高级特征，包含：
   - **核心原则/深层原因**（例如：“不要在异步上下文中使用同步锁”、“本系统的数据库 Session 必须在中间件统一管理”）。
   - **代表性案例**（保留 1-2 个具体的代码快照和报错作为抓手，避免过度抽象导致的细节丢失）。

### 2.2 软删除机制 (Soft Deletion / Archiving)
- 对原始错题集数据结构增加状态字段（如 `status: active | summarized`）或直接归档记录。
- AI 在日常开发检索上下文时，**优先并主要检索 Insights 和 active 状态的 Errors**。
- 只有在特定的“源码溯源”或“历史 Debug”场景下，才允许穿透查询 `summarized` 的记录。

## 3. 具体实施步骤 (Implementation Steps)

### Step 1: 数据结构与存储更新 (Schema & Storage Update)
- 检查并更新现有的错题集存储（无论是 Markdown 解析、SQLite 还是 Vector DB）。
- **方案A（结构化数据库）**：在 `models/` 和 DB Schema 中，为错误记录添加 `status` 或 `is_archived` 字段；新建一个 `insights` 或 `principles` 表/集合（或在原表增加 `type` 字段区分）。
- **方案B（Markdown / 文件记忆）**：若基于 `error_ledger.md` 或文件系统，需重构文档格式（分为 `[Active Errors]` 和 `[Systemic Insights]` 区域），并修改相应的 Parser。

### Step 2: 设计反思 Prompt 与 AI 结构化输出 (Reflection Prompt Design)
- 编写专门用于“错误归纳总结”的 System Prompt。
- **Prompt 核心要求**：
  1. 忽略琐碎的语法错误（如拼写错误），重点寻找架构摩擦、依赖冲突、对框架理解的系统性偏差。
  2. 提取出 1~3 条核心开发原则。
  3. 必须保留一个最能说明问题的代码片段作为 Representative Example。
  4. 输出格式需为严格的 JSON，便于后续结构化入库。

### Step 3: 开发归纳业务逻辑 (Consolidation Logic)
- 在 `services/` 层或 `skills/` 层编写务流：
  1. **Fetch**: 查询出所有 `状态为空/未归档 (active)` 且到达一定数量时间阈值的错误记录。
  2. **Batch**: 将这些记录拼接为上下文发给 LLM。
  3. **Process**: 调用 LLM 进行总结，获取结构化的 Insight 数据。
  4. **Save & Archive**: 将新提取的 Insight 入库保存（或追加到文档）；将参与总结的原始 active 记录的状态更新为 `summarized`（或从中删除/移入存档区）。

### Step 4: 触发机制与接口 (Trigger Mechanism & API)
- **手动触发**：创建一个新的 API 路由或 Skill（如 `error_ledger_reflect`），允许主动让 AI 进行自我反思总结。
- **(可选) 自动触发**：设定阈值（如堆积了 5 条以上的 active 错误时，由后台 worker 自动触发提炼）。

### Step 5: 优化上下文检索逻辑 (Update Retrieval Logic)
- 调整系统在生成代码和规划任务时的 Memory/Context 检索逻辑（如 `memory_retriever.py` 或相关 Skill）。
- 确保系统在常规开发时：
  - **高权重包含**：提炼后的 Systemic Insights。
  - **默认包含**：未被总结的最新 Raw Errors (active)。
  - **排除**：已被标记为 `summarized` 的老旧 Raw Errors。

## 4. 验收标准 (Acceptance Criteria)
1. 存在明确的数据区分：可以直接查看到精炼后的“开发原则 (Insights)”以及被归档的“原始错误 (Archived Errors)”。
2. 执行反思任务后：旧的活跃记录状态正确翻转（或被移出核心区），且生成了带有“核心原因+代表案例”的总结体。
3. 检索测试：证明在系统查询历史错误上下文时，提取到的是提炼后的原则，不再被已被归档的长篇报错刷屏。

## 5. 潜在依赖项与受连带影响的文件
- 数据模型层的可能变更：`app/models/schemas.py`, `app/models/database.py` 
- 检索与记忆处理层：`app/services/memory_retriever.py`
- 技能模块：`skills/error_ledger_crud/` 和可能新增的 `skills/error_ledger_reflection/`
- 如果采用纯文本记录，则直接牵连 `error_ledger.md` 或类似的管理文件。