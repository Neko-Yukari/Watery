---
id: web_search
name: 联网搜索
description: |
  通过 EXA API 或 SerpAPI 搜索互联网，获取最新的网页内容和信息。
  支持搜索查询和网页内容摘要。适用于查找技术文档、配置教程、
  最新 API 变更、开源项目信息等需要实时联网获取的信息。
  优先使用 EXA（语义搜索），EXA 不可用时自动 fallback 到 SerpAPI。
language: python
entrypoint: scripts/main.py
parameters_schema:
  type: object
  properties:
    query:
      type: string
      description: 搜索查询关键词或自然语言问题
    num_results:
      type: integer
      description: 返回结果数量，默认 5，最大 10
    search_type:
      type: string
      enum:
        - auto
        - keyword
        - neural
      description: |
        搜索类型：
        - auto: 自动选择（默认）
        - keyword: 关键词精确匹配
        - neural: 语义搜索（EXA 专用）
    include_content:
      type: boolean
      description: 是否返回网页正文摘要（默认 true，会增加响应时间）
  required:
    - query
tags:
  - search
  - web
  - internet
  - research
---

# 联网搜索

## 描述

使 AI Agent 具备联网搜索能力。在对话中遇到需要实时信息的问题时
（如"飞书机器人怎么配置"、"某个库的最新版本"），自动调用此技能获取最新网络信息。

## 搜索引擎优先级

1. **EXA**（`EXA_API_KEY`）— 语义搜索，适合技术文档查找
2. **SerpAPI**（`SERPAPI_API_KEY`）— Google 搜索，覆盖面广
3. **Fallback** — 两者均不可用时返回错误提示

## 使用示例

```json
{
  "query": "飞书自定义机器人 webhook 配置教程 2026",
  "num_results": 5,
  "include_content": true
}
```
