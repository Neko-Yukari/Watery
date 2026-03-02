#!/usr/bin/env python3
"""
技能脚本：飞书机器人推送（Feishu Webhook）
描述：通过飞书自定义机器人 Webhook 发送消息到指定飞书群。
支持纯文本、富文本、交互卡片三种格式。

依赖说明：仅使用 httpx + hmac（标准库），无需额外安装。
"""
import base64
import hashlib
import hmac
import json
import os
import sys
import time
from typing import Any, Dict, Optional


def _progress(msg: str) -> None:
    """写心跳到 stderr，刷新 SkillExecutor 的空闲计时器。"""
    print(f"[progress] {msg}", file=sys.stderr, flush=True)


def _gen_sign(secret: str, timestamp: str) -> str:
    """
    飞书签名校验算法。

    签名公式：
    string_to_sign = timestamp + "\n" + secret
    sign = base64(hmac_sha256(string_to_sign, ""))
    """
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def _build_text_body(content: str) -> dict:
    """构建纯文本消息体。"""
    return {
        "msg_type": "text",
        "content": {
            "text": content,
        },
    }


def _build_rich_text_body(title: str, content: str) -> dict:
    """
    构建富文本消息体。
    将 content 按换行拆分为段落。
    """
    # 将内容按换行拆分为文本段落
    paragraphs = []
    for line in content.split("\n"):
        if line.strip():
            paragraphs.append([{"tag": "text", "text": line}])

    return {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": title or "通知",
                    "content": paragraphs,
                }
            }
        },
    }


def _build_interactive_body(title: str, content: str, header_color: str = "blue") -> dict:
    """
    构建交互卡片消息体。
    content 支持飞书卡片 Markdown 子集（加粗、列表、链接等）。
    """
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": title or "通知",
                },
                "template": header_color,
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": content,
                    },
                }
            ],
        },
    }


def send_message(
    content: str,
    title: str = "",
    msg_type: str = "text",
    header_color: str = "blue",
    webhook_url: str = "",
    secret: str = "",
) -> dict:
    """
    发送消息到飞书。

    Args:
        content: 消息正文
        title: 标题（富文本和卡片模式使用）
        msg_type: text / rich_text / interactive
        header_color: 卡片颜色
        webhook_url: Webhook URL（不传则从环境变量读取）
        secret: 签名密钥（不传则从环境变量读取）

    Returns:
        {"status": "success"} 或 {"status": "error", "message": "..."}
    """
    url = webhook_url or os.environ.get("FEISHU_WEBHOOK_URL", "")
    if not url:
        return {
            "status": "error",
            "message": "飞书 Webhook URL 未配置。请在 .env 中设置 FEISHU_WEBHOOK_URL。"
                       "\n\n配置步骤：\n"
                       "1. 打开飞书目标群 → 群设置 → 群机器人 → 添加机器人 → 自定义机器人\n"
                       "2. 复制 Webhook URL\n"
                       "3. 在 .env 中添加: FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/你的token",
        }

    # 构建消息体
    if msg_type == "rich_text":
        body = _build_rich_text_body(title, content)
    elif msg_type == "interactive":
        body = _build_interactive_body(title, content, header_color)
    else:
        body = _build_text_body(content)

    # 签名校验（如果配置了 secret）
    sign_secret = secret or os.environ.get("FEISHU_WEBHOOK_SECRET", "")
    if sign_secret:
        timestamp = str(int(time.time()))
        sign = _gen_sign(sign_secret, timestamp)
        body["timestamp"] = timestamp
        body["sign"] = sign

    try:
        import httpx
        _progress(f"Sending {msg_type} message to Feishu...")
        resp = httpx.post(
            url,
            json=body,
            timeout=30,
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        result = resp.json()

        if result.get("code") == 0 or result.get("StatusCode") == 0:
            return {"status": "success", "message": "消息已发送到飞书"}
        else:
            return {
                "status": "error",
                "message": f"飞书 API 返回错误: {result.get('msg', result)}",
                "detail": result,
            }

    except Exception as e:
        return {"status": "error", "message": f"发送失败: {str(e)}"}


def main(params: dict) -> dict:
    """
    主入口函数。

    Args:
        params: {
            "content": str,         # 必填：消息正文
            "title": str,           # 可选：标题
            "msg_type": str,        # 可选：text/rich_text/interactive
            "header_color": str,    # 可选：卡片颜色
        }

    Returns:
        发送结果 dict
    """
    content = params.get("content", "").strip()
    if not content:
        return {"status": "error", "message": "必须提供 content 消息内容"}

    return send_message(
        content=content,
        title=params.get("title", ""),
        msg_type=params.get("msg_type", "text"),
        header_color=params.get("header_color", "blue"),
    )


if __name__ == "__main__":
    params = {}
    if len(sys.argv) > 1:
        try:
            params = json.loads(sys.argv[1])
        except json.JSONDecodeError:
            params = {}
    result = main(params)
    print(json.dumps(result, ensure_ascii=False, indent=2))
