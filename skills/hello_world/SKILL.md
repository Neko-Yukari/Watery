---
name: Hello World
description: 向指定名称打招呼，用于测试技能加载和执行管道是否正常工作
language: python
entrypoint: scripts/main.py
parameters_schema:
  type: object
  properties:
    name:
      type: string
      description: 打招呼的对象名称
  required: []
tags:
  - demo
  - utility
---

# Hello World 技能

这是一个最简单的演示技能，用于验证 Watery 技能库的端到端流程：

1. **技能注册** — 通过 `POST /api/v1/skills` 或 `POST /api/v1/skills/load-dir` 导入
2. **语义检索** — Worker 在处理任务时通过 ChromaDB 向量搜索找到该技能
3. **脚本执行** — SkillExecutor 调用 `scripts/main.py`，传入 JSON 参数
4. **结果返回** — 脚本标准输出 JSON，Worker 将结果写入任务数据库

## 参数说明

| 参数   | 类型   | 说明       |
|--------|--------|------------|
| `name` | string | 打招呼的对象（可选，默认 "World"） |

## 输出格式

```json
{"message": "Hello, World!"}
```

## 使用示例

```bash
# 直接通过 API 执行
curl -X POST http://localhost:18000/api/v1/skills/hello_world/run \
  -H "Content-Type: application/json" \
  -d '{"name": "Watery"}'
```
