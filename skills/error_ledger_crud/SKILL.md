---
id: error_ledger_crud
name: 错题库管理
description: |
  用于创建、查询、删除错题库（Error Ledger）中的条目。
  允许 AI Agent 在对话中自主记录新发现的错误经验、查询已有错题、或删除过时条目。
  每条错题包含标题、上下文、纠正方案、预防建议和标签。
  通过调用本地 FastAPI 后端的 Error Entries CRUD API 完成操作。
language: python
entrypoint: scripts/main.py
parameters_schema:
  type: object
  properties:
    operation:
      type: string
      enum:
        - create
        - list
        - get
        - delete
        - search
      description: |
        操作类型：
        - create: 创建新错题
        - list: 列出错题（可按标签/严重程度筛选）
        - get: 获取单条错题详情
        - delete: 删除指定错题
        - search: 按关键词搜索错题
    entry_id:
      type: string
      description: 错题 ID（get/delete 时必填）
    entry_data:
      type: object
      description: 错题数据（create 时必填）
      properties:
        title:
          type: string
          description: 简短标题（一句话概括错误）
        context:
          type: string
          description: 错误发生的完整上下文描述
        correction:
          type: string
          description: 纠正方案
        prevention:
          type: string
          description: 预防建议
        tags:
          type: array
          items:
            type: string
          description: 标签数组，如 ["python", "docker"]
        severity:
          type: string
          enum: [critical, warning, info]
          description: 严重程度
    tags:
      type: string
      description: 筛选标签（list 时可选，逗号分隔，如 "python,docker"）
    severity:
      type: string
      description: 筛选严重程度（list 时可选）
    search:
      type: string
      description: 搜索关键词（search 操作时必填）
  required:
    - operation
tags:
  - meta
  - error-ledger
  - self-amendment
---

# 错题库管理

## 描述

允许 AI Agent 在对话中自主管理错题库。当 Agent 在对话中发现用户遇到了新的技术问题、
编码错误或配置陷阱时，可以主动调用此技能将经验记录到错题库，供未来的任务执行时参考。

## 适用场景

- 用户在对话中描述了一个 Bug 的解决过程 → 记录为错题
- 用户要求"帮我查查之前遇到过的 Docker 相关问题" → 按标签查询
- 定期回顾当天对话，提炼错误经验 → 批量创建
- 清理过时或不再适用的错题 → 删除

## 执行逻辑

### CREATE — 创建新错题

```json
{
  "operation": "create",
  "entry_data": {
    "title": "NumPy 2.0 不兼容导致 ChromaDB 崩溃",
    "context": "chromadb 0.4.24 依赖 numpy 旧版别名，numpy 2.0+ 移除了 np.float_",
    "correction": "在 requirements.txt 中将 numpy 版本限制在 <2.0.0",
    "prevention": "对底层依赖较重的库，固化次版本号",
    "tags": ["python", "numpy", "chromadb", "dependency"],
    "severity": "warning"
  }
}
```

### LIST — 列出错题

```json
{
  "operation": "list",
  "tags": "docker,python",
  "severity": "critical"
}
```

### SEARCH — 搜索错题

```json
{
  "operation": "search",
  "search": "代理配置"
}
```
