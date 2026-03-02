# Phase 9 — PDF 大文件处理增强 + 多模态图片理解 详细任务分解

> **[ARCHITECT] 模式产出**  
> 日期：2026-03-02  
> 目标：使 PDF-to-Skills 流水线能够可靠处理 5MB-200MB 的大型教材 PDF（含图片），引入多模态 LLM 图片理解、并发处理、进度上报等增强能力。
>
> **完成状态：组 A 🔲 | 组 B 🔲 | 组 C 🔲 | 组 D 🔲**

---

## 核心需求回顾

**当前状态**：Phase 4 实现了 PDF→Skills 完整流水线（提取 → 分块 → LLM 摘要 → SKILL.md 生成 → 注册），但存在以下限制：

1. 上传接口 `file.read()` 一次性全量读入内存，无文件大小限制 → 200MB PDF 直接 OOM
2. LLM 摘要逐 Chunk **串行调用** → 100+ Chunk 的教材需 10-17 分钟，无并发
3. Chunk 注入 Prompt 时 `chunk_text[:4000]` 硬截断 → 丢失约 50% 已分块内容
4. 无超时保护 → 单次 LLM hang 则整条流水线永不结束
5. 教材 PDF 中的**图表/流程图/示意图**中的知识完全丢失（无图片处理能力）
6. 标题检测正则不支持英文教材格式（`Chapter X` / `Part I` / `1.1.1`）
7. 无 Chunk 级进度上报 → 前端轮询只能看到 pending/completed 两态
8. 只有细粒度 Chunk 技能，缺少教材级全局概述技能

**翻译为工程需求**：
1. 流式上传 + 文件大小上限（250MB）
2. `asyncio.Semaphore` 并发池（5-10 并发）
3. 修复 Chunk 截断逻辑
4. 全局 + per-chunk 超时保护
5. PDF 图片提取 → `gemini-2.5-flash` Vision → 文字描述 → 合入 Chunk
6. 扩展标题检测正则覆盖
7. PDFDocument 新增 `processed_chunks` 字段，实时上报进度
8. 流水线末尾追加"教材概述"总技能生成

---

## 功能分组 & 优先级

| 组 | 名称 | 优先级 | 阻塞关系 |
|---|------|--------|---------|
| **A** | 上传层加固 + DB 迁移 | P0 | 无阻塞，可独立开发 |
| **B** | 流水线核心增强（并发/截断/超时） | P0 | 无阻塞，可独立开发 |
| **C** | 多模态图片理解 | P0 | 依赖 B（合入 Chunk 处理流程） |
| **D** | 质量增强（标题正则/进度/概述技能） | P1 | 依赖 B（流水线改造完成后） |

**建议实施顺序**：A → B → C → D

---

## 组 A — 上传层加固 + DB 迁移

### 当前状态

- `POST /pdf/upload` 使用 `content = await file.read()` 全量读入内存
- 无文件大小校验，恶意或超大文件可导致 OOM
- `PDFDocument` 表无 `processed_chunks` 进度字段

### 目标

流式分块写盘、250MB 上限校验、DB 预留进度字段。

---

### 任务 A-1：流式上传 + 文件大小限制

**改动文件**：`app/api/routes.py`

**改动点**：

1. 新增常量：
```python
_MAX_UPLOAD_SIZE = 250 * 1024 * 1024   # 250MB
_UPLOAD_CHUNK_SIZE = 1024 * 1024        # 1MB 分块写盘
```

2. 重写 `upload_pdf()` 中的文件写入逻辑：
```python
# 替换原有的：
#   content = await file.read()
#   with open(tmp_path, "wb") as f:
#       f.write(content)

# 改为流式分块写盘：
total_size = 0
with open(tmp_path, "wb") as f:
    while True:
        chunk = await file.read(_UPLOAD_CHUNK_SIZE)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > _MAX_UPLOAD_SIZE:
            # 超限：立即删除临时文件，返回 413
            f.close()
            os.remove(tmp_path)
            raise HTTPException(
                status_code=413,
                detail=f"文件大小超过 {_MAX_UPLOAD_SIZE // (1024*1024)}MB 上限。"
            )
        f.write(chunk)
```

**设计决策**：
- 内存占用固定 ~1MB（不随文件大小线性增长）
- 超限时**立即中断**，不等读完整个文件
- HTTP 413 `Payload Too Large` 是语义正确的状态码

