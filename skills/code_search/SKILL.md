---
id: code_search
name: 代码语义搜索
description: |
  在项目代码库中进行语义搜索，定位函数、类、方法的精确位置。
  返回文件路径、行号范围、函数签名等信息，AI 可据此精准读取代码片段，
  大幅降低上下文 Token 消耗（平均节省 90%）。
  适用于：查找某个功能的实现位置、了解某个模块的结构、定位需要修改的代码。
  注意：此技能搜索的是项目自身的代码，而非互联网内容。
language: python
entrypoint: skills/code_search/scripts/main.py
parameters_schema:
  type: object
  properties:
    query:
      type: string
      description: |
        自然语言搜索查询，描述你要找的代码功能。例如：
        "处理 PDF 上传的函数"
        "ChromaDB 向量检索"
        "对话消息持久化"
        "Tool Calling 循环逻辑"
    top_k:
      type: integer
      description: 返回结果数量（默认 5，最大 20）
    symbol_types:
      type: array
      items:
        type: string
      description: "过滤符号类型：function / method / class / module / global_var（不填则搜索所有类型）"
    file_pattern:
      type: string
      description: "文件路径前缀过滤，如 'app/services/' 只搜索 services 目录"
  required:
    - query
tags:
  - code
  - search
  - development
  - index
  - watery-internal
---

## 代码语义搜索技能

通过语义向量搜索在项目代码库中定位符号，返回精确的文件路径和行号范围。

### 返回格式

每个结果包含：
- `file_path`：相对文件路径（如 `app/services/worker.py`）
- `symbol_name`：函数/类/方法名（方法格式为 `ClassName.method_name`）
- `symbol_type`：符号类型（function / method / class / module）
- `line_start` / `line_end`：精确行号范围（1-based）
- `signature`：完整函数签名（含参数类型和返回类型）
- `docstring`：文档字符串摘要
- `relevance_score`：相关度评分（0-1）

### 最佳实践

1. 先用自然语言描述你要找的功能（用中文或英文均可）
2. 拿到结果后，用 `file_path` + `line_start` ~ `line_end` 精准读取代码
3. 如果结果不够精确，尝试换一种描述或使用 `file_pattern` 缩小搜索范围
4. 用 `symbol_types: ["class"]` 只搜索类定义，了解整体结构
