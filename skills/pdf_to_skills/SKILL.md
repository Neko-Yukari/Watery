---
id: pdf_to_skills
name: PDF 转技能包
description: |
  将一个 PDF 文档自动转化为一组可调用的 AI 技能。
  流程：提取文本 → 语义分块（标题/段落/Token 窗口三级递降）→
  调用 LLM 结构化摘要 → 生成 SKILL.md → 注册到技能库。
  适用于专业书籍、操作手册、行业报告、技术规范。
language: python
entrypoint: scripts/main.py
parameters_schema:
  type: object
  properties:
    pdf_path:
      type: string
      description: 待处理的 PDF 文件路径（容器内绝对路径）
    skill_prefix:
      type: string
      description: 生成技能 ID 的前缀（如 'finance_' 则生成 'finance_xxx'），留空则不加前缀
    max_tokens_per_chunk:
      type: integer
      description: 每个语义块的最大 Token 数，默认 6000
    output_dir:
      type: string
      description: 生成的 SKILL.md 输出目录，默认 /app/skills
  required:
    - pdf_path
tags:
  - pdf
  - skill-generation
  - pipeline
  - knowledge
---

# PDF 转技能包

## 描述

将一个 PDF 文档自动转化为一组可调用的 AI 技能。整个流程全自动异步执行，
从原始 PDF 文件到注册到系统的可调用技能，无需人工干预。

## 触发条件

- 当用户提供专业书籍或文档并要求"学习"该文档时
- 当需要将知识库文档转化为可执行技能时
- 当 Manager Agent 发现技能库缺少某领域知识时

## 执行逻辑

1. **PDF 提取**：调用 `/api/v1/pdf/upload` 上传文件，或使用已上传的 doc_id
2. **触发流水线**：调用 `POST /api/v1/pdf/to-skills` 触发异步处理
3. **等待进度**：轮询 `GET /api/v1/pdf/status/{doc_id}` 直到 status=completed
4. **结果汇报**：返回生成的技能 ID 列表

## 输出格式

```json
{
  "doc_id": "uuid",
  "status": "completed",
  "total_pages": 320,
  "total_chunks": 48,
  "skills_generated": ["skill-name-1", "skill-name-2"],
  "skills_registered": 46,
  "errors": []
}
```