**验证**：
- 上传 5MB PDF → 成功
- 上传 300MB 文件 → 返回 413 + 错误信息
- 上传期间容器内存不超过基线 + 5MB

---

### 任务 A-2：PDFDocument 新增 processed_chunks 字段

**改动文件**：`app/models/database.py`、`app/core/db.py`

**改动点**：

1. `database.py` — `PDFDocument` 新增字段：
```python
class PDFDocument(SQLModel, table=True):
    # ... 已有字段 ...
    processed_chunks: int = Field(default=0, description="已处理的 Chunk 数量（实时进度）")
```

2. `db.py` — `_migrate_schema()` 新增增量迁移：
```python
migrations = [
    # ... 已有迁移条目 ...
    # Phase 9 — PDF 大文件处理
    ("pdfdocument", "processed_chunks", "INTEGER DEFAULT 0"),
]
```

**验证**：
- `docker-compose up --build` 后 `PRAGMA table_info(pdfdocument)` 包含 `processed_chunks` 列
- 已有 PDFDocument 行的 `processed_chunks` 自动为 0（DEFAULT 生效）

---

### 组 A 验证清单

- [ ] 5MB PDF 上传成功，写盘完成
- [ ] 300MB 文件上传返回 HTTP 413
- [ ] 上传期间容器 RSS 内存不超过基线 + 10MB
- [ ] `pdfdocument` 表包含 `processed_chunks` 列

---

## 组 B — 流水线核心增强（并发 / 截断 / 超时）

### 当前状态

- `pdf_to_skills()` 内 `for chunk in chunks: await summarize_chunk(...)` 串行执行
- `summarize_chunk()` 中 `chunk.text[:4000]` 硬截断字符数
- 无任何超时保护机制

### 目标

并发 LLM 调用（5 并发）、修复截断逻辑、全局 + per-chunk 超时保护。

---

### 任务 B-1：LLM 并发池（asyncio.Semaphore）

**改动文件**：`app/services/pdf_processor.py` — `pdf_to_skills()` 方法

**改动点**：

将串行 for 循环改为并发执行：

```python
# 在 PDFProcessor 类中新增并发控制常量
_LLM_CONCURRENCY = 5          # 最大并发 LLM 调用数
_PER_CHUNK_TIMEOUT = 60       # 单 Chunk 超时（秒）

async def pdf_to_skills(self, ...):
    # ... Step 1 + Step 2 不变 ...

    # ---- Step 3 + 4: 并发摘要 + 生成 ----
    semaphore = asyncio.Semaphore(self._LLM_CONCURRENCY)

    async def _process_one_chunk(i: int, chunk: TextChunk):
        """处理单个 Chunk：摘要 → 生成 SKILL.md（带信号量限流）。"""
        async with semaphore:
            try:
                draft = await asyncio.wait_for(
                    self.summarize_chunk(chunk, doc_title=doc_title),
                    timeout=self._PER_CHUNK_TIMEOUT,
                )
            except asyncio.TimeoutError:
                return i, None, f"Chunk {chunk.chunk_id}: LLM timeout ({self._PER_CHUNK_TIMEOUT}s)"

            if draft is None:
                return i, None, None  # 跳过（质量不达标）

            # 生成技能 ID + SKILL.md（同步操作，速度快）
            raw_name = re.sub(r"[^a-z0-9-]", "-", draft.skill_name.lower())
            raw_name = re.sub(r"-+", "-", raw_name).strip("-") or f"skill-{uuid.uuid4().hex[:6]}"
            skill_id = f"{skill_prefix}{raw_name}" if skill_prefix else raw_name
            # 并发安全的去重后缀
            base_id = skill_id
            suffix = 1
            while os.path.exists(os.path.join(output_dir, skill_id)):
                skill_id = f"{base_id}-{suffix}"
                suffix += 1

            pages_str = (
                f"{min(chunk.source_pages)}-{max(chunk.source_pages)}"
                if chunk.source_pages else ""
            )
            skill_md_path = self.generate_skill_md(
                draft, skill_id, output_dir,
                source_pdf_id=doc_id or "",
                source_pages=pages_str,
            )
            return i, (skill_id, os.path.dirname(skill_md_path), pages_str), None

    # 启动所有 Chunk 的并发任务
    tasks = [_process_one_chunk(i, chunk) for i, chunk in enumerate(chunks)]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 收集结果
    for result in results:
        if isinstance(result, Exception):
            errors.append(f"Unexpected: {result}")
            continue
        idx, skill_info, err_msg = result
        if err_msg:
            errors.append(err_msg)
        if skill_info:
            skill_id, skill_dir, pages_str = skill_info
            skills_dir_generated.append((skill_id, skill_dir, pages_str))
            skills_generated.append(skill_id)

        # 更新进度
        if doc_id:
            await self._update_pdf_doc_progress(doc_id, processed=idx + 1)
```

