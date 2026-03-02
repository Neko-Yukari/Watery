---
id: feishu_webhook
name: 飞书机器人推送
description: |
  通过飞书自定义机器人 Webhook 发送消息到指定飞书群。
  支持纯文本、富文本（Rich Text）和交互卡片（Interactive Card）三种消息格式。
  可用于发送日报摘要、任务通知、错误告警等信息。
  需要在环境变量中配置 FEISHU_WEBHOOK_URL。
language: python
entrypoint: scripts/main.py
parameters_schema:
  type: object
  properties:
    content:
      type: string
      description: |
        消息正文内容。纯文本或 Markdown 格式。
        - msg_type=text 时：直接发送纯文本
        - msg_type=rich_text 时：作为富文本段落内容
        - msg_type=interactive 时：作为卡片正文（支持 Markdown 子集）
    title:
      type: string
      description: 消息标题（富文本和卡片模式必填）
    msg_type:
      type: string
      enum:
        - text
        - rich_text
        - interactive
      description: |
        消息类型：
        - text: 纯文本（默认）
        - rich_text: 飞书富文本（支持加粗、链接等）
        - interactive: 交互卡片（最美观，支持 Markdown 子集）
    header_color:
      type: string
      enum: [blue, wathet, turquoise, green, yellow, orange, red, carmine, violet, purple, indigo, grey]
      description: 卡片头部颜色（仅 interactive 模式有效，默认 blue）
  required:
    - content
tags:
  - notification
  - feishu
  - lark
  - webhook
  - messaging
---

# 飞书机器人推送

## 描述

通过飞书自定义机器人 Webhook 向指定群发送消息。这是实现"日报自动推送"的最后一环。

## 前置配置

1. 在飞书目标群 → 群设置 → 群机器人 → 添加自定义机器人
2. 复制 Webhook URL（格式：`https://open.feishu.cn/open-apis/bot/v2/hook/{token}`）
3. 在 `.env` 中添加：
   ```
   FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/你的token
   FEISHU_WEBHOOK_SECRET=你的签名密钥（可选）
   ```

## 使用示例

### 发送日报卡片

```json
{
  "title": "📅 Watery 日报 — 2026-03-02",
  "content": "## 概览\n- 对话数: 5\n- 消息数: 128\n\n## 关键主题\n1. 讨论了定时调度...",
  "msg_type": "interactive",
  "header_color": "blue"
}
```

### 发送简单文本

```json
{
  "content": "✅ 日报已生成，技能库已更新 3 项。"
}
```
