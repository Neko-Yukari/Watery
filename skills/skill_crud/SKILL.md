---
id: skill_crud
name: 技能库管理
description: |
  用于创建、更新、删除技能库中的技能。这是一个元技能（meta-skill），
  允许 AI Agent 在运行过程中发现知识缺口时自主补充技能库，
  或在发现已有技能内容不全/不准确时进行修正。
  支持 create / update / delete 三种操作。
language: python
entrypoint: scripts/main.py
parameters_schema:
  type: object
  properties:
    operation:
      type: string
      enum:
        - create
        - update
        - delete
      description: 操作类型
    skill_id:
      type: string
      description: 技能 ID（update/delete 时必填）
    skill_data:
      type: object
      description: 技能数据（create/update 时提供）
      properties:
        name:
          type: string
        description:
          type: string
        language:
          type: string
        entrypoint:
          type: string
        parameters_schema:
          type: object
        content:
          type: string
          description: 技能正文/执行逻辑（用于知识型技能）
  required:
    - operation
tags:
  - meta
  - self-amendment
  - skills-management
---

# 技能库管理（元技能）

## 描述

元技能（meta-skill）：允许 AI Agent 在运行时自主管理技能库，实现"知识自我进化"。
当 Worker Agent 检测到 RAG 检索相似度 < 阈值，或发现知识缺口时，
自动调用此技能创建/更新技能，无需人工干预。

## 触发条件

- 当 Worker Agent RAG 检索相似度 < 0.5（知识缺口检测）
- 当 Agent 在执行任务时发现已有技能描述不完整或不准确
- 当需要删除已废弃或错误的技能时

## 执行逻辑

### CREATE — 创建新技能

```python
params = {
    "operation": "create",
    "skill_data": {
        "id": "new-skill-name",
        "name": "技能名称",
        "description": "描述",
        "language": "python",
        "entrypoint": "scripts/main.py",
        "parameters_schema": {},
        "content": "技能脚本内容（可选）"
    }
}
```

调用 `POST /api/v1/skills` 写入 SQLite + ChromaDB。

### UPDATE — 更新已有技能

```python
params = {
    "operation": "update",
    "skill_id": "existing-skill-id",
    "skill_data": {
        "description": "更新后的描述"
    }
}
```

调用 `PUT /api/v1/skills/{skill_id}` 就地更新。

### DELETE — 删除技能

```python
params = {
    "operation": "delete",
    "skill_id": "skill-to-delete"
}
```

调用 `DELETE /api/v1/skills/{skill_id}` 从系统删除。

## 输出格式

```json
{
  "operation": "create",
  "skill_id": "new-skill-id",
  "status": "success",
  "message": "技能已创建并注册到系统"
}
```