**设计决策**：
- `Semaphore(5)` — 5 并发平衡速度与 API 限流风险（火山引擎 QPS ≈ 10，Gemini ≈ 30）
- 每个 Chunk 独立超时 60s — 不因单个慢 Chunk 拖垮全局
- `asyncio.gather(*tasks, return_exceptions=True)` — 异常不中断其他任务
- 技能 ID 去重用 `os.path.exists` 检查（并发写入同目录时需注意，但概率极低且无副作用）

**性能预期**：
- 100 Chunks 串行：~500s（8分钟）
- 100 Chunks 5 并发：~100s（1.7分钟）
- 提速约 **5 倍**

---

### 任务 B-2：修复 Chunk 注入截断逻辑

**改动文件**：`app/services/pdf_processor.py` — `summarize_chunk()` 方法

**改动点**：

```python
# 当前（有问题）：
chunk_text=chunk.text[:4000],  # 硬截断 4000 字符 → 丢失约 50% 内容

# 改为 token 级截断，对齐分块层的 max_tokens 参数：
def _truncate_to_tokens(self, text: str, max_tokens: int = 5000) -> str:
    """按 token 估算截断文本，保留最多 max_tokens 个 token 的内容。"""
    current_tokens = estimate_tokens(text)
    if current_tokens <= max_tokens:
        return text
    # 按比例截断字符数
    ratio = max_tokens / current_tokens
    return text[:int(len(text) * ratio)]
```

并在 `summarize_chunk()` 中使用：
```python
# 原先：chunk_text=chunk.text[:4000]
# 改为：
chunk_text=self._truncate_to_tokens(chunk.text, max_tokens=5000)
```

**设计决策**：
- 分块层已保证单 Chunk ≤ 6000 tokens，这里截断到 5000 tokens 为 Prompt 模板和系统消息预留 ~1000 tokens
- 使用已有的 `estimate_tokens()` 函数保持一致性
- 按比例截断而非硬截断字符数，对中英文混合内容更公平

**验证**：
- 一个 6000 token 的 Chunk → 注入 Prompt 后总 token < 8000（在模型上下文窗口内）
- 一个 2000 token 的 Chunk → 不被截断，全文保留

---

### 任务 B-3：全局超时保护

**改动文件**：`app/services/pdf_processor.py` — `pdf_to_skills()` 方法

**改动点**：

1. 新增类常量：
```python
_PIPELINE_TIMEOUT = 3600  # 整条流水线最大超时（秒），1 小时
```

2. 在 `pdf_to_skills()` 入口处包裹全局超时：
```python
async def pdf_to_skills(self, ...):
    try:
        return await asyncio.wait_for(
            self._pdf_to_skills_inner(...),  # 将原方法体抽到 inner
            timeout=self._PIPELINE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.error(f"[Pipeline] Global timeout ({self._PIPELINE_TIMEOUT}s) exceeded.")
        if doc_id:
            await self._update_pdf_doc_status(
                doc_id, "failed",
                error_msg=f"Pipeline timeout after {self._PIPELINE_TIMEOUT}s",
            )
        return PipelineResult(
            pdf_path=pdf_path,
            total_pages=0,
            total_chunks=0,
            errors=[f"Pipeline timeout ({self._PIPELINE_TIMEOUT}s)"],
        )
```

**设计决策**：
- 1 小时全局超时足以覆盖 200MB 教材（含图片处理）的最坏情况
- 超时后自动标记 PDFDocument 状态为 `failed`，前端轮询可感知
- per-chunk 超时在 B-1 中已覆盖（60s），全局超时是兜底

---

### 任务 B-4：进度上报辅助方法

**改动文件**：`app/services/pdf_processor.py`

**新增方法**：

