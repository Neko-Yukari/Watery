---
id: pdf_extract_text
name: PDF 文本提取
description: 从 PDF 文件中提取纯文本内容和表格数据，支持多页 PDF，输出结构化 JSON。
  支持文字型 PDF（书籍、手册、报告），使用 pypdf + pdfplumber 双引擎提取。
language: python
entrypoint: scripts/main.py
parameters_schema:
  type: object
  properties:
    pdf_path:
      type: string
      description: PDF 文件的绝对路径（容器内路径）
    extract_tables:
      type: boolean
      description: 是否同时提取表格，默认 true
    page_range:
      type: string
      description: "页码范围，如 '1-5' 或 '1,3,5'（省略则提取全部）"
  required:
    - pdf_path
tags:
  - pdf
  - extraction
  - document
---

# PDF 文本提取

## 描述

从 PDF 文件中提取纯文本内容和表格数据，适用于文字型 PDF（书籍、报告、技术手册）。
不依赖 OCR 或系统级工具，纯 Python 实现（pypdf + pdfplumber）。

## 触发条件

- 当用户提供 PDF 文件并要求提取文字内容时
- 当需要将 PDF 文档转化为可处理的文本格式时
- 作为 PDF-to-Skills 流水线的第一步

## 执行逻辑

1. 使用 `pypdf` 读取 PDF 基础文本（快速）
2. 使用 `pdfplumber` 补充表格识别和空白页处理
3. 按页码构建结构化结果
4. 支持 `page_range` 参数过滤特定页码
5. 返回 JSON 格式：`{text, pages, page_count, metadata}`

## 输出格式

```json
{
  "text": "全文纯文本",
  "page_count": 320,
  "pages": [
    {
      "page_number": 1,
      "text": "第一页文字...",
      "tables": [["列1", "列2"], ["数据1", "数据2"]]
    }
  ],
  "metadata": {
    "title": "文档标题",
    "author": "作者",
    "page_count": 320
  }
}
```
