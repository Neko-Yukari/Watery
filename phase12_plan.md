# Phase 12 — 流式 SSE 输出 + OpenAI 兼容层 详细任务分解

> **[ARCHITECT] 模式产出**  
> 日期：2026-03-02  
> 目标：  
> 1. **Phase 12a（方案 B）**：为 `/api/v1/chat` 新增 SSE 流式端点 `/api/v1/chat/stream`，实现逐词增量输出、Tool Calling 中间步骤实时展示，体验对标 GitHub Copilot Chat。  
> 2. **Phase 12b（方案 C）**：在 12a 基础上新增 OpenAI 兼容端点 `POST /v1/chat/completions`，让 Open WebUI、LobeChat、LangChain 等任何支持自定义 Base URL 的工具都能接入 Watery。  
>
> **完成状态：组 A 🔲 | 组 B 🔲 | 组 C 🔲 | 组 D 🔲 | 组 E 🔲**

---

## 背景与问题诊断

### 根本原因

系统当前采用**完整响应模式（Non-Streaming）**，调用链路均无流式支持：

| 层级 | 当前状态 | 问题 |
|------|----------|------|
| `model_router.py` | `chat.completions.create()` 无 `stream=True` | 等待 LLM 完整响应后才返回 |
| `routes.py /chat` | 整个 Tool Calling 循环（多轮）跑完才 `return` | 无任何中间输出 |
| 前端 `sendMessage()` | `await fetch(...) → await response.json()` | 阻塞等待完整 JSON |
| 前端渲染 | 一次性渲染整块 HTML | 无增量 DOM 更新 |

### 架构决策（经架构师会话确认）

1. ✅ **所有 Tool Calling 中间轮次的文本均需流式输出**（对标 GitHub Copilot）
2. ✅ **DB 持久化时机**：`done` 事件之后，一次性写入（不在流式过程中写 DB）
3. ✅ **旧端点 `POST /api/v1/chat` 保留不变**（供 `worker.py`、`pdf_processor.py`、`orchestrator.py` 等内部服务继续使用）
4. ✅ **先实现 12a（SSE 自定义端点），再实现 12b（OpenAI 兼容端点）**

---

## 功能分组 & 优先级

| 组 | 名称 | 优先级 | 阻塞关系 |
|---|------|--------|---------|
| **A** | SSEEvent Schema 定义 | P0 | 无阻塞 |
| **B** | `model_router.py` 新增 `generate_stream()` | P0 | 依赖 A（SSEEvent 类型） |
| **C** | `routes.py` 新增 `/chat/stream` 端点 | P0 | 依赖 A + B |
| **D** | 前端 `index.html` 改用 SSE 消费 | P0 | 依赖 C（端点就绪） |
| **E** | OpenAI 兼容端点 `POST /v1/chat/completions` | P1 | 依赖 B（`generate_stream()` 就绪） |

**建议实施顺序**：A → B → C → D → E

---

## 组 A — SSEEvent Schema 定义

### 当前状态

- `app/models/schemas.py` 共 426 行，末尾最后一个类是 `IndexStatusResponse`（约第 420 行）
- `ChatRequest` 和 `ChatResponse` 均无 `stream` 字段，无任何 SSE 相关类型

### 目标

在 `app/models/schemas.py` 末尾追加 SSE 事件数据结构。

### 任务 A-1：在 `app/models/schemas.py` 末尾追加 SSE 相关类

在文件末尾（最后一行之后）**追加**以下代码：

```python
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
    # error
    message: Optional[str] = Field(None, description="[error] 错误描述")
```

---

## 组 B — `model_router.py` 新增 `generate_stream()` 方法

### 当前状态

- `app/services/model_router.py` 共 249 行
- `ModelRouter` 类有 `generate()`、`_call_volcengine()`、`_call_gemini()` 方法，全部为**非流式**
- 文件末尾是 `model_router = ModelRouter()`（全局单例）
- 内部已使用 `AsyncOpenAI` SDK，完全支持 `stream=True`

### 目标

在 `ModelRouter` 类内（`model_router = ModelRouter()` 全局单例之前）新增以下三个方法：
1. `generate_stream()` — 统一流式入口（AsyncGenerator）
2. `_call_volcengine_stream()` — 火山引擎流式实现
3. `_call_gemini_stream()` — Gemini 流式实现

### 任务 B-1：添加必要 import

在 `app/services/model_router.py` 文件顶部，修改 `from typing` 那一行：

**当前第 4 行**：
```python
from typing import Any, Dict, List, Optional
```

**替换为**：
```python
from typing import Any, AsyncGenerator, Dict, List, Optional
from app.models.schemas import SSEEvent
```