```python
async def _update_pdf_doc_progress(self, doc_id: str, processed: int):
    """
    增量更新 PDFDocument 的已处理 Chunk 数量。
    高频调用（每处理完一个 Chunk 更新一次），用轻量 UPDATE 实现。
    """
    from app.models.database import PDFDocument
    from sqlmodel import Session, text
    from app.core.db import engine

    with Session(engine) as session:
        session.exec(
            text("UPDATE pdfdocument SET processed_chunks = :n WHERE id = :id"),
            params={"n": processed, "id": doc_id},
        )
        session.commit()
```

**补充**：同步更新 `GET /pdf/status/{doc_id}` 端点，在响应中返回 `processed_chunks` 字段：

```python
# routes.py — GET /pdf/status/{doc_id} 的响应增加：
{
    "doc_id": "...",
    "status": "processing",
    "total_chunks": 85,
    "processed_chunks": 32,     # ← 新增
    "progress_pct": 37.6,       # ← 新增（processed / total * 100）
    "skills_generated": [...]
}
```

**验证**：
- 流水线处理中轮询 status API → `processed_chunks` 单调递增
- `progress_pct` 从 0 到 100

---

### 组 B 验证清单

- [ ] 100 Chunk 的 PDF 并发处理耗时 < 2 分钟（5 并发）
- [ ] 单 Chunk LLM 超时 60s 后跳过，不影响其他 Chunk
- [ ] 6000 token 的 Chunk 内容注入 Prompt 后不被过度截断
- [ ] 全局超时 1h 触发后，PDFDocument 状态变为 `failed`
- [ ] `GET /pdf/status/{doc_id}` 返回 `processed_chunks` 和 `progress_pct`

---

## 组 C — 多模态图片理解

### 当前状态

- `pypdf` 和 `pdfplumber` 只提取文字和表格
- PDF 中嵌入的图表、流程图、示意图中的知识完全丢失
- specs.md §9 规定「不引入 OCR/Tesseract」
- 项目已集成 Gemini 客户端（`model_router.py`），走 Clash 代理可用

### 目标

从 PDF 逐页提取图片 → 发送至 `gemini-2.5-flash` Vision → 获取文字描述 → 合入对应页的文本内容。**零系统依赖，复用现有 LLM 通道**。

---

### 任务 C-1：PDF 图片提取模块

**改动文件**：`app/services/pdf_processor.py`

**新增方法**：

```python
import base64
from typing import Tuple

def _extract_images_from_page(
    self,
    pdf_path: str,
    page_number: int,
    min_width: int = 100,
    min_height: int = 100,
    max_images_per_page: int = 5,
) -> List[Tuple[bytes, str, int, int]]:
    """
    从 PDF 指定页提取嵌入图片。

    使用 pypdf 的 page.images API 提取图片二进制数据。
    过滤掉过小的装饰图片（宽高 < 100px）。

    Args:
        pdf_path:           PDF 文件路径。
        page_number:        页码（0-based）。
        min_width:          最小宽度阈值（px），过滤装饰图标。
        min_height:         最小高度阈值（px）。
        max_images_per_page: 单页最大提取图片数（防止图片过多的页面阻塞处理）。

    Returns:
        List of (image_bytes, mime_type, width, height)。
        mime_type 通常为 "image/jpeg" 或 "image/png"。
    """
    import pypdf

    images = []
    try:
        reader = pypdf.PdfReader(pdf_path)
        if page_number >= len(reader.pages):
            return []

        page = reader.pages[page_number]
        for img_obj in page.images[:max_images_per_page]:
            # pypdf >= 4.0 的 page.images 返回 ImageObject
            img_data = img_obj.data
            # 尝试获取尺寸信息
            width = getattr(img_obj, 'width', 0) or 0
            height = getattr(img_obj, 'height', 0) or 0

            # 过滤过小的图片（图标、装饰线等）
            if width > 0 and width < min_width:
                continue
            if height > 0 and height < min_height:
                continue

            # 推断 MIME 类型
            mime = "image/jpeg"  # 默认
            if img_obj.name and img_obj.name.lower().endswith(".png"):
                mime = "image/png"

            images.append((img_data, mime, width, height))

    except Exception as e:
        logger.warning(f"Image extraction failed for page {page_number}: {e}")

    return images
```

**设计决策**：
- 使用 pypdf >= 4.0 内置的 `page.images` API，无需额外依赖
- `min_width/min_height = 100` 过滤掉小图标、分隔线等装饰元素
- `max_images_per_page = 5` 防止极端页面（如图册页）生成过多 LLM 调用
- 返回原始 bytes + MIME type，便于后续 base64 编码发给 LLM

