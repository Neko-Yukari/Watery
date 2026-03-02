from __future__ import annotations

from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any


# ============================================================
# Tool Calling — 基础数据模型（A-3）
# ============================================================

class ToolCallFunction(BaseModel):
    """LLM 调用函数的名称和参数。"""
    name: str = Field(..., description="函数名称，对应 SkillMetadata.id 的 sanitize 版本")
    arguments: str = Field(..., description="JSON 字符串，函数调用参数")


class ToolCall(BaseModel):
    """LLM 返回的单个工具调用请求。"""
    id: str = Field(..., description="工具调用 ID，用于关联 role=tool 的响应消息")
    type: str = Field("function", description="调用类型，目前固定为 'function'")
    function: ToolCallFunction


# ============================================================
# 对话消息（A-5）
# ============================================================

class Message(BaseModel):
    """
    对话消息。支持四种 role：
    - system / user / assistant — 标准对话角色
    - tool — 工具执行结果回传（需要 tool_call_id 关联）

    Tool Calling 消息说明：
    - assistant 发起调用时：tool_calls 非空，content 可能为 null
    - tool 角色回传时：tool_call_id 必填，content 为执行结果 JSON 字符串

    多模态消息支持（Phase 9 C-4）：
    - content 可以是 str（文本）或 List[dict]（多模态，含 text 和 image_url 元素）
    """
    role: str = Field(..., description="角色：system / user / assistant / tool")
    content: Optional[Any] = Field(None, description="消息内容。纯文本时为 str；多模态时为 List[dict]（含 text/image_url 元素）")
    # 以下字段仅在 Tool Calling 模式下使用，前端普通对话无需传递
    tool_calls: Optional[List[ToolCall]] = Field(None, description="assistant 消息发起的工具调用列表")
    tool_call_id: Optional[str] = Field(None, description="role=tool 消息需关联的工具调用 ID")


class ChatRequest(BaseModel):
    messages: Optional[List[Message]] = Field(
        None,
        description="消息列表（旧模式：与 conversation_id 二选一；不传 conversation_id 时必填）",
    )
    conversation_id: Optional[str] = Field(
        None,
        description="会话 ID（新模式：后端自动从 DB 加载历史消息并追加结果；与 messages 二选一）",
    )
    model: str = Field("ark-code-latest", description="指定使用的模型名称，默认使用火山引擎自动化模型 ark-code-latest")
    temperature: Optional[float] = Field(0.7, description="生成温度")
    max_tokens: Optional[int] = Field(2048, description="最大生成 token 数")


class ChatResponse(BaseModel):
    """
    对话响应。

    普通对话：content 有内容，tool_calls / tool_results 为 null。
    Tool Calling 完成后：content 为最终 LLM 回复，tool_results 包含所有工具执行记录。
    """
    id: str
    model: str
    content: Optional[str] = Field(None, description="LLM 最终回复文本（tool calling 时中间步骤可能为 null）")
    usage: Dict[str, Any]
    provider: str = Field(..., description="提供商，如 'volcengine', 'gemini'")
    # Tool Calling 扩展字段（A-3）
    tool_calls: Optional[List[ToolCall]] = Field(None, description="LLM 本轮决定调用的工具列表")
    tool_results: Optional[List[Dict[str, Any]]] = Field(None, description="本次对话所有工具执行结果（供前端展示）")
    finish_reason: Optional[str] = Field(None, description="完成原因：stop / tool_calls / length")
    # Phase 7 — 会话持久化
    conversation_id: Optional[str] = Field(None, description="关联的会话 ID（conversation_id 模式下填充，供前端更新侧边栏）")

class SkillManifest(BaseModel):
    """(Legacy) 保留向后兼容"""
    id: str
    name: str = Field(..., description="技能名称")
    description: str = Field("", description="技能描述，用于语义检索")
    language: str = Field("python", description="执行环境")
    content: str = Field("", description="技能代码或执行逻辑")

class SkillCreate(BaseModel):
    """
    技能注册请求体。同时写入 SQLite SkillMetadata 表和 ChromaDB skills_vector 集合。
    """
    id: str = Field(..., description="技能唯一标识，如 'fetch_webpage'")
    name: str = Field(..., description="技能名称")
    description: str = Field(..., description="自然语言描述，用于向量化语义检索")
    language: str = Field("python", description="执行环境，如 'python' / 'shell' / 'nodejs'")
    entrypoint: str = Field(..., description="脚本路径，如 'scripts/skills/fetch.py'")
    parameters_schema: Dict[str, Any] = Field(
        default_factory=dict,
        description="OpenAI Function Calling 格式的参数定义",
    )
    script_content: Optional[str] = Field(
        None,
        description="可选：如果提供则自动将脚本内容写入 entrypoint 指定的文件",
    )
    # Phase 6 B-3 文档型技能
    skill_type: str = Field(
        "executable",
        description="技能类型：executable（可执行脚本）| knowledge（纯文档知识）",
    )
    knowledge_content: Optional[str] = Field(
        None,
        description="文档型技能内容（skill_type=knowledge 时使用，Worker 将其注入 LLM 上下文）",
    )
    # Phase 8 — 标签化错题集关联
    error_tags: List[str] = Field(
        default_factory=list,
        description="该技能关联的错题标签列表（不填时 AI 自动推断）",
    )