> ⚠️ 注意：`SSEEvent` 的 import 要和 `Message, ChatResponse, ToolCall, ToolCallFunction` 放在同一个或分开的 import 语句里均可，但需确保不循环导入。建议单独一行。

### 任务 B-2：在 `_call_volcengine` 方法结束后、`# 全局单例` 注释之前，插入三个新方法

定位锚点（`app/services/model_router.py` 文件末尾部分）：

```python
        except Exception as e:
            logger.error(f"Error calling Volcengine API: {str(e)}")
            raise

# 全局单例
model_router = ModelRouter()
```

在 `# 全局单例` 之前（即 `raise` 之后的空行处），**插入**以下完整代码：

```python
    async def generate_stream(
        self,
        messages: List[Message],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
    ) -> AsyncGenerator[SSEEvent, None]:
        """
        统一流式生成入口（AsyncGenerator）。

        yield 事件顺序：
        1. text_delta  × N  — LLM 逐词输出（如果本轮有文本）
        2. tool_start  × M  — LLM 决定调用工具（如果本轮有 tool_calls）
        最终由调用方（routes.py /chat/stream）处理 tool_result 和 done。

        Args:
            messages:    对话消息列表。
            model:       指定模型，为 None 时使用默认模型。
            temperature: 生成温度。
            max_tokens:  最大 token 数。
            tools:       OpenAI tool definitions 列表；None 时不启用工具调用。
            tool_choice: 工具选择策略，默认 'auto'。

        Yields:
            SSEEvent — text_delta 或 tool_start 事件。
        """
        provider, selected_model = self._select_model(model)
        logger.info(f"[stream] Routing to provider={provider}, model={selected_model}")

        if provider == "volcengine":
            async for event in self._call_volcengine_stream(
                messages, selected_model, temperature, max_tokens, tools, tool_choice
            ):
                yield event
        elif provider == "gemini":
            async for event in self._call_gemini_stream(
                messages, selected_model, temperature, max_tokens, tools, tool_choice
            ):
                yield event
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    async def _call_volcengine_stream(
        self,
        messages: List[Message],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
    ) -> AsyncGenerator[SSEEvent, None]:
        """
        火山引擎流式实现。

        使用 stream=True 调用 OpenAI SDK，逐块 yield text_delta 事件；
        当检测到 tool_calls 时，先 yield tool_start 事件。

        最终返回一个特殊的 _stream_done 事件（event="__stream_done__"），
        携带完整的 tool_calls 列表和 usage 信息，供调用方处理。
        """
        formatted_messages = self._format_messages(messages)
        create_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": formatted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            create_kwargs["tools"] = tools
            create_kwargs["tool_choice"] = tool_choice
            # 流式 + tool_calls 需要 stream_options 才能拿到 usage
            create_kwargs["stream_options"] = {"include_usage": True}

        try:
            stream = await self.volcengine_client.chat.completions.create(**create_kwargs)

            # 累积 tool_calls（流式时 tool_calls 是分块到达的）
            accumulated_tool_calls: Dict[int, Dict] = {}
            full_content = ""
            finish_reason = None
            usage = {}

            async for chunk in stream:
                # usage 块（stream_options 启用时，最后一个 chunk 携带）
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = chunk.usage.model_dump() if hasattr(chunk.usage, "model_dump") else {}

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                finish_reason = choice.finish_reason or finish_reason
                delta = choice.delta

                # 文本增量
                if delta.content:
                    full_content += delta.content
                    yield SSEEvent(event="text_delta", delta=delta.content)

                # tool_calls 增量（OpenAI 流式 tool_calls 是分块到达的）
                if delta.tool_calls:
                    for tc_chunk in delta.tool_calls:
                        idx = tc_chunk.index
                        if idx not in accumulated_tool_calls:
                            accumulated_tool_calls[idx] = {
                                "id": tc_chunk.id or "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc_chunk.id:
                            accumulated_tool_calls[idx]["id"] = tc_chunk.id
                        if tc_chunk.function:
                            if tc_chunk.function.name:
                                accumulated_tool_calls[idx]["function"]["name"] += tc_chunk.function.name
                            if tc_chunk.function.arguments:
                                accumulated_tool_calls[idx]["function"]["arguments"] += tc_chunk.function.arguments

            # 流结束后，yield tool_start 事件（每个工具一个）
            for idx in sorted(accumulated_tool_calls.keys()):
                tc = accumulated_tool_calls[idx]
                yield SSEEvent(
                    event="tool_start",
                    tool_name=tc["function"]["name"],
                    tool_call_id=tc["id"],
                    arguments=tc["function"]["arguments"],
                )

            # 内部完成信号，携带元数据（由 routes.py 消费，不推送给前端）
            yield SSEEvent(
                event="__stream_done__",
                content=full_content,
                finish_reason=finish_reason,
                usage=usage,
                # 将 accumulated_tool_calls 序列化进 message 字段（临时复用字段传递复杂对象）
                message=json.dumps(
                    [accumulated_tool_calls[i] for i in sorted(accumulated_tool_calls.keys())],
                    ensure_ascii=False,
                ) if accumulated_tool_calls else None,
            )

        except Exception as e:
            logger.error(f"[stream] Volcengine stream error: {e}")
            yield SSEEvent(event="error", message=str(e))

    async def _call_gemini_stream(
        self,
        messages: List[Message],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
    ) -> AsyncGenerator[SSEEvent, None]:
        """
        Gemini 流式实现（通过 Clash 代理）。

        逻辑与 _call_volcengine_stream 完全相同，使用 self.gemini_client。
        """
        formatted_messages = self._format_messages(messages)
        create_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": formatted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            create_kwargs["tools"] = tools
            create_kwargs["tool_choice"] = tool_choice
            create_kwargs["stream_options"] = {"include_usage": True}

        try:
            stream = await self.gemini_client.chat.completions.create(**create_kwargs)

            accumulated_tool_calls: Dict[int, Dict] = {}
            full_content = ""
            finish_reason = None
            usage = {}

            async for chunk in stream:
                if hasattr(chunk, "usage") and chunk.usage:
                    usage = chunk.usage.model_dump() if hasattr(chunk.usage, "model_dump") else {}

                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                finish_reason = choice.finish_reason or finish_reason
                delta = choice.delta

                if delta.content:
                    full_content += delta.content
                    yield SSEEvent(event="text_delta", delta=delta.content)

                if delta.tool_calls:
                    for tc_chunk in delta.tool_calls:
                        idx = tc_chunk.index
                        if idx not in accumulated_tool_calls:
                            accumulated_tool_calls[idx] = {
                                "id": tc_chunk.id or "",
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        if tc_chunk.id:
                            accumulated_tool_calls[idx]["id"] = tc_chunk.id
                        if tc_chunk.function:
                            if tc_chunk.function.name:
                                accumulated_tool_calls[idx]["function"]["name"] += tc_chunk.function.name
                            if tc_chunk.function.arguments:
                                accumulated_tool_calls[idx]["function"]["arguments"] += tc_chunk.function.arguments

            for idx in sorted(accumulated_tool_calls.keys()):
                tc = accumulated_tool_calls[idx]
                yield SSEEvent(
                    event="tool_start",
                    tool_name=tc["function"]["name"],
                    tool_call_id=tc["id"],
                    arguments=tc["function"]["arguments"],
                )

            yield SSEEvent(
                event="__stream_done__",
                content=full_content,
                finish_reason=finish_reason,
                usage=usage,
                message=json.dumps(
                    [accumulated_tool_calls[i] for i in sorted(accumulated_tool_calls.keys())],
                    ensure_ascii=False,
                ) if accumulated_tool_calls else None,
            )

        except Exception as e:
            logger.error(f"[stream] Gemini stream error: {e}")
            yield SSEEvent(event="error", message=str(e))
```

> ⚠️ **注意**：`_call_volcengine_stream` 和 `_call_gemini_stream` 内部用到了 `json`，需确认文件顶部已有 `import json`（当前文件第 1 行已有）。

---

## 组 C — `routes.py` 新增 `/chat/stream` 端点

### 当前状态

- `app/api/routes.py` 共 2317 行
- `POST /chat` 端点（`chat_endpoint`）在第 107 行，实现了完整的 Tool Calling 循环
- `_persist_chat_turn()` 辅助函数在第 371 行，供持久化使用
- `_MAX_TOOL_ROUNDS = 10`、`_AGENT_SYSTEM_PROMPT` 常量在文件顶部区域定义

### 目标

在 `chat_endpoint` 函数结束之后（约第 312 行的 `except` 块之后）、`_estimate_tokens` 之前，新增 `chat_stream_endpoint` 函数。

### 任务 C-1：在 `routes.py` 添加必要 import

在文件顶部的 `from fastapi import` 那一行，补充 `StreamingResponse`（`Response` 也需要）：

**当前**：
```python
from fastapi import APIRouter, Body, File, HTTPException, UploadFile
```

**替换为**：
```python
from fastapi import APIRouter, Body, File, HTTPException, Response, UploadFile
from fastapi.responses import StreamingResponse
```

同时，在 `from app.models.schemas import (` 的导入块中，追加 `SSEEvent`：

**当前导入列表末尾**（找到 `SkillUpdate,` 那一行）：
```python
    SkillUpdate,
)
```

**替换为**：
```python
    SkillUpdate,
    SSEEvent,
)
```

### 任务 C-2：新增 `chat_stream_endpoint` 端点

定位锚点（`_estimate_tokens` 函数之前的注释行）：

```python
# ---- Phase 7 辅助函数 ----

def _estimate_tokens(text: str) -> int:
```

在这行**之前**，插入以下完整代码：

```python
@router.post("/chat/stream", summary="流式聊天接口（SSE，支持 Tool Calling 中间步骤实时推送）")
async def chat_stream_endpoint(request: ChatRequest):
    """
    与 POST /chat 功能相同，但以 SSE（Server-Sent Events）格式流式返回结果。

    **SSE 事件流格式**（每条事件为 `data: <JSON>\\n\\n`）：

    | event        | 说明                              | 关键字段                          |
    |-------------|-----------------------------------|---------------------------------|
    | text_delta  | LLM 逐词输出的文本增量              | delta                           |
    | tool_start  | LLM 决定调用某工具（立即推送）        | tool_name, tool_call_id, arguments |
    | tool_result | 工具执行完成                        | tool_name, tool_call_id, result, ok |
    | done        | 整轮对话完成（持久化已写入）           | content, usage, finish_reason, conversation_id |
    | error       | 发生错误                            | message                         |

    **DB 持久化时机**：在 `done` 事件发出之前完成（对话链路原子写入）。
    **旧端点 POST /chat 保留不变**，内部服务（worker/pdf_processor 等）继续使用。
    """
    async def _event_generator():
        try:
            conv_id = request.conversation_id

            # ---- 1. 加载消息（逻辑与 chat_endpoint 完全一致） ----
            if conv_id:
                with Session(engine) as session:
                    conv = session.get(Conversation, conv_id)
                    if not conv:
                        yield f'data: {SSEEvent(event="error", message=f"Conversation \'{conv_id}\' not found.").model_dump_json()}\n\n'
                        return
                    db_msgs = session.exec(
                        select(ConversationMessage)
                        .where(ConversationMessage.conversation_id == conv_id)
                        .order_by(ConversationMessage.seq.asc())
                    ).all()

                from app.models.schemas import ToolCall as ToolCallSchema
                messages: list = []
                for m in db_msgs:
                    msg = Message(
                        role=m.role,
                        content=m.content,
                        tool_call_id=m.tool_call_id,
                    )
                    if m.tool_calls_json:
                        try:
                            msg.tool_calls = [
                                ToolCallSchema(**tc) for tc in json.loads(m.tool_calls_json)
                            ]
                        except Exception:
                            pass
                    messages.append(msg)

                if request.messages:
                    for m in request.messages:
                        if m.role == "user":
                            messages.append(m)
            else:
                if not request.messages:
                    yield f'data: {SSEEvent(event="error", message="必须提供 messages 或 conversation_id。").model_dump_json()}\n\n'
                    return
                messages = list(request.messages)

            # ---- 2. 上下文截断 ----
            _CONTEXT_LIMITS = {
                "ark-code-latest": 28000,
                "gemini-2.0-flash": 100000,
                "gemini-1.5-flash": 100000,
                "gemini-2.5-pro-exp-03-25": 200000,
            }
            _DEFAULT_CTX = 28000
            max_ctx = _CONTEXT_LIMITS.get(request.model or "", _DEFAULT_CTX)
            messages = _truncate_messages(messages, max_tokens=max_ctx)

            messages_before_loop_len = len(messages)
            all_tool_results: list = []

            _max_rounds = int(get_runtime_setting("max_tool_rounds") or _MAX_TOOL_ROUNDS)

            # ---- 3. Agent System Prompt 注入 ----
            tools = tool_registry.get_tool_definitions()
            if tools and not any(m.role == "system" for m in messages):
                messages.insert(
                    0,
                    Message(
                        role="system",
                        content=_AGENT_SYSTEM_PROMPT.format(max_rounds=_max_rounds),
                    ),
                )
                messages_before_loop_len = len(messages)

            # ---- 4. Tool Calling 流式循环 ----
            final_content = ""
            final_usage = {}
            final_finish_reason = "stop"

            for round_num in range(_max_rounds):
                tools = tool_registry.get_tool_definitions()

                # 本轮流式调用
                round_tool_calls_raw = []  # 累积本轮的 tool_calls（原始 dict）
                round_content = ""

                async for sse_event in model_router.generate_stream(
                    messages=messages,
                    model=request.model,
                    temperature=request.temperature,
                    max_tokens=request.max_tokens,
                    tools=tools if tools else None,
                ):
                    if sse_event.event == "__stream_done__":
                        # 内部完成信号，不推送给前端
                        round_content = sse_event.content or ""
                        final_usage = sse_event.usage or {}
                        final_finish_reason = sse_event.finish_reason or "stop"
                        if sse_event.message:
                            try:
                                round_tool_calls_raw = json.loads(sse_event.message)
                            except Exception:
                                round_tool_calls_raw = []
                        continue
                    elif sse_event.event == "error":
                        yield f"data: {sse_event.model_dump_json()}\n\n"
                        return
                    else:
                        # text_delta / tool_start — 直接推送给前端
                        yield f"data: {sse_event.model_dump_json()}\n\n"

                # 无工具调用：本轮结束，准备 done
                if not round_tool_calls_raw:
                    final_content = round_content
                    break

                # 有工具调用：执行工具，yield tool_result，追加消息，继续下一轮
                # 将 assistant 的 tool_calls 消息追加到对话上下文
                from app.models.schemas import ToolCall as ToolCallSchema, ToolCallFunction as ToolCallFunctionSchema
                assistant_tool_calls = [
                    ToolCallSchema(
                        id=tc["id"],
                        type=tc.get("type", "function"),
                        function=ToolCallFunctionSchema(
                            name=tc["function"]["name"],
                            arguments=tc["function"]["arguments"],
                        ),
                    )
                    for tc in round_tool_calls_raw
                ]
                messages.append(
                    Message(
                        role="assistant",
                        content=round_content or None,
                        tool_calls=assistant_tool_calls,
                    )
                )

                for tc in assistant_tool_calls:
                    skill = tool_registry.get_tool_by_name(tc.function.name)
                    if skill:
                        try:
                            params = json.loads(tc.function.arguments)
                        except json.JSONDecodeError:
                            params = {}
                        exec_result = await skill_executor.run(
                            language=skill.language,
                            entrypoint=skill.entrypoint,
                            params=params,
                        )
                    else:
                        exec_result = {
                            "status": "error",
                            "message": f"Tool '{tc.function.name}' not found in skill registry.",
                        }

                    ok = exec_result.get("status") == "success"
                    all_tool_results.append({
                        "tool_call_id": tc.id,
                        "tool_name": tc.function.name,
                        "result": exec_result,
                    })

                    # 推送 tool_result 事件给前端
                    yield f"data: {SSEEvent(event='tool_result', tool_name=tc.function.name, tool_call_id=tc.id, result=exec_result, ok=ok).model_dump_json()}\n\n"

                    # 追加 tool 消息到对话上下文
                    messages.append(
                        Message(
                            role="tool",
                            content=json.dumps(exec_result, ensure_ascii=False),
                            tool_call_id=tc.id,
                        )
                    )

                # 超出轮次上限
                if round_num == _max_rounds - 1:
                    logger.warning(f"chat_stream_endpoint: reached max tool rounds ({_max_rounds}).")
                    final_content = round_content
                    break

            # ---- 5. DB 持久化（done 事件之前） ----
            if conv_id:
                new_messages = list(messages[messages_before_loop_len:])
                if not new_messages or new_messages[-1].role != "assistant" or new_messages[-1].tool_calls:
                    # 追加最终 assistant 消息（纯文本回复）
                    new_messages.append(Message(role="assistant", content=final_content))
                _persist_chat_turn(conv_id=conv_id, new_messages=new_messages)

            # ---- 6. 推送 done 事件 ----
            yield f"data: {SSEEvent(event='done', content=final_content, usage=final_usage, finish_reason=final_finish_reason, conversation_id=conv_id).model_dump_json()}\n\n"

        except Exception as e:
            logger.error(f"chat_stream_endpoint error: {e}")
            yield f"data: {SSEEvent(event='error', message=str(e)).model_dump_json()}\n\n"

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 禁用 Nginx 缓冲，确保立即推送
            "Connection": "keep-alive",
        },
    )

```

> ⚠️ **注意事项**：
> 1. `_truncate_messages`、`get_runtime_setting`、`_MAX_TOOL_ROUNDS`、`_AGENT_SYSTEM_PROMPT`、`_persist_chat_turn` 等函数/常量在同文件中定义，均可直接引用。
> 2. `_truncate_messages` 定义在 `/chat/stream` 端点**之后**（约第 320 行），Python 在运行时会找到它，没有问题（函数体内引用，不是模块级别的直接调用）。
> 3. `get_runtime_setting` 在路由文件中已定义（约第 1299 行），同样可直接引用。

---

## 组 D — 前端 `index.html` 改用 SSE 消费

### 当前状态

- `app/web/index.html` 共 1859 行
- `sendMessage()` 函数在第 1688 行，使用 `await fetch('/api/v1/chat') → await response.json()` 一次性等待
- `appendMessageWithTools()` 在第 1660 行，一次性渲染所有工具调用卡片和文本
- 加载气泡 `loadingDiv` 在发送时创建，响应到来时删除

### 目标

1. 改造 `sendMessage()` 中调用 `/api/v1/chat` 的分支，改为调用 `/api/v1/chat/stream` 并消费 SSE
2. 实现增量 DOM 更新（逐词追加文本）
3. 实现工具调用卡片的动态状态（调用中 → 成功/失败）
4. 保留 `isIntentionMode` 分支不变