**依赖**：无新依赖，`pypdf >= 4.0` 已在 requirements.txt 中

---

### 任务 C-2：Gemini Vision 图片描述服务

**改动文件**：`app/services/pdf_processor.py`

**新增方法**：

```python
# ---- 图片理解 Prompt 模板 ----
IMAGE_DESCRIBE_PROMPT = """你是一位专业的文档分析师。请分析这张来自教材的图片，并提供详细的文字描述。

## 文档上下文
- 文档标题: {doc_title}
- 所在页码: 第 {page_number} 页
- 该页文字内容摘要: {page_text_snippet}

## 输出要求
1. 如果是**流程图/架构图**：描述每个节点和连接关系，以及整体流程含义
2. 如果是**数据图表**（柱状图/折线图/饼图等）：描述数据趋势、关键数据点、图表标题和轴标签
3. 如果是**示意图/概念图**：描述图中各元素及其关系
4. 如果是**纯装饰图片/照片**：回复"[装饰图片，无实质内容]"

请用中文输出，300字以内。""".strip()


# ---- 图片理解常量 ----
_IMAGE_VISION_MODEL_DEFAULT = "gemini-2.5-flash"  # 默认值，用户可在 Web UI 设置页切换
_IMAGE_CONCURRENCY = 3                      # 图片 LLM 调用并发数（独立于文字 Chunk 并发）
_IMAGE_TIMEOUT = 30                         # 单张图片 LLM 超时（秒）

@property
def _image_vision_model(self) -> str:
    """从运行时设置读取图片识别模型，DB 无记录时回退到默认值。"""
    from app.api.routes import get_runtime_setting
    return get_runtime_setting("vision_model") or self._IMAGE_VISION_MODEL_DEFAULT


async def _describe_image(
    self,
    image_bytes: bytes,
    mime_type: str,
    doc_title: str,
    page_number: int,
    page_text_snippet: str,
) -> Optional[str]:
    """
    调用 Gemini Vision 对单张图片生成文字描述。

    Args:
        image_bytes:       图片二进制数据。
        mime_type:         MIME 类型（image/jpeg 或 image/png）。
        doc_title:         文档标题（注入上下文）。
        page_number:       图片所在页码（1-based）。
        page_text_snippet: 该页文字内容前 200 字（辅助 LLM 理解图片上下文）。

    Returns:
        图片的文字描述；失败或为装饰图片时返回 None。
    """
    from app.services.model_router import model_router

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    prompt = IMAGE_DESCRIBE_PROMPT.format(
        doc_title=doc_title or "未知文档",
        page_number=page_number,
        page_text_snippet=page_text_snippet[:200],
    )

    # Gemini Vision 使用 OpenAI 兼容格式的多模态消息
    messages = [
        Message(role="user", content=[
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{b64}",
                },
            },
        ]),
    ]

    try:
        response = await asyncio.wait_for(
            model_router.generate(
                messages=messages,
                model=self._image_vision_model,
                temperature=0.2,
                max_tokens=500,
            ),
            timeout=self._IMAGE_TIMEOUT,
        )
        desc = response.content.strip()
        if "装饰图片" in desc or "无实质内容" in desc:
            return None
        return desc
    except asyncio.TimeoutError:
        logger.warning(f"Image description timeout for page {page_number}")
        return None
    except Exception as e:
        logger.warning(f"Image description failed for page {page_number}: {e}")
        return None
```

**设计决策**：
- **模型从运行时设置读取**：通过 `GET/PUT /settings` API + Web UI 设置页的 `vision_model` 配置项动态切换，默认 `gemini-2.5-flash`。用户可随时在设置页从支持 Vision 的模型列表中选择替换
- `temperature=0.2`：图片描述需要准确，不需要创造性
- `max_tokens=500`：300 字中文描述 ≈ 200 tokens，500 留足余量
- 超时 30s（图片编码 + 网络传输 + 推理，比纯文本更慢）
- 装饰图片检测：LLM 判断为装饰图片时返回 None，不注入 Chunk

**⚠️ 注意 — Message 模型适配**：

