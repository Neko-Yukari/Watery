---
id: conversation_summary
name: 对话历史摘要
description: |
  回溯指定日期（默认今天）的所有对话会话，读取完整消息内容，
  调用 LLM 生成结构化的对话摘要日报。
  支持提取关键讨论主题、待办事项、新发现的错误经验和技能改进建议。
  输出为 Markdown 格式的日报文本，可直接用于飞书推送或存档。
language: python
entrypoint: scripts/main.py
parameters_schema:
  type: object
  properties:
    date:
      type: string
      description: 目标日期，格式 YYYY-MM-DD（默认今天）
    include_tool_calls:
      type: boolean
      description: 是否在摘要中包含 Tool Calling 记录（默认 false）
    output_format:
      type: string
      enum:
        - markdown
        - json
      description: 输出格式（默认 markdown）
  required: []
tags:
  - summary
  - conversation
  - daily-report
  - self-amendment
---

# 对话历史摘要

## 描述

读取指定日期的所有对话记录，通过 LLM 生成结构化日报摘要。
这是实现"每日自动总结"流程的核心技能。

## 输出结构（Markdown 模式）

```markdown
# 📅 Watery 日报 — 2026-03-02

## 📊 概览
- 今日对话数: 5
- 总消息数: 128
- 工具调用次数: 12

## 🔑 关键主题
1. 讨论了 AI 平台定时调度能力缺口
2. 分析了 Tool Calling 链路现状
3. ...

## ✅ 已完成事项
- 完成了 4 个新技能的设计...

## 📝 待办 / 后续行动
- 配置飞书机器人 Webhook
- ...

## 🐛 新发现的错误/经验（建议录入错题库）
- ...

## 💡 技能改进建议
- 建议更新 xxx 技能...
```
