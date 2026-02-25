#!/usr/bin/env python3
"""
技能脚本：PDF 文本提取
描述：从 PDF 文件中提取纯文本内容和表格数据，输出结构化 JSON。
依赖：pypdf>=4.0.0, pdfplumber>=0.11.0
"""
import json
import sys
import re
from typing import Any, Dict, List, Optional


def parse_page_range(page_range_str: str, total_pages: int) -> List[int]:
    """
    解析页码范围字符串，如 "1-5" 或 "1,3,5"。

    Args:
        page_range_str: 页码字符串（1-based）
        total_pages:    总页数

    Returns:
        0-based 页码索引列表
    """
    pages = []
    if "-" in page_range_str and "," not in page_range_str:
        parts = page_range_str.split("-")
        start = int(parts[0]) - 1
        end = int(parts[1])
        pages = list(range(start, min(end, total_pages)))
    else:
        for p in page_range_str.split(","):
            p = p.strip()
            if p.isdigit():
                idx = int(p) - 1
                if 0 <= idx < total_pages:
                    pages.append(idx)
    return pages


def extract_pdf(pdf_path: str, extract_tables: bool = True, page_range: Optional[str] = None) -> Dict[str, Any]:
    """
    提取 PDF 文本和表格。

    Args:
        pdf_path:       PDF 文件路径
        extract_tables: 是否提取表格
        page_range:     页码范围字符串（可选）

    Returns:
        dict — {text, page_count, pages, metadata}
    """
    try:
        import pypdf
        import pdfplumber
    except ImportError as e:
        return {"error": f"缺少依赖: {e}. 请确认已安装 pypdf 和 pdfplumber"}

    result_pages = []
    all_text_parts = []
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
        total_pages = len(reader.pages)
        page_texts_pypdf = [page.extract_text() or "" for page in reader.pages]

    # 确定处理的页码范围
    if page_range:
        target_pages = parse_page_range(page_range, total_pages)
    else:
        target_pages = list(range(total_pages))

    # ---- pdfplumber 补充表格 ----
    with pdfplumber.open(pdf_path) as pdf:
        for i in target_pages:
            plumber_page = pdf.pages[i]
            base_text = page_texts_pypdf[i]
            if not base_text.strip():
                base_text = plumber_page.extract_text() or ""

            tables: List[List[List[str]]] = []
            if extract_tables:
                raw_tables = plumber_page.extract_tables() or []
                tables = [
                    [[str(cell) if cell is not None else "" for cell in row] for row in table]
                    for table in raw_tables
                ]

            result_pages.append({
                "page_number": i + 1,
                "text": base_text,
                "tables": tables,
            })
            all_text_parts.append(base_text)

    return {
        "text": "\n".join(all_text_parts),
        "page_count": total_pages,
        "pages": result_pages,
        "metadata": metadata,
    }


def main(params: dict) -> dict:
    """
    主入口函数。

    Args:
        params: {
            "pdf_path": str,           # 必填
            "extract_tables": bool,    # 可选，默认 true
            "page_range": str,         # 可选
        }

    Returns:
        提取结果 dict 或错误 dict
    """
    pdf_path = params.get("pdf_path")
    if not pdf_path:
        return {"error": "必须提供 pdf_path 参数"}

    import os
    if not os.path.exists(pdf_path):
        return {"error": f"文件不存在: {pdf_path}"}

    extract_tables = params.get("extract_tables", True)
    page_range = params.get("page_range")

    return extract_pdf(pdf_path, extract_tables=extract_tables, page_range=page_range)


if __name__ == "__main__":
    params = {}
    if len(sys.argv) > 1:
        try:
            params = json.loads(sys.argv[1])
        except json.JSONDecodeError:
            params = {}
    result = main(params)
    print(json.dumps(result, ensure_ascii=False, indent=2))