当前 `Message.content` 类型是 `Optional[str]`，但 OpenAI 多模态消息要求 `content` 为 `List[dict]`（含 `text` 和 `image_url` 元素）。需要在 B-2 之前确认 `model_router._format_messages()` 是否能正确处理 list 类型的 content。

**需要的适配（小改动）**：

```python
# model_router.py — _format_messages() 中：
if msg.content is not None:
    d["content"] = msg.content   # 已支持 str 和 list 两种类型（OpenAI SDK 原生兼容）
```

同时 `Message` schema 需调整：
```python
# schemas.py
class Message(BaseModel):
    content: Optional[Any] = Field(None, ...)  # str 或 List[dict]（多模态）
```

---

### 任务 C-3：图片描述合入 extract_text 流程

**改动文件**：`app/services/pdf_processor.py` — `extract_text()` / `_extract_text_sync()`

**改动点**：

在 `extract_text()` 方法中，提取文字后追加一个异步图片处理步骤：

```python
async def extract_text(self, pdf_path: str) -> PDFExtractResult:
    """
    提取 PDF 全文文本、表格数据和图片描述。
    改进：提取后异步调用多模态 LLM 描述图片，合入各页文本。
    """
    import asyncio

    # Step 1: 同步提取文字 + 表格（原有逻辑，不变）
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, partial(self._extract_text_sync, pdf_path)
    )

    # Step 2: 异步提取图片并生成描述（新增）
    doc_title = result.metadata.get("title", "") or os.path.basename(pdf_path)
    image_semaphore = asyncio.Semaphore(self._IMAGE_CONCURRENCY)

    async def _process_page_images(page: PageContent) -> List[str]:
        """提取并描述单页的所有图片，返回描述列表。"""
        images = self._extract_images_from_page(pdf_path, page.page_number - 1)
        if not images:
            return []

        descriptions = []
        for img_data, mime, w, h in images:
            async with image_semaphore:
                desc = await self._describe_image(
                    image_bytes=img_data,
                    mime_type=mime,
                    doc_title=doc_title,
                    page_number=page.page_number,
                    page_text_snippet=page.text[:200],
                )
                if desc:
                    descriptions.append(desc)
        return descriptions

    # 并发处理所有页的图片
    page_image_tasks = [_process_page_images(page) for page in result.pages]
    page_descriptions = await asyncio.gather(*page_image_tasks)

    # Step 3: 将图片描述追加到对应页的文本末尾
    full_text_parts = []
    for page, descs in zip(result.pages, page_descriptions):
        if descs:
            image_section = "\n\n---\n[页面图片内容]\n" + "\n\n".join(
                f"[图 {i+1}] {d}" for i, d in enumerate(descs)
            )
            page.text += image_section
        full_text_parts.append(page.text)

    # 重新拼接全文
    result.text = "\n".join(full_text_parts)
    return result
```

**设计决策**：
- 图片描述以 `[页面图片内容]` 标记块追加到页尾，与正文文字区分
- 后续分块层会自动将图片描述纳入 Chunk，无需修改分块逻辑
- 3 并发图片 LLM 调用（独立于 Step 3/4 的文字 Chunk 并发池）
- 图片处理失败时静默跳过（不阻塞文字提取链路）

**费用分析**：
- 200MB 教材约 300-500 页，有图片的页面约 40-60%
- 每页平均 1-2 张有效图片 → 约 100-300 张图
- 每张图 ~258 input tokens（Gemini 图片标准） + 200 output tokens
- 总 token：~140K（$0.02-0.05 @ Gemini 2.5 Flash 价格）

---

### 任务 C-4：Message Schema 多模态适配

**改动文件**：`app/models/schemas.py`、`app/services/model_router.py`

**改动点**：

1. `schemas.py` — `Message.content` 字段类型放宽：
```python
from typing import Union

class Message(BaseModel):
    role: str = Field(...)
    content: Optional[Union[str, List[Any]]] = Field(
        None,
        description="消息内容。纯文本时为 str；多模态时为 List[dict]（含 text/image_url 元素）",
    )
    # ... 其余字段不变
```

2. `model_router.py` — `_format_messages()` 确认兼容：
```python
# content 字段：str 或 List[dict] 均直接透传给 OpenAI SDK
if msg.content is not None:
    d["content"] = msg.content  # OpenAI SDK 原生支持两种格式
else:
    d["content"] = ""
```

此处改动极小，OpenAI SDK 本身就支持多模态 content 格式，只需确保我们的 `Message` schema 不拒绝 list 类型即可。

