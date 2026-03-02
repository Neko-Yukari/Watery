#!/usr/bin/env python3
"""
技能脚本：对话历史摘要（Conversation Summary）
描述：读取指定日期的所有对话，调用 LLM 生成结构化日报摘要。

工作流程：
1. 调用 GET /api/v1/conversations 获取会话列表
2. 按 created_at 过滤出目标日期的会话
3. 逐个调用 GET /api/v1/conversations/{id} 加载完整消息
4. 拼接对话内容，调用 POST /api/v1/chat 让 LLM 生成摘要
5. 返回 Markdown 格式日报
"""
import json
import os
import sys
from datetime import datetime, date
from typing import Any, Dict, List


_API_BASE = "http://localhost:18000/api/v1"


def _progress(msg: str) -> None:
    """写心跳到 stderr，刷新 SkillExecutor 的空闲计时器。"""
    print(f"[progress] {msg}", file=sys.stderr, flush=True)


def _get_conversations() -> List[dict]:
    """获取所有对话列表。"""
    import httpx
    _progress("Fetching conversation list...")
    resp = httpx.get(f"{_API_BASE}/conversations", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # API 可能返回 list 或 {"conversations": [...]}
    if isinstance(data, list):
        return data
    return data.get("conversations", data.get("items", []))


def _get_conversation_detail(conv_id: str) -> dict:
    """获取单个对话的完整消息。"""
    import httpx
    _progress(f"Loading conversation {conv_id[:8]}...")
    resp = httpx.get(f"{_API_BASE}/conversations/{conv_id}", timeout=60)
    resp.raise_for_status()
    return resp.json()


def _filter_by_date(conversations: List[dict], target_date: str) -> List[dict]:
    """
    按日期过滤对话。target_date 格式：YYYY-MM-DD。
    比较 created_at 或 updated_at 字段的日期部分。
    """
    filtered = []
    for conv in conversations:
        # 尝试多个时间字段
        ts = conv.get("updated_at") or conv.get("created_at") or ""
        if not ts:
            continue
        # 取日期部分（兼容 ISO 格式）
        date_part = ts[:10]
        if date_part == target_date:
            filtered.append(conv)
    return filtered


def _build_conversation_text(detail: dict, include_tool_calls: bool = False) -> str:
    """将对话详情格式化为纯文本摘要素材。"""
    title = detail.get("title", "无标题")
    messages = detail.get("messages", [])
    lines = [f"### 会话: {title}"]

    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "") or ""

        if role == "system":
            continue  # 跳过 system prompt

        if role == "tool" and not include_tool_calls:
            continue

        if role == "assistant" and not content and msg.get("tool_calls"):
            if include_tool_calls:
                tc_names = []
                for tc in (msg.get("tool_calls") or []):
                    fn = tc.get("function", {})
                    tc_names.append(fn.get("name", "unknown"))
                lines.append(f"[Assistant 调用工具: {', '.join(tc_names)}]")
            continue

        # 截断过长的单条消息
        if len(content) > 2000:
            content = content[:2000] + "...(已截断)"

        prefix = {"user": "👤 用户", "assistant": "🤖 AI", "tool": "🔧 工具"}.get(role, role)
        lines.append(f"{prefix}: {content}")

    return "\n".join(lines)


def _generate_summary(conversation_texts: str, target_date: str, model: str = "ark-code-latest") -> str:
    """调用 LLM 生成日报摘要。"""
    import httpx
    _progress("Generating summary via LLM (this may take a while)...")

    prompt = f"""你是一个日报摘要助手。以下是 {target_date} 这一天用户与 AI 的所有对话记录。
请生成一份结构化的日报摘要，使用 Markdown 格式，包含以下章节：

# 📅 Watery 日报 — {target_date}

## 📊 概览
（对话数、大致主题数量）

## 🔑 关键讨论主题
（列出主要讨论了哪些话题，每个主题一句话描述）

## ✅ 已完成事项
（对话中提到的已经完成的任务或决策）

## 📝 待办 / 后续行动
（对话中提到的需要后续跟进的事项）

## 🐛 新发现的错误/经验
（如果对话中讨论了 Bug、问题排查等，提炼为错题库格式，包含标题、原因、解决方案）

## 💡 技能改进建议
（如果对话中发现了需要新增或更新的 AI 技能）

请注意：
- 只总结重要内容，忽略寒暄和重复内容
- 错误经验部分要足够具体，能直接录入错题库
- 使用中文输出

--- 以下是今日对话记录 ---

{conversation_texts}
"""

    payload = {
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "model": model,
        "temperature": 0.3,
        "max_tokens": 4096,
    }

    resp = httpx.post(
        f"{_API_BASE}/chat",
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("content", "（摘要生成失败）")


def main(params: dict) -> dict:
    """
    主入口函数。

    Args:
        params: {
            "date": "YYYY-MM-DD",       # 可选，默认今天
            "include_tool_calls": bool,  # 可选，默认 false
            "output_format": str,        # 可选，markdown / json
        }

    Returns:
        {"status": "success", "summary": "...", "stats": {...}}
    """
    # 1. 确定目标日期
    target_date = params.get("date", "")
    if not target_date:
        target_date = date.today().isoformat()  # YYYY-MM-DD

    include_tool_calls = params.get("include_tool_calls", False)
    output_format = params.get("output_format", "markdown")

    try:
        # 2. 获取并过滤对话
        all_convs = _get_conversations()
        day_convs = _filter_by_date(all_convs, target_date)
        _progress(f"Found {len(day_convs)} conversations for {target_date}")

        if not day_convs:
            return {
                "status": "success",
                "summary": f"# 📅 Watery 日报 — {target_date}\n\n今日无对话记录。",
                "stats": {"date": target_date, "conversations": 0, "messages": 0},
            }

        # 3. 加载每个对话的完整消息
        all_texts = []
        total_messages = 0
        for conv in day_convs:
            conv_id = conv.get("id", "")
            if not conv_id:
                continue
            try:
                detail = _get_conversation_detail(conv_id)
                msgs = detail.get("messages", [])
                total_messages += len(msgs)
                text = _build_conversation_text(detail, include_tool_calls)
                all_texts.append(text)
            except Exception as e:
                all_texts.append(f"### 会话 {conv_id}: 加载失败 ({e})")

        combined_text = "\n\n---\n\n".join(all_texts)

        # 4. Token 安全：如果内容过长，截断（避免超出 LLM 上下文窗口）
        max_chars = 60000  # 约 20K tokens
        if len(combined_text) > max_chars:
            combined_text = combined_text[:max_chars] + "\n\n...(更多对话已省略，总计过长)"

        # 5. 调用 LLM 生成摘要
        summary = _generate_summary(combined_text, target_date)

        result = {
            "status": "success",
            "summary": summary,
            "stats": {
                "date": target_date,
                "conversations": len(day_convs),
                "messages": total_messages,
            },
        }

        if output_format == "json":
            # JSON 模式：尝试解析摘要中的结构
            result["format"] = "json"
        else:
            result["format"] = "markdown"

        return result

    except Exception as e:
        return {"status": "error", "message": f"日报生成失败: {str(e)}"}


if __name__ == "__main__":
    params = {}
    if len(sys.argv) > 1:
        try:
            params = json.loads(sys.argv[1])
        except json.JSONDecodeError:
            params = {}
    result = main(params)
    print(json.dumps(result, ensure_ascii=False, indent=2))