class ErrorLog(BaseModel):
    id: Optional[str] = None
    context: str = Field(..., description="触发错误的上下文描述")
    correction: str = Field(..., description="纠错后的建议或代码")
    skill_id: Optional[str] = None

class IntentionRequest(BaseModel):
    intention: str = Field(..., description="用户意图描述")


# ============================================================
# Phase 4 — PDF-to-Skills 数据模型
# ============================================================

class PageContent(BaseModel):
    """单页 PDF 提取结果。"""
    page_number: int
    text: str
    tables: List[List[List[str]]] = Field(default_factory=list, description="该页提取的表格（行/列/单元格）")


class PDFExtractResult(BaseModel):
    """PDF 文本提取的完整结果。"""
    text: str = Field(..., description="全文拼接的纯文本")
    pages: List[PageContent]
    page_count: int
    metadata: Dict[str, Any] = Field(default_factory=dict, description="PDF 元数据（标题/作者等）")


class TextChunk(BaseModel):
    """语义分块结果。"""
    chunk_id: str
    text: str
    source_pages: List[int] = Field(default_factory=list, description="来源页码列表（1-based）")
    heading_path: List[str] = Field(default_factory=list, description="标题层级路径，如 ['第三章', '3.2 xxx']")
    token_count: int = 0


class SkillDraft(BaseModel):
    """LLM 生成的技能草案（JSON 格式直接映射），用于生成 SKILL.md。"""
    skill_name: str
    display_name: str = ""
    description: str
    trigger_conditions: List[str] = Field(default_factory=list)
    execution_logic: str = ""
    input_parameters: Dict[str, Any] = Field(default_factory=dict)
    output_format: str = ""
    tags: List[str] = Field(default_factory=list)
    quality_score: int = Field(3, ge=1, le=5)
    skip_reason: Optional[str] = None


class PipelineResult(BaseModel):
    """PDF-to-Skills 流水线执行结果（返回给前端或写入 PDFDocument）。"""
    pdf_path: str
    total_pages: int
    total_chunks: int
    skills_generated: List[str] = Field(default_factory=list, description="成功生成并注册的技能 ID")
    skills_skipped: int = 0
    skills_registered: int = 0
    errors: List[str] = Field(default_factory=list)


class PDFUploadResponse(BaseModel):
    """POST /pdf/upload 响应体。"""
    doc_id: str
    filename: str
    file_path: str
    page_count: int
    file_hash: str


class PDFToSkillsRequest(BaseModel):
    """POST /pdf/to-skills 请求体。"""
    doc_id: Optional[str] = None
    pdf_path: Optional[str] = None
    skill_prefix: str = ""
    max_tokens_per_chunk: int = Field(6000, ge=1000, le=20000)
    output_dir: str = "/app/skills"


class SkillUpdate(BaseModel):
    """PUT /skills/{skill_id} 请求体，PATCH 语义（只更新提供的字段）。"""
    name: Optional[str] = None
    description: Optional[str] = None
    language: Optional[str] = None
    entrypoint: Optional[str] = None
    parameters_schema: Optional[Dict[str, Any]] = None
    script_content: Optional[str] = None
    tags: Optional[List[str]] = None
    # Phase 6 B-3 文档型技能
    skill_type: Optional[str] = None
    knowledge_content: Optional[str] = None
    # Phase 8 — 标签化错题集关联
    error_tags: Optional[List[str]] = None


# ============================================================
# Phase 5 — ms-agent 深度能力（深度研究 / 代码生成）
# ============================================================

class DeepResearchRequest(BaseModel):
    """POST /research/deep 请求体，触发 ms-agent deep_research v2 工作流。"""
    query: str = Field(..., min_length=5, description="研究问题，支持中英文，建议 ≥ 30 字以获得更好效果")
    model: Optional[str] = Field(None, description="覆盖默认模型名称（不填则沿用 Volcengine 默认）")
    exa_api_key: Optional[str] = Field(None, description="EXA Search API Key（可选，填写后启用高质量网络搜索）")
    serpapi_api_key: Optional[str] = Field(None, description="SerpAPI Key（可选，EXA 的备选搜索引擎）")
    max_rounds: int = Field(6, ge=1, le=20, description="最大研究轮次，默认 6 轮")


