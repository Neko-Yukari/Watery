"""
PDF 处理核心服务 (Phase 4)
===========================
职责：PDF → 语义分块 → AI 摘要 → SKILL.md 生成 → 自动注册

遵循遵循 specs.md §9 的架构设计：
  - 不引入 OCR/Tesseract（主流文字型 PDF 已足够）
  - 纯 Python 依赖（pypdf + pdfplumber），无系统级工具
  - LLM 输出 SkillDraft 用 JSON，不用 YAML（格式可靠性高）
  - 三级递降分块：标题层级 → 段落 → Token 窗口滑动
"""

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

import yaml

from app.models.schemas import (
    PageContent,
    PDFExtractResult,
    PipelineResult,
    SkillDraft,
    TextChunk,
)

logger = logging.getLogger(__name__)

# ---- Prompt 模板（§9.13）----
CHUNK_TO_SKILL_PROMPT = """你是一个专业的知识工程师。你的任务是将下面这段文档内容转化为一个可被 AI Agent 调用的"技能"定义。

## 文档上下文
- 文档标题: {doc_title}
- 章节路径: {heading_path}
- 页码范围: {page_range}

## 文档片段
{chunk_text}

## 输出要求
请以纯 JSON 格式输出（不要 markdown 代码块），包含以下字段：
{{
    "skill_name": "简洁的技能名称（英文 kebab-case，如 cash-flow-analysis）",
    "display_name": "中文显示名称",
    "description": "一句话描述这个技能的用途（50-100字）",
    "trigger_conditions": [
        "当用户问到 XXX 时",
        "当需要执行 YYY 操作时"
    ],
    "execution_logic": "详细的分步执行逻辑（Markdown 格式）",
    "input_parameters": {{
        "param1": {{"type": "string", "description": "参数说明"}}
    }},
    "output_format": "期望的输出格式描述",
    "tags": ["领域标签1", "领域标签2"],
    "quality_score": 3,
    "skip_reason": null
}}

## 判断标准
- 如果这段文本包含**可操作的步骤、方法论、分析框架、决策规则**，则转化为技能
- 如果只是**背景介绍、历史沿革、纯理论叙述**，设置 skip_reason 并返回
- quality_score 范围 1-5：1=勉强可用，5=高度可操作；quality_score < 3 时建议设置 skip_reason
""".strip()


# ---- Token 估算 ----

def estimate_tokens(text: str) -> int:
    """
    快速 Token 估算（无需加载 tokenizer）。
    中文：约 1.5 字符 ≈ 1 token；英文：约 4 字符 ≈ 1 token。
    """
    if not text:
        return 0
    chinese_chars = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    other_chars = len(text) - chinese_chars
    return int(chinese_chars / 1.5 + other_chars / 4)


# ---- Heading 检测 ----

_HEADING_RE = re.compile(
    r"^(#{1,3})\s+.+"          # Markdown 风格 # / ## / ###
    r"|^第[一二三四五六七八九十百千\d]+[章节部]\s*[\u4e00-\u9fff\w]*"  # 中文章节
    r"|^\d+\.\s+[A-Z\u4e00-\u9fff]",  # 数字标题  "1. Introduction"
    re.MULTILINE,
)


def _extract_heading_level(line: str) -> Tuple[int, str]:
    """
    返回 (级别, 标题文本)。
      级别 1 = 最顶级  (# / 第X章)
      级别 2 = 二级标题 (## / 第X节)
      级别 3 = 三级标题 (### / 1.1 xxx)
      级别 0 = 非标题行
    """
    line = line.strip()
    if line.startswith("###"):
        return 3, line.lstrip("#").strip()
    if line.startswith("##"):
        return 2, line.lstrip("#").strip()
    if line.startswith("#"):
        return 1, line.lstrip("#").strip()
    if re.match(r"^第[一二三四五六七八九十百千\d]+章", line):
        return 1, line
    if re.match(r"^第[一二三四五六七八九十百千\d]+节", line):
        return 2, line
    if re.match(r"^\d+\.\d+\s", line):
        return 3, line
    if re.match(r"^\d+\.\s+[A-Z\u4e00-\u9fff]", line):
        return 2, line
    return 0, ""