**验证**：
- 发送纯文本消息 → 行为不变
- 发送含 `image_url` 的多模态消息到 Gemini → 返回图片描述

---

### 组 C 验证清单

- [ ] 含图片的 PDF 上传后，`extract_text()` 结果中包含 `[页面图片内容]` 标记
- [ ] 流程图/数据图表 → Gemini 返回有意义的文字描述
- [ ] 装饰图片 → 被过滤，不出现在文本中
- [ ] 图片提取/描述失败 → 不影响文字提取链路
- [ ] 纯文字 PDF（无图片）→ 行为完全不变

---

## 组 D — 质量增强（标题正则 / 概述技能）

### 当前状态

- `_extract_heading_level()` 不支持英文教材常见标题
- 只有 Chunk 级技能，缺少全局概述

### 目标

扩展标题检测正则，新增教材概述技能生成。

---

### 任务 D-1：扩展标题检测正则

**改动文件**：`app/services/pdf_processor.py` — `_extract_heading_level()` 函数

**当前支持**：
```
# / ## / ###          ← Markdown 标题
第X章 / 第X节         ← 中文标题
1. Title              ← 一级数字标题
1.1 Title             ← 二级数字标题（实为 Level 3）
```

**新增支持**：
```python
def _extract_heading_level(line: str) -> Tuple[int, str]:
    line = line.strip()

    # ---- Markdown 风格 ----
    if line.startswith("###"):
        return 3, line.lstrip("#").strip()
    if line.startswith("##"):
        return 2, line.lstrip("#").strip()
    if line.startswith("#"):
        return 1, line.lstrip("#").strip()

    # ---- 中文标题 ----
    if re.match(r"^第[一二三四五六七八九十百千\d]+章", line):
        return 1, line
    if re.match(r"^第[一二三四五六七八九十百千\d]+[节部分篇]", line):
        return 2, line

    # ---- 英文教材标题（新增）----
    # "Chapter 1: ..." / "CHAPTER 1 ..."
    if re.match(r"^(?:CHAPTER|Chapter)\s+\d+", line):
        return 1, line
    # "Part I" / "Part 1" / "PART ONE"
    if re.match(r"^(?:PART|Part)\s+(?:[IVX]+|\d+|[A-Z]+)", line):
        return 1, line
    # "Section 1.2" / "SECTION 3"
    if re.match(r"^(?:SECTION|Section)\s+[\d.]+", line):
        return 2, line
    # "Appendix A" / "附录"
    if re.match(r"^(?:APPENDIX|Appendix|附录)\s*[A-Z\d]*", line):
        return 1, line

    # ---- 数字编号标题 ----
    # "1.1.1 Title"（三级）
    if re.match(r"^\d+\.\d+\.\d+\s+\S", line):
        return 3, line
    # "1.1 Title"（二级）
    if re.match(r"^\d+\.\d+\s+\S", line):
        return 3, line
    # "1. Title" / "1 Title"（一级数字）
    if re.match(r"^\d+\.?\s+[A-Z\u4e00-\u9fff]", line):
        return 2, line

    return 0, ""
```

**验证**：
- `"Chapter 3: Data Structures"` → level=1
- `"Part II"` → level=1
- `"1.1.1 Binary Search"` → level=3
- `"普通段落文本"` → level=0

---

### 任务 D-2：教材概述技能生成

**改动文件**：`app/services/pdf_processor.py` — `pdf_to_skills()` 末尾

**新增 Prompt**：

```python
OVERVIEW_SKILL_PROMPT = """你是一位知识工程师。下面是一本教材的各章节摘要片段。
请基于这些片段，生成一个"教材概述"技能定义。

## 教材标题
{doc_title}

## 各章节摘要
{chapter_summaries}

## 输出要求（纯 JSON，无 markdown 代码块）
{{
    "skill_name": "{skill_prefix}overview",
    "display_name": "{doc_title} - 总览",
    "description": "对整本教材的核心知识体系、章节结构和主要内容的总结概述（150-300字）",
    "trigger_conditions": [
        "当用户询问这本教材的整体内容时",
        "当需要了解教材的知识结构和章节关系时",
        "当需要为某个子技能提供上下文背景时"
    ],
    "execution_logic": "基于教材全局视角的分步概述（Markdown 格式）",
    "input_parameters": {{}},
    "output_format": "Markdown 格式的教材概述",
    "tags": ["overview", "教材概述"],
    "quality_score": 5,
    "skip_reason": null
}}""".strip()
```