### 任务 D-1：修改 `sendMessage()` 的 `/chat` 分支

**定位**：在 `sendMessage()` 函数中，找到 `else` 分支（`// 确保有当前对话` 注释处），即第 1762 行左右：

**当前代码**（第 1762 行 ~ 第 1798 行）：
```javascript
                } else {
                    // 确保有当前对话
                    if (!currentConversationId) {
                        await createNewChat();
                    }
                    const response = await fetch('/api/v1/chat', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            conversation_id: currentConversationId,
                            messages: [{ role: 'user', content: messageContent }],
                            model: modelSelect.value
                        })
                    });
                    const data = await response.json();
                    document.getElementById(loadingId)?.remove();
                    if (response.ok) {
                        const toolResults = data.tool_results || null;
                        appendMessageWithTools('assistant', data.content, toolResults);
                        // 后端可能返回最终 conversation_id（首次创建时）
                        if (data.conversation_id) {
                            currentConversationId = data.conversation_id;
                            localStorage.setItem('wateryLastConversationId', data.conversation_id);
                        }
                        // 异步刷新侧边栏（更新标题、排序）
                        renderHistoryList().then(() => {
                            const activeItem = historyList.querySelector('.history-item.active');
                            if (activeItem) chatTitle.textContent = activeItem.textContent;
                        });
                    } else {
                        appendMessage('assistant', `❌ 错误: ${data.detail || '请求失败'}`);
                    }
                }
```

**替换为**：
```javascript
                } else {
                    // 确保有当前对话
                    if (!currentConversationId) {
                        await createNewChat();
                    }

                    // Phase 12a — 改用 SSE 流式端点
                    const response = await fetch('/api/v1/chat/stream', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            conversation_id: currentConversationId,
                            messages: [{ role: 'user', content: messageContent }],
                            model: modelSelect.value
                        })
                    });

                    if (!response.ok) {
                        document.getElementById(loadingId)?.remove();
                        const errData = await response.json().catch(() => ({}));
                        appendMessage('assistant', `❌ 错误: ${errData.detail || '请求失败'}`);
                        return;
                    }

                    // 移除 loading 气泡，创建真正的 AI 回复气泡
                    document.getElementById(loadingId)?.remove();
                    const aiMsgDiv = document.createElement('div');
                    aiMsgDiv.className = 'message assistant';
                    chatMessages.appendChild(aiMsgDiv);
                    chatMessages.scrollTop = chatMessages.scrollHeight;

                    // 用于追踪工具调用卡片（tool_call_id → DOM 元素）
                    const toolCardMap = {};
                    // 文本容器（工具卡片之后的文本节点）
                    let textSpan = null;

                    // 消费 SSE 流
                    const reader = response.body.getReader();
                    const decoder = new TextDecoder('utf-8');
                    let buffer = '';

                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;

                        buffer += decoder.decode(value, { stream: true });
                        const lines = buffer.split('\n');
                        buffer = lines.pop(); // 保留未完整的行

                        for (const line of lines) {
                            if (!line.startsWith('data: ')) continue;
                            let evt;
                            try {
                                evt = JSON.parse(line.slice(6));
                            } catch {
                                continue;
                            }

                            if (evt.event === 'text_delta') {
                                // 确保文本容器存在
                                if (!textSpan) {
                                    textSpan = document.createElement('span');
                                    textSpan.className = 'stream-text';
                                    aiMsgDiv.appendChild(textSpan);
                                }
                                textSpan.textContent += evt.delta || '';
                                chatMessages.scrollTop = chatMessages.scrollHeight;

                            } else if (evt.event === 'tool_start') {
                                // 创建工具调用卡片（状态：执行中）
                                const card = document.createElement('div');
                                card.className = 'tool-call-card pending';
                                card.innerHTML = `
                                    <div class="tool-call-header">⏳ 工具调用: ${evt.tool_name || '?'}</div>
                                    <div class="tool-call-result">执行中...</div>
                                `;
                                // 工具卡片插入到文本之前
                                if (textSpan) {
                                    aiMsgDiv.insertBefore(card, textSpan);
                                } else {
                                    aiMsgDiv.appendChild(card);
                                }
                                // 每次新工具后，重置 textSpan（工具结果之后的文本是新段落）
                                textSpan = null;
                                toolCardMap[evt.tool_call_id] = card;
                                chatMessages.scrollTop = chatMessages.scrollHeight;

                            } else if (evt.event === 'tool_result') {
                                // 更新对应工具卡片
                                const card = toolCardMap[evt.tool_call_id];
                                if (card) {
                                    const ok = evt.ok;
                                    card.className = `tool-call-card ${ok ? '' : 'error'}`;
                                    const resultText = JSON.stringify(
                                        evt.result?.result ?? evt.result?.message ?? evt.result ?? ''
                                    ).slice(0, 150);
                                    card.innerHTML = `
                                        <div class="tool-call-header">${ok ? '🔧' : '❌'} 工具调用: ${evt.tool_name || '?'}</div>
                                        <div class="tool-call-result">→ ${resultText}</div>
                                    `;
                                }

                            } else if (evt.event === 'done') {
                                // 对话完成
                                if (evt.conversation_id) {
                                    currentConversationId = evt.conversation_id;
                                    localStorage.setItem('wateryLastConversationId', evt.conversation_id);
                                }
                                // 如果没有流式文本输出（全是工具调用），补充最终 content
                                if (!textSpan && evt.content) {
                                    const finalSpan = document.createElement('span');
                                    finalSpan.className = 'stream-text';
                                    finalSpan.textContent = evt.content;
                                    aiMsgDiv.appendChild(finalSpan);
                                }
                                // 刷新侧边栏
                                renderHistoryList().then(() => {
                                    const activeItem = historyList.querySelector('.history-item.active');
                                    if (activeItem) chatTitle.textContent = activeItem.textContent;
                                });

                            } else if (evt.event === 'error') {
                                appendMessage('assistant', `❌ 错误: ${evt.message || '未知错误'}`);
                                aiMsgDiv.remove();
                            }
                        }
                    }
                }
```

### 任务 D-2：添加工具卡片 `pending` 状态的 CSS 样式

在 `index.html` 的 `<style>` 块中，找到 `.tool-call-card.error` 的样式，在其之后追加：

```css
        .tool-call-card.pending {
            border-color: #ffc107;
            background-color: #fff9c4;
        }

        .stream-text {
            white-space: pre-wrap;
            word-break: break-word;
        }
```

> **定位锚点**：搜索 `.tool-call-card` 样式块。如果当前没有 `.tool-call-card.error` 样式，在 `.tool-call-card` 样式之后追加即可。

---

## 组 E — OpenAI 兼容端点 `POST /v1/chat/completions`

> **Phase 12b 内容**，在 Phase 12a（A~D）全部完成并测试通过后再实现。

### 背景

标准 OpenAI API 格式：
- **请求路径**：`POST /v1/chat/completions`
- **请求体**：`{"model":"...", "messages":[...], "stream":true, "temperature":...}`
- **流式响应**：`data: {"id":"...","choices":[{"delta":{"content":"..."}}]}\n\n`，最后一条为 `data: [DONE]\n\n`

### 目标

新增一个 `main.py` 级别的路由，实现标准 OpenAI 兼容接口，内部复用 `generate_stream()`。

### 任务 E-1：在 `app/main.py` 中新增兼容路由

先查看 `app/main.py` 结构：
```
app/
  main.py       ← FastAPI 应用入口，挂载了 router（来自 api/routes.py）
```

在 `app/main.py` 末尾（或在 router include 之后）添加一个新的路由端点：

```python
import time
from fastapi.responses import StreamingResponse as _StreamingResponse

@app.post("/v1/chat/completions", include_in_schema=True, summary="OpenAI 兼容端点（支持 stream=True）")
async def openai_compatible_chat(body: dict = Body(...)):
    """
    标准 OpenAI ChatCompletion 兼容接口。

    接受标准 OpenAI 格式的请求（model, messages, stream, temperature, max_tokens）。
    当 stream=True 时，返回标准 OpenAI SSE 格式（data: {...} / data: [DONE]）。
    当 stream=False 时，返回标准 OpenAI 非流式响应格式。

    **注意**：此端点不支持 conversation_id（会话持久化），适用于外部工具集成。
    若需要会话管理，请使用 POST /api/v1/chat 或 POST /api/v1/chat/stream。
    """
    from app.services.model_router import model_router
    from app.models.schemas import Message, SSEEvent

    raw_messages = body.get("messages", [])
    model = body.get("model", "ark-code-latest")
    stream = body.get("stream", False)
    temperature = float(body.get("temperature", 0.7))
    max_tokens = int(body.get("max_tokens", 2048))

    messages = [
        Message(role=m["role"], content=m.get("content"))
        for m in raw_messages
    ]

    req_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    if stream:
        async def _openai_stream():
            full_content = ""
            async for evt in model_router.generate_stream(
                messages=messages,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
            ):
                if evt.event == "text_delta":
                    full_content += evt.delta or ""
                    chunk = {
                        "id": req_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": evt.delta or ""}, "finish_reason": None}],
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                elif evt.event == "__stream_done__":
                    chunk = {
                        "id": req_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": evt.finish_reason or "stop"}],
                        "usage": evt.usage or {},
                    }
                    yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                elif evt.event == "error":
                    err = {"error": {"message": evt.message, "type": "server_error"}}
                    yield f"data: {json.dumps(err)}\n\n"
                    return
                # tool_start / tool_result 在 OpenAI 兼容模式下透传 tool_calls delta（此处简化，仅透传文本）
            yield "data: [DONE]\n\n"

        return _StreamingResponse(
            _openai_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    else:
        # 非流式：调用非流式 generate()
        resp = await model_router.generate(messages=messages, model=model, temperature=temperature, max_tokens=max_tokens)
        return {
            "id": req_id,
            "object": "chat.completion",
            "created": created,
            "model": resp.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": resp.content},
                "finish_reason": resp.finish_reason or "stop",
            }],
            "usage": resp.usage or {},
        }
```

> ⚠️ **注意**：`app/main.py` 中已有 `import uuid`（若无则补充）。`json` 和 `time` 需要在 main.py 中 import。

---

## 测试验证步骤

### 测试 Phase 12a（组 A~D）

**重启服务**：
```bash
docker-compose up --build
```

**测试 1：基础流式输出**  
在浏览器打开 `http://localhost:18000`，发送一条普通消息，观察：
- ✅ 文字是否逐词出现（而非一次性弹出）
- ✅ "思考中..." 气泡是否被替换为逐渐填充的文字气泡

**测试 2：Tool Calling 流式**  
发送一条需要调用工具的消息（如"搜索一下 xxx"），观察：
- ✅ 是否出现 `⏳ 工具调用: tool_name` 卡片（黄色背景）
- ✅ 工具执行完成后卡片是否变为 `🔧` 绿色或 `❌` 红色
- ✅ 工具结果之后 LLM 继续的文字是否流式输出

**测试 3：curl 验证 SSE 格式**：
```bash
curl -N -X POST http://localhost:18000/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"你好"}],"model":"ark-code-latest"}'
```
应看到逐行输出 `data: {"event":"text_delta","delta":"..."}`，最后一行为 `data: {"event":"done",...}`

### 测试 Phase 12b（组 E）

**测试 4：OpenAI 兼容端点**：
```bash
curl -N -X POST http://localhost:18000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"ark-code-latest","messages":[{"role":"user","content":"hello"}],"stream":true}'
```
应看到标准 OpenAI SSE 格式，最后一行为 `data: [DONE]`

**测试 5：Open WebUI 接入**（可选）  
在 Open WebUI 中配置：
- OpenAI Base URL: `http://your-server-ip:18000/v1`
- API Key: 任意字符串（后端不校验，可加鉴权层）

---

## 文件修改清单

| 文件 | 修改类型 | 任务 |
|------|----------|------|
| `app/models/schemas.py` | 追加 | A-1：SSEEvent 类 |
| `app/services/model_router.py` | 修改 import + 新增方法 | B-1、B-2：generate_stream 等 |
| `app/api/routes.py` | 修改 import + 新增端点 | C-1、C-2：chat/stream 端点 |
| `app/web/index.html` | 修改 JS + 追加 CSS | D-1、D-2：SSE 消费逻辑 |
| `app/main.py` | 新增端点 | E-1：OpenAI 兼容端点 |

**严禁改动的文件**（避免破坏内部服务）：
- `app/services/worker.py`
- `app/services/pdf_processor.py`
- `app/services/orchestrator.py`
- `app/services/manager.py`
- 所有 `app/models/database.py` 的现有字段

---

## 已知风险与注意事项

1. **`stream_options` 兼容性**：火山引擎 API 是否支持 `stream_options: {include_usage: true}` 需要测试；若不支持，将 `create_kwargs["stream_options"]` 这行注释掉，usage 返回空 dict 即可，不影响功能。

2. **Gemini 流式 tool_calls**：Gemini 的流式 tool_calls 格式与 OpenAI 略有不同（`index` 字段可能缺失），如遇错误，在 `_call_gemini_stream` 中将 `idx = tc_chunk.index` 改为 `idx = getattr(tc_chunk, 'index', 0)` 并用列表长度兜底。

3. **前端 `model_dump_json()`**：Pydantic v2 中 `SSEEvent.model_dump_json()` 会包含所有 `None` 字段。若要精简输出，可改为 `SSEEvent.model_dump_json(exclude_none=True)`。

4. **Nginx 缓冲**：如果服务部署在 Nginx 反向代理后，必须确保 `X-Accel-Buffering: no` 响应头生效，否则 Nginx 会缓冲 SSE 输出。`docker-compose.yml` 中的 Nginx 配置需加 `proxy_buffering off`。