class DeepResearchResponse(BaseModel):
    """POST /research/deep 响应体。"""
    task_id: str = Field(..., description="任务 ID，用于后续查询状态")
    status: str = Field("started", description="初始状态总是 'started'")
    work_dir: str = Field(..., description="容器内工作目录路径")
    message: str = Field(..., description="提示信息")


class CodeGenRequest(BaseModel):
    """POST /code/generate 请求体，触发 ms-agent code_genesis 工作流。"""
    query: str = Field(
        ...,
        min_length=10,
        description="代码生成需求描述，例如：'使用 FastAPI 实现一个图片压缩接口，支持 JPEG/PNG，有 EXIF 保留选项'",
    )
    model: Optional[str] = Field(None, description="覆盖默认模型名称")


class CodeGenResponse(BaseModel):
    """POST /code/generate 响应体。"""
    task_id: str = Field(..., description="任务 ID")
    status: str = Field("started", description="初始状态总是 'started'")
    work_dir: str = Field(..., description="容器内工作目录路径")
    message: str = Field(..., description="提示信息")


class MSAgentTaskStatus(BaseModel):
    """
    GET /research/{task_id} 或 GET /code/{task_id} 通用响应体。

    status 枚举：
      - pending    — 任务目录已创建，进程尚未产出任何文件
      - running    — 进程运行中（.watery_status.json 标记为 running）
      - completed  — 进程正常退出
      - failed     — 进程异常退出
      - not_found  — 任务不存在
    """
    task_id: str
    status: str
    task_type: Optional[str] = None
    work_dir: Optional[str] = None
    output_files: List[str] = Field(default_factory=list, description="产物文件列表（相对路径）")
    report: Optional[str] = Field(None, description="最终报告全文（仅 research 类型填充）")
    returncode: Optional[int] = Field(None, description="进程退出码（0=成功）")
    stderr_tail: Optional[str] = Field(None, description="失败时 stderr 末尾 100 行")


class MSAgentTaskListItem(BaseModel):
    """任务列表单项。"""
    task_id: str
    status: str
    task_type: Optional[str] = None


# ============================================================
# Phase 7 — 对话 Session 持久化
# ============================================================

class ConversationCreate(BaseModel):
    """POST /conversations 请求体。"""
    title: Optional[str] = Field("新对话", description="会话标题（可选，后续会根据第一条消息自动更新）")
    model: str = Field("ark-code-latest", description="该会话使用的模型")
    system_prompt: Optional[str] = Field(
        None,
        description="可选的 system prompt，创建时自动插入为第一条 role=system 消息",
    )


class ConversationInfo(BaseModel):
    """会话摘要（列表页使用）。"""
    id: str
    title: str
    model: str
    message_count: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    is_archived: bool = False


class ConversationDetail(BaseModel):
    """会话详情（含完整消息列表，GET /conversations/{id} 使用）。"""
    id: str
    title: str
    model: str
    message_count: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    messages: List[Message] = Field(default_factory=list)


class ConversationUpdate(BaseModel):
    """PATCH /conversations/{id} 请求体，仅更新提供的字段。"""
    title: Optional[str] = None
    model: Optional[str] = None
    is_archived: Optional[bool] = None


class ImportMessagesRequest(BaseModel):
    """POST /conversations/{id}/import 请求体（localStorage 迁移用）。"""
    messages: List[Message] = Field(
        ...,
        description="要批量导入的消息列表（role/content 格式）",
    )


# ============================================================
# Phase 8 — 标签化错题集
# ============================================================

class ErrorEntryCreate(BaseModel):
    """创建错题条目请求体（手动添加或 AI 自动生成）。"""
    title: str = Field(..., description="简短标题")
    context: str = Field(..., description="错误上下文")
    correction: str = Field(..., description="纠正方案")
    prevention: str = Field("", description="预防建议")
    tags: List[str] = Field(default_factory=list, description="标签数组")
    severity: str = Field("warning", description="critical / warning / info")
    source: str = Field("manual", description="manual / auto / task_failure")
    related_skill_ids: List[str] = Field(default_factory=list, description="关联 Skill ID")


class ErrorReflectRequest(BaseModel):
    """手动触发错题归纳总结请求体。"""
    min_entries: int = Field(3, ge=1, le=50, description="至少收集多少条 active raw 错题才触发归纳")
    max_entries: int = Field(20, ge=1, le=200, description="本次最多参与归纳的错题条数")
    archive_instead_of_delete: bool = Field(
        True,
        description="是否归档原始错题而非硬删除；true=标记 summarized，false=直接删除",
    )
    model: Optional[str] = Field(None, description="可选：指定用于归纳的模型")