**实现逻辑**：

在 `pdf_to_skills()` 的 Step 5（注册）之前，插入 Step 4.5：

```python
# ---- Step 4.5: 生成教材概述技能 ----
if skills_generated and len(skills_generated) >= 3:
    logger.info("[Pipeline] Step 4.5: Generating overview skill...")
    try:
        # 收集各 Chunk 的标题路径和简短描述，拼接为概述输入
        chapter_summaries = []
        for i, chunk in enumerate(chunks[:30]):  # 取前 30 个 Chunk 的概要
            heading = " > ".join(chunk.heading_path) if chunk.heading_path else f"Chunk {i+1}"
            snippet = chunk.text[:200]
            chapter_summaries.append(f"### {heading}\n{snippet}...")

        overview_prompt = OVERVIEW_SKILL_PROMPT.format(
            doc_title=doc_title,
            skill_prefix=skill_prefix,
            chapter_summaries="\n\n".join(chapter_summaries),
        )

        overview_messages = [
            Message(role="system", content="你是专业的知识工程师。请只输出 JSON。"),
            Message(role="user", content=overview_prompt),
        ]

        overview_response = await model_router.generate(
            messages=overview_messages,
            temperature=0.3,
            max_tokens=2000,
        )
        raw = overview_response.content.strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        overview_draft = SkillDraft(**json.loads(raw))

        overview_id = f"{skill_prefix}overview" if skill_prefix else "overview"
        self.generate_skill_md(
            overview_draft, overview_id, output_dir,
            source_pdf_id=doc_id or "",
            source_pages=f"1-{extract_result.page_count}",
        )
        skills_dir_generated.insert(0, (
            overview_id,
            os.path.join(output_dir, overview_id),
            f"1-{extract_result.page_count}",
        ))
        skills_generated.insert(0, overview_id)
        logger.info(f"[Pipeline] Overview skill '{overview_id}' generated.")

    except Exception as e:
        logger.warning(f"[Pipeline] Overview skill generation failed: {e}")
        errors.append(f"Overview generation: {e}")
```

**设计决策**：
- 只在生成了 ≥ 3 个子技能时才生成概述（太少说明 PDF 内容不足）
- 取前 30 个 Chunk 的标题 + 前 200 字作为 LLM 输入（控制 token 量 < 8000）
- 概述技能 ID 固定为 `{prefix}overview`，插入到技能列表首位
- 失败时仅 warning 不 abort（概述是锦上添花，不应阻塞主流程）

**验证**：
- 50+ Chunk 的教材 → 生成 `{prefix}overview` 技能
- 概述内容覆盖教材主要章节结构
- 不足 3 个 Chunk → 不生成概述

---

### 组 D 验证清单

- [ ] 英文教材 PDF → `Chapter` / `Part` / `Section` 被正确识别为标题
- [ ] `1.1.1` 三级编号被识别为 level=3
- [ ] 大型教材生成 `overview` 总技能
- [ ] 概述技能描述覆盖教材核心章节结构

---

## 改动文件汇总

| 文件 | 改动任务 | 改动量 |
|---|---|---|
| `app/api/routes.py` | A-1 流式上传, B-4 进度端点 | 中 |
| `app/services/pdf_processor.py` | B-1 并发, B-2 截断, B-3 超时, B-4 进度, C-1 图片提取, C-2 Vision, C-3 合入, D-1 正则, D-2 概述 | **大（核心改动）** |
| `app/models/database.py` | A-2 新增字段 | 小 |
| `app/core/db.py` | A-2 迁移条目 | 小 |
| `app/models/schemas.py` | C-4 Message 多模态适配 | 小 |
| `app/services/model_router.py` | C-4 format_messages 确认 | 极小 |

---

## 实施顺序建议

```
Day 1: A-1 → A-2（上传层 + DB 迁移）
Day 1: B-2 → B-1 → B-3 → B-4（截断 → 并发 → 超时 → 进度）
Day 2: C-4 → C-1 → C-2 → C-3（Schema 适配 → 图片提取 → Vision → 合入）
Day 2: D-1 → D-2（正则扩展 → 概述技能）
Day 2: 端到端验证（上传一本 5MB+ 教材，全流水线跑通）
```