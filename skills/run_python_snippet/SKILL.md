---
name: Run Python Snippet
description: 在沙箱中执行一段 Python 代码片段并返回标准输出，适用于数据处理、计算和脚本验证等任务
language: python
entrypoint: scripts/main.py
parameters_schema:
  type: object
  properties:
    code:
      type: string
      description: 要执行的 Python 代码字符串
    timeout:
      type: integer
      description: 执行超时秒数，默认 10
  required:
    - code
tags:
  - coding
  - execution
  - python
---

# Run Python Snippet 技能

在受控环境中执行任意 Python 代码片段，捕获标准输出和标准错误，安全返回结果。

## 适用场景

- 数学计算、公式验证
- 数据格式转换（JSON / CSV / YAML）
- 字符串处理、正则提取
- 简单算法验证

## 参数说明

| 参数      | 类型    | 说明                              |
|-----------|---------|-----------------------------------|
| `code`    | string  | Python 代码（必填）               |
| `timeout` | integer | 超时秒数（可选，默认 10s，最大 60s） |

## 输出格式

```json
{
  "stdout": "代码标准输出内容",
  "stderr": "错误信息（如有）",
  "exit_code": 0
}
```

## 安全说明

> ⚠️ 脚本在容器内直接执行，请仅传入可信代码。
> 生产环境建议结合 ms-enclave 沙箱隔离。

## 使用示例

```bash
curl -X POST http://localhost:18000/api/v1/skills/run_python_snippet/run \
  -H "Content-Type: application/json" \
  -d '{"code": "print(sum(range(1, 101)))"}'
# 输出: {"stdout": "5050\n", "stderr": "", "exit_code": 0}
```