class ErrorEntryInfo(BaseModel):
    """错题摘要（列表页使用）。"""
    id: str
    title: str
    tags: List[str] = []
    severity: str = "warning"
    source: str = "manual"
    entry_type: str = "raw"
    status: str = "active"
    hit_count: int = 0
    created_at: Optional[str] = None


class ErrorEntryDetail(BaseModel):
    """错题完整详情。"""
    id: str
    title: str
    context: str
    correction: str
    prevention: str = ""
    tags: List[str] = []
    severity: str = "warning"
    source: str = "manual"
    entry_type: str = "raw"
    status: str = "active"
    related_skill_ids: List[str] = []
    summarized_from_ids: List[str] = []
    hit_count: int = 0
    created_at: Optional[str] = None


# ============================================================
# Phase 11 — 代码语义索引
# ============================================================

class CodeSymbolResponse(BaseModel):
    """代码符号查询结果（单条）。"""
    file_path: str
    symbol_name: str
    symbol_type: str
    line_start: int
    line_end: int
    signature: str
    docstring: str
    decorators: List[str] = []
    parent_symbol: Optional[str] = None
    relevance_score: float = Field(0.0, description="语义搜索相关度（0-1，越高越相关）")


class CodeSearchRequest(BaseModel):
    """代码语义搜索请求。"""
    query: str = Field(
        ...,
        description="搜索查询（自然语言），如 '处理 PDF 上传的函数'",
    )
    top_k: int = Field(default=5, ge=1, le=20, description="返回最相关的前 K 条结果")
    symbol_types: Optional[List[str]] = Field(
        default=None,
        description="过滤符号类型，如 ['function', 'method']，不传则不过滤",
    )
    file_pattern: Optional[str] = Field(
        default=None,
        description="文件路径前缀过滤，如 'app/services/' 只搜索 services 目录",
    )


class CodeSearchResponse(BaseModel):
    """代码语义搜索响应。"""
    results: List[CodeSymbolResponse]
    total_indexed: int = Field(description="索引中的总符号数")
    query: str


class IndexStatusResponse(BaseModel):
    """索引状态响应。"""
    total_files: int
    total_symbols: int
    last_indexed_at: Optional[str] = None
    stale_files: int = Field(default=0, description="文件已变更但索引未更新的文件数")


# ──────────────────────────────────────────────────────────────
# Phase 12 — SSE 流式输出事件类型
# ──────────────────────────────────────────────────────────────

class SSEEventType(str):
    """SSE 事件类型枚举常量。"""
    TEXT_DELTA  = "text_delta"   # 文本增量块（逐词推送）
    TOOL_START  = "tool_start"   # 工具调用开始（LLM 决定调用某工具）
    TOOL_RESULT = "tool_result"  # 工具执行结果（工具执行完成）
    DONE        = "done"         # 整轮对话完成信号
    ERROR       = "error"        # 错误信号


class SSEEvent(BaseModel):
    """
    SSE 单条事件数据结构。

    event 类型说明：
    - text_delta:   delta 字段包含本次新增文本片段（空字符串表示 LLM 暂停）
    - tool_start:   tool_name + tool_call_id + arguments 字段有效
    - tool_result:  tool_name + tool_call_id + result + ok 字段有效
    - done:         content（完整拼接的最终文本）+ usage + finish_reason 有效
    - __stream_done__: 内部同步事件，允许附带 tool_calls 供路由层继续执行
    - error:        message 字段包含错误描述
    """
    event: str = Field(..., description="事件类型：text_delta / tool_start / tool_result / done / error")
    # text_delta
    delta: Optional[str] = Field(None, description="[text_delta] 本次新增文本片段")
    # tool_start / tool_result 共用
    tool_name: Optional[str] = Field(None, description="[tool_*] 工具名称")
    tool_call_id: Optional[str] = Field(None, description="[tool_*] 工具调用 ID")
    # tool_start
    arguments: Optional[str] = Field(None, description="[tool_start] 工具参数 JSON 字符串")
    # tool_result
    result: Optional[Dict[str, Any]] = Field(None, description="[tool_result] 工具执行结果")
    ok: Optional[bool] = Field(None, description="[tool_result] 执行是否成功")
    # done
    content: Optional[str] = Field(None, description="[done] 最终完整文本（所有 delta 拼接）")
    usage: Optional[Dict[str, Any]] = Field(None, description="[done] token 用量统计")
    finish_reason: Optional[str] = Field(None, description="[done] 完成原因：stop / tool_calls / length")
    conversation_id: Optional[str] = Field(None, description="[done] 关联会话 ID")
    tool_calls: Optional[List[ToolCall]] = Field(None, description="[__stream_done__] 本轮结构化工具调用列表")
    # error
    message: Optional[str] = Field(None, description="[error] 错误描述")
    error: Optional[str] = Field(None, description="[error] 错误描述（兼容字段）")