class PDFProcessor:
    """
    PDF 文档处理流水线。

    典型用法::

        processor = PDFProcessor()
        result = await processor.pdf_to_skills("/app/data/pdfs/example.pdf")
        print(result.skills_generated)
    """

    # -------------------------------------------------------------------
    # 提取层
    # -------------------------------------------------------------------

    async def extract_text(self, pdf_path: str) -> PDFExtractResult:
        """
        提取 PDF 全文文本和表格数据。

        使用双引擎策略：
          - pypdf  — 基础文本提取（速度快）
          - pdfplumber — 补充表格识别
        如果某行 pypdf 提取为空，尝试用 pdfplumber 重提。

        Args:
            pdf_path: PDF 文件的绝对路径。

        Returns:
            PDFExtractResult — 含全文、逐页内容及元数据。
        """
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(self._extract_text_sync, pdf_path))

    def _extract_text_sync(self, pdf_path: str) -> PDFExtractResult:
        """同步提取，由 run_in_executor 调用以防止阻塞事件循环。"""
        try:
            import pypdf
            import pdfplumber
        except ImportError as e:
            raise ImportError(
                f"缺少 PDF 处理依赖: {e}。"
                "请确认 requirements.txt 中已包含 pypdf 和 pdfplumber 并重新构建镜像。"
            ) from e

        pages: List[PageContent] = []
        full_text_parts: List[str] = []
        metadata: Dict[str, Any] = {}

        # ---- pypdf 基础提取 ----
        with pypdf.PdfReader(pdf_path) as reader:
            raw_meta = reader.metadata or {}
            metadata = {
                "title": raw_meta.get("/Title", ""),
                "author": raw_meta.get("/Author", ""),
                "subject": raw_meta.get("/Subject", ""),
                "page_count": len(reader.pages),
            }
            page_texts_pypdf = []
            for page in reader.pages:
                page_texts_pypdf.append(page.extract_text() or "")

        # ---- pdfplumber 补充表格 + 改善空白页文本 ----
        with pdfplumber.open(pdf_path) as pdf:
            for i, plumber_page in enumerate(pdf.pages):
                base_text = page_texts_pypdf[i] if i < len(page_texts_pypdf) else ""
                # 如果 pypdf 提取为空，用 pdfplumber 补充
                if not base_text.strip():
                    base_text = plumber_page.extract_text() or ""

                # 表格提取
                tables_raw = plumber_page.extract_tables() or []
                # 每个 table 是 list[list[str|None]]，统一转为 list[list[str]]
                tables: List[List[List[str]]] = [
                    [[str(cell) if cell is not None else "" for cell in row] for row in table]
                    for table in tables_raw
                ]

                pages.append(PageContent(
                    page_number=i + 1,
                    text=base_text,
                    tables=tables,
                ))
                full_text_parts.append(base_text)

        full_text = "\n".join(full_text_parts)
        return PDFExtractResult(
            text=full_text,
            pages=pages,
            page_count=len(pages),
            metadata=metadata,
        )

    # -------------------------------------------------------------------
    # 分块层（三级递降算法）
    # -------------------------------------------------------------------

    def chunk_text(
        self,
        pages: List[PageContent],
        max_tokens: int = 6000,
        overlap_tokens: int = 200,
    ) -> List[TextChunk]:
        """
        三级递降语义分块算法（§9.7）。

        Level 1：按一级标题分割大章节。
        Level 2：若章节仍超 max_tokens，按段落（双换行）切分。
        Level 3：若段落仍超 max_tokens，按 Token 窗口滑动切分（带 overlap）。

        每个 Chunk 携带 source_pages（来源页码）和 heading_path（章节路径）。

        Args:
            pages:          逐页内容列表。
            max_tokens:     单个 Chunk 的最大 Token 数，默认 6000。
            overlap_tokens: 滑动窗口重叠 Token 数，默认 200。

        Returns:
            List[TextChunk] — 分块结果列表。
        """
        # 构建 (文本行, 页码) 的平铺序列
        lines_with_pages: List[Tuple[str, int]] = []
        for page in pages:
            for line in page.text.splitlines():
                lines_with_pages.append((line, page.page_number))
            # 页间加空行分隔
            lines_with_pages.append(("", page.page_number))

        # Level 1: 按一级标题切分大章节
        sections = self._split_by_headings(lines_with_pages, target_level=1)

        chunks: List[TextChunk] = []
        chunk_counter = 0

        for section_text, section_pages, heading_path in sections:
            section_tokens = estimate_tokens(section_text)
            if section_tokens <= max_tokens:
                # 整章直接作为一个 Chunk
                chunks.append(TextChunk(
                    chunk_id=f"chunk_{chunk_counter:04d}",
                    text=section_text.strip(),
                    source_pages=sorted(set(section_pages)),
                    heading_path=heading_path,
                    token_count=section_tokens,
                ))
                chunk_counter += 1
            else:
                # Level 2: 按段落切分
                paragraphs = self._split_by_paragraphs(section_text, section_pages)
                buffer_text = ""
                buffer_pages: List[int] = []
                buffer_tokens = 0

                for para_text, para_pages in paragraphs:
                    para_tokens = estimate_tokens(para_text)

                    if para_tokens > max_tokens:
                        # Level 3: 单段落超长 → 滑动窗口
                        if buffer_text.strip():
                            chunks.append(TextChunk(
                                chunk_id=f"chunk_{chunk_counter:04d}",
                                text=buffer_text.strip(),
                                source_pages=sorted(set(buffer_pages)),
                                heading_path=heading_path,
                                token_count=buffer_tokens,
                            ))
                            chunk_counter += 1
                            buffer_text, buffer_pages, buffer_tokens = "", [], 0

                        sub_chunks = self._sliding_window(
                            para_text, para_pages, max_tokens, overlap_tokens, heading_path, chunk_counter
                        )
                        chunks.extend(sub_chunks)
                        chunk_counter += len(sub_chunks)
                        continue

                    if buffer_tokens + para_tokens > max_tokens and buffer_text.strip():
                        # 缓冲区满，先提交
                        chunks.append(TextChunk(
                            chunk_id=f"chunk_{chunk_counter:04d}",
                            text=buffer_text.strip(),
                            source_pages=sorted(set(buffer_pages)),
                            heading_path=heading_path,
                            token_count=buffer_tokens,
                        ))
                        chunk_counter += 1
                        buffer_text, buffer_pages, buffer_tokens = "", [], 0

                    buffer_text += para_text + "\n\n"
                    buffer_pages.extend(para_pages)
                    buffer_tokens += para_tokens

                # 提交尾部缓冲
                if buffer_text.strip():
                    chunks.append(TextChunk(
                        chunk_id=f"chunk_{chunk_counter:04d}",
                        text=buffer_text.strip(),
                        source_pages=sorted(set(buffer_pages)),
                        heading_path=heading_path,
                        token_count=buffer_tokens,
                    ))
                    chunk_counter += 1

        return [c for c in chunks if c.text.strip()]

    def _split_by_headings(
        self,
        lines_with_pages: List[Tuple[str, int]],
        target_level: int = 1,
    ) -> List[Tuple[str, List[int], List[str]]]:
        """
        按目标层级的标题切割文本。

        Returns:
            List of (section_text, page_numbers, heading_path)
        """
        sections: List[Tuple[str, List[int], List[str]]] = []
        current_lines: List[str] = []
        current_pages: List[int] = []
        current_heading: List[str] = []

        for line, page_num in lines_with_pages:
            level, heading_text = _extract_heading_level(line)
            if level == target_level and current_lines:
                sections.append(("\n".join(current_lines), list(current_pages), list(current_heading)))
                current_lines = []
                current_pages = []
                current_heading = [heading_text]
            else:
                if level == target_level:
                    current_heading = [heading_text]
            current_lines.append(line)
            current_pages.append(page_num)

        if current_lines:
            sections.append(("\n".join(current_lines), list(current_pages), list(current_heading)))

        return sections if sections else [
            ("\n".join(l for l, _ in lines_with_pages),
             [p for _, p in lines_with_pages],
             [])
        ]

    def _split_by_paragraphs(
        self, text: str, pages: List[int]
    ) -> List[Tuple[str, List[int]]]:
        """
        按段落（双换行）切分文本，保留每段的来源页码（近似）。
        """
        paragraphs = re.split(r"\n{2,}", text)
        if not paragraphs:
            return [(text, pages)]
        # 平均分配页码（近似）
        total = len(paragraphs)
        result = []
        for i, para in enumerate(paragraphs):
            if not para.strip():
                continue
            # 按段落位置比例估算页码分配
            start_ratio = i / total
            end_ratio = (i + 1) / total
            page_start = max(0, int(start_ratio * len(pages)))
            page_end = max(page_start + 1, int(end_ratio * len(pages)))
            para_pages = pages[page_start:page_end] or pages[:1]
            result.append((para, para_pages))
        return result or [(text, pages)]

    def _sliding_window(
        self,
        text: str,
        pages: List[int],
        max_tokens: int,
        overlap_tokens: int,
        heading_path: List[str],
        start_counter: int,
    ) -> List[TextChunk]:
        """按 Token 窗口滑动切分超长段落（Level 3）。"""
        words = text.split()
        chunks: List[TextChunk] = []
        # 估算每词平均 token
        avg_token_per_word = max(1, estimate_tokens(text) / max(len(words), 1))
        step = max(1, int(max_tokens / avg_token_per_word))
        overlap_words = max(0, int(overlap_tokens / avg_token_per_word))
        i = 0
        idx = start_counter
        while i < len(words):
            end = min(i + step, len(words))
            window_text = " ".join(words[i:end])
            token_count = estimate_tokens(window_text)
            # 来源页码近似
            ratio_start = i / len(words)
            ratio_end = end / len(words)
            p_start = int(ratio_start * len(pages))
            p_end = max(p_start + 1, int(ratio_end * len(pages)))
            window_pages = pages[p_start:p_end] or pages[:1]
            chunks.append(TextChunk(
                chunk_id=f"chunk_{idx:04d}",
                text=window_text.strip(),
                source_pages=sorted(set(window_pages)),
                heading_path=heading_path,
                token_count=token_count,
            ))
            idx += 1
            i = end - overlap_words
            if i >= len(words) - overlap_words:
                break

        return chunks

    # -------------------------------------------------------------------
    # 摘要层
    # -------------------------------------------------------------------

    async def summarize_chunk(
        self,
        chunk: TextChunk,
        doc_title: str = "",
        doc_context: str = "",
    ) -> Optional[SkillDraft]:
        """
        调用 LLM 对单个 Chunk 生成结构化技能草案（SkillDraft）。

        Args:
            chunk:       待摘要的文本切片。
            doc_title:   PDF 文档标题（注入 Prompt 上下文）。
            doc_context: 全文摘要（可选，注入 Prompt 丰富上下文）。

        Returns:
            SkillDraft — 解析成功；None — LLM 判断此 Chunk 不适合转为技能。
        """
        from app.services.model_router import model_router
        from app.models.schemas import Message

        heading_path_str = " > ".join(chunk.heading_path) if chunk.heading_path else "正文"
        page_range_str = (
            f"{min(chunk.source_pages)}-{max(chunk.source_pages)}"
            if chunk.source_pages
            else "未知"
        )

        prompt = CHUNK_TO_SKILL_PROMPT.format(
            doc_title=doc_title or "未知文档",
            heading_path=heading_path_str,
            page_range=page_range_str,
            chunk_text=chunk.text[:4000],  # 限制单 Chunk 注入 token
        )

        messages = [
            Message(role="system", content="你是专业的知识工程师，善于将文档内容转化为结构化 AI 技能描述。请只输出 JSON，不要添加 markdown 代码块。"),
            Message(role="user", content=prompt),
        ]

        try:
            response = await model_router.generate(
                messages=messages,
                temperature=0.3,
                max_tokens=1500,
            )
            raw = response.content.strip()
            # 去除可能残留的 markdown 代码块标记
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
            data = json.loads(raw)
            draft = SkillDraft(**data)
            if draft.skip_reason:
                logger.info(f"Chunk {chunk.chunk_id} skipped: {draft.skip_reason}")
                return None
            if draft.quality_score < 3:
                logger.info(
                    f"Chunk {chunk.chunk_id} quality_score={draft.quality_score} < 3, skipping."
                )
                return None
            return draft
        except json.JSONDecodeError as e:
            logger.warning(
                f"Chunk {chunk.chunk_id}: LLM 输出非法 JSON: {e}. "
                f"原始输出: {response.content[:200]}"
            )
            return None
        except Exception as e:
            logger.error(f"Chunk {chunk.chunk_id} summarize error: {e}")
            return None

    # -------------------------------------------------------------------
    # 生成层
    # -------------------------------------------------------------------

    def generate_skill_md(
        self,
        draft: SkillDraft,
        skill_id: str,
        output_dir: str,
        source_pdf_id: str = "",
        source_pages: str = "",
    ) -> str:
        """
        将 SkillDraft 转化为 Anthropic Skills 协议的目录结构：

            {output_dir}/{skill_id}/
            ├── SKILL.md     # YAML frontmatter + Markdown 正文
            └── scripts/
                └── main.py  # 知识型技能无实际执行逻辑，仅作占位

        Args:
            draft:        LLM 生成的技能草案。
            skill_id:     技能唯一 ID（kebab-case）。
            output_dir:   技能目录的父级路径。
            source_pdf_id: 来源 PDF 的 doc_id（用于溯源）。
            source_pages: 来源页码范围字符串，如 "12-18"。

        Returns:
            生成的 SKILL.md 文件路径。
        """
        skill_dir = os.path.join(output_dir, skill_id)
        scripts_dir = os.path.join(skill_dir, "scripts")
        os.makedirs(scripts_dir, exist_ok=True)

        # ---- YAML frontmatter ----
        parameters_schema = draft.input_parameters or {}
        if parameters_schema:
            # 确保格式合法
            ps: Dict[str, Any] = {
                "type": "object",
                "properties": {
                    k: (v if isinstance(v, dict) else {"type": "string", "description": str(v)})
                    for k, v in parameters_schema.items()
                },
                "required": [],
            }
        else:
            ps = {"type": "object", "properties": {}, "required": []}

        frontmatter: Dict[str, Any] = {
            "id": skill_id,
            "name": draft.display_name or draft.skill_name,
            "description": draft.description,
            "language": "python",
            "entrypoint": f"scripts/main.py",
            "parameters_schema": ps,
            "tags": draft.tags,
        }
        if source_pdf_id:
            frontmatter["source_pdf_id"] = source_pdf_id
        if source_pages:
            frontmatter["source_pages"] = source_pages

        frontmatter_yaml = yaml.dump(
            frontmatter, allow_unicode=True, default_flow_style=False, sort_keys=False
        )

        # ---- Markdown 正文 ----
        trigger_md = "\n".join(f"- {t}" for t in draft.trigger_conditions) if draft.trigger_conditions else "- 按需调用"
        content_md = f"""---
{frontmatter_yaml.strip()}
---

# {draft.display_name or draft.skill_name}

## 描述
{draft.description}

## 触发条件
{trigger_md}

## 执行逻辑
{draft.execution_logic or "（知识型技能，无固定执行步骤）"}

## 输出格式
{draft.output_format or "自然语言或结构化 JSON"}
"""

        skill_md_path = os.path.join(skill_dir, "SKILL.md")
        with open(skill_md_path, "w", encoding="utf-8") as f:
            f.write(content_md)

        # ---- scripts/main.py — 知识型技能占位脚本 ----
        script_content = f'''#!/usr/bin/env python3
"""
技能脚本：{draft.display_name or draft.skill_name}
来源 PDF：{source_pdf_id or "N/A"}  页码：{source_pages or "N/A"}
该技能为知识型技能，主要通过 RAG 检索调用；如需编程化执行请扩展此脚本。
"""
import json, sys

def main(params: dict) -> dict:
    """
    执行技能逻辑。

    Args:
        params: 来自技能调用方的参数字典。

    Returns:
        包含执行结果的字典。
    """
    return {{
        "skill_id": "{skill_id}",
        "description": """{draft.description}""",
        "message": "该技能为知识型技能，请参考 SKILL.md 中的执行逻辑手动处理，"
                   "或由 LLM 根据 SKILL.md 内容自动生成具体逻辑。",
        "params_received": params,
    }}

if __name__ == "__main__":
    raw = sys.stdin.read().strip()
    params = json.loads(raw) if raw else {{}}
    result = main(params)
    print(json.dumps(result, ensure_ascii=False, indent=2))
'''
        with open(os.path.join(scripts_dir, "main.py"), "w", encoding="utf-8") as f:
            f.write(script_content)

        logger.info(f"Generated SKILL.md at {skill_md_path}")
        return skill_md_path

    # -------------------------------------------------------------------
    # 全流水线
    # -------------------------------------------------------------------

    async def pdf_to_skills(
        self,
        pdf_path: str,
        output_dir: str = "/app/skills",
        skill_prefix: str = "",
        max_tokens_per_chunk: int = 6000,
        doc_id: Optional[str] = None,
    ) -> PipelineResult:
        """
        完整 PDF→Skills 流水线（§9.6.1 序列图实现）：
          1. extract_text()      — 提取全文
          2. chunk_text()        — 三级递降分块
          3. summarize_chunk()   — 每 Chunk → SkillDraft（LLM）
          4. generate_skill_md() — SkillDraft → SKILL.md 文件
          5. load_dir()          — 批量注册 SQLite + ChromaDB

        Args:
            pdf_path:             PDF 文件路径。
            output_dir:           技能输出目录（容器内路径）。
            skill_prefix:         技能 ID 前缀（如 "finance_"）。
            max_tokens_per_chunk: 单 Chunk 最大 Token 数。
            doc_id:               关联的 PDFDocument.id（可选，用于状态更新）。

        Returns:
            PipelineResult — 流水线执行摘要。
        """
        skills_generated: List[str] = []
        errors: List[str] = []
        total_chunks = 0

        # ---- Step 1: 提取文本 ----
        logger.info(f"[Pipeline] Step 1: Extracting text from {pdf_path}")
        try:
            extract_result = await self.extract_text(pdf_path)
        except Exception as e:
            logger.error(f"[Pipeline] Extract failed: {e}")
            return PipelineResult(
                pdf_path=pdf_path,
                total_pages=0,
                total_chunks=0,
                errors=[f"Extract failed: {e}"],
            )

        doc_title = extract_result.metadata.get("title", "") or os.path.basename(pdf_path)
        logger.info(f"[Pipeline] Extracted {extract_result.page_count} pages.")

        # ---- Step 2: 语义分块 ----
        logger.info("[Pipeline] Step 2: Chunking text...")
        chunks = self.chunk_text(
            extract_result.pages,
            max_tokens=max_tokens_per_chunk,
        )
        total_chunks = len(chunks)
        logger.info(f"[Pipeline] Generated {total_chunks} chunks.")

        # ---- 更新 PDFDocument 进度（如果传入了 doc_id）----
        if doc_id:
            await self._update_pdf_doc_status(doc_id, "processing", total_chunks=total_chunks)

        # ---- Step 3 + 4: 逐 Chunk 摘要 + 生成 SKILL.md ----
        skills_dir_generated: List[Tuple[str, str, str]] = []  # (skill_id, skill_dir, pages_str)

        for i, chunk in enumerate(chunks):
            logger.info(
                f"[Pipeline] Step 3/4: Processing chunk {i+1}/{total_chunks} "
                f"(tokens={chunk.token_count}) ..."
            )
            try:
                draft = await self.summarize_chunk(chunk, doc_title=doc_title)
                if draft is None:
                    continue

                # 生成唯一技能 ID
                raw_name = re.sub(r"[^a-z0-9-]", "-", draft.skill_name.lower())
                raw_name = re.sub(r"-+", "-", raw_name).strip("-") or f"skill-{uuid.uuid4().hex[:6]}"
                skill_id = f"{skill_prefix}{raw_name}" if skill_prefix else raw_name
                # 若已存在则加后缀
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
                skills_dir_generated.append((skill_id, os.path.dirname(skill_md_path), pages_str))
                skills_generated.append(skill_id)

            except Exception as e:
                err_msg = f"Chunk {chunk.chunk_id}: {e}"
                logger.error(f"[Pipeline] {err_msg}")
                errors.append(err_msg)

        # ---- Step 5: 批量注册到 SQLite + ChromaDB ----
        logger.info(f"[Pipeline] Step 5: Registering {len(skills_dir_generated)} skills...")
        skills_registered = 0
        for skill_id, skill_dir, pages_str in skills_dir_generated:
            try:
                registered = await self._register_skill_from_dir(
                    skill_dir=skill_dir,
                    skill_id=skill_id,
                    source_pdf_id=doc_id or "",
                    source_pages=pages_str,
                )
                if registered:
                    skills_registered += 1
            except Exception as e:
                err_msg = f"Register {skill_id}: {e}"
                logger.error(f"[Pipeline] {err_msg}")
                errors.append(err_msg)

        # ---- 更新 PDFDocument 完成状态 ----
        if doc_id:
            await self._update_pdf_doc_status(
                doc_id, "completed",
                skills_generated=skills_generated,
                completed_at=datetime.now(timezone.utc),
            )

        result = PipelineResult(
            pdf_path=pdf_path,
            total_pages=extract_result.page_count,
            total_chunks=total_chunks,
            skills_generated=skills_generated,
            skills_skipped=total_chunks - len(skills_generated),
            skills_registered=skills_registered,
            errors=errors,
        )
        logger.info(
            f"[Pipeline] Completed: {skills_registered} skills registered, "
            f"{result.skills_skipped} chunks skipped, {len(errors)} errors."
        )
        return result

    # -------------------------------------------------------------------
    # 辅助方法
    # -------------------------------------------------------------------

    async def _register_skill_from_dir(
        self,
        skill_dir: str,
        skill_id: str,
        source_pdf_id: str = "",
        source_pages: str = "",
    ) -> bool:
        """
        从已生成的技能目录（含 SKILL.md）注册到 SQLite + ChromaDB。

        Returns:
            True — 注册成功；False — 已存在，跳过。
        """
        from app.services.skill_loader import skill_loader
        from app.services.memory_retriever import memory_retriever
        from app.models.database import SkillMetadata
        from app.models.schemas import SkillManifest
        from sqlmodel import Session, select
        from app.core.db import engine

        skill_md_path = os.path.join(skill_dir, "SKILL.md")
        if not os.path.exists(skill_md_path):
            logger.warning(f"SKILL.md not found in {skill_dir}, skipping.")
            return False

        # ---- 解析 SKILL.md ----
        parsed_list = skill_loader.load_dir(os.path.dirname(skill_dir))
        parsed = next((p for p in parsed_list if p.id == skill_id), None)
        if not parsed:
            # 尝试直接加载单个
            parsed_single = skill_loader.load_one(skill_dir)
            if not parsed_single:
                logger.warning(f"Failed to parse SKILL.md for {skill_id}")
                return False
            parsed = parsed_single

        # ---- 检查是否已存在 ----
        with Session(engine) as session:
            existing = session.exec(
                select(SkillMetadata).where(SkillMetadata.id == skill_id)
            ).first()
            if existing:
                logger.info(f"Skill '{skill_id}' already registered, skipping.")
                return False

        # ---- 写入 SQLite ----
        skill_meta = SkillMetadata(
            id=parsed.id,
            name=parsed.name,
            language=parsed.language,
            entrypoint=parsed.entrypoint,
            description=parsed.description,
            parameters_schema=parsed.parameters_schema,
            source_pdf_id=source_pdf_id or None,
            source_pages=source_pages or None,
            tags=[],
        )
        with Session(engine) as session:
            session.add(skill_meta)
            session.commit()

        # ---- 写入 ChromaDB ----
        skill_manifest = SkillManifest(
            id=parsed.id,
            name=parsed.name,
            description=parsed.description,
            language=parsed.language,
            content=parsed.script_content or "",
        )
        await memory_retriever.add_skill(skill_manifest)

        logger.info(f"Skill '{skill_id}' registered from {skill_dir}.")
        return True

    async def _update_pdf_doc_status(
        self,
        doc_id: str,
        status: str,
        total_chunks: int = 0,
        skills_generated: Optional[List[str]] = None,
        completed_at: Optional[datetime] = None,
    ):
        """更新 PDFDocument 记录的状态字段。"""
        from app.models.database import PDFDocument
        from sqlmodel import Session, select
        from app.core.db import engine

        with Session(engine) as session:
            doc = session.exec(select(PDFDocument).where(PDFDocument.id == doc_id)).first()
            if doc:
                doc.status = status
                if total_chunks:
                    doc.total_chunks = total_chunks
                if skills_generated is not None:
                    doc.skills_generated = skills_generated
                if completed_at:
                    doc.completed_at = completed_at
                session.add(doc)
                session.commit()


    @staticmethod
    def compute_file_hash(file_path: str) -> str:
        """计算文件的 SHA-256 哈希值（用于幂等去重）。"""
        sha256 = hashlib.sha256()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
        return sha256.hexdigest()


# 全局单例
pdf_processor = PDFProcessor()
