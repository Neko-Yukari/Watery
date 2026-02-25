#!/usr/bin/env python3
"""
技能脚本：PDF 转技能包
描述：将一个 PDF 文档自动转化为一组可调用的 AI 技能，通过调用系统 API 实现。
通过 HTTP 调用本地 FastAPI 后端的 PDF 处理流水线端点完成全流程。
"""
import json
import sys
import time
from typing import Any, Dict, Optional


_API_BASE = "http://localhost:18000/api/v1"
_POLL_INTERVAL = 5   # 状态轮询间隔（秒）
_MAX_WAIT = 600      # 最长等待时间（秒）


def upload_and_process(
    pdf_path: str,
    skill_prefix: str = "",
    max_tokens_per_chunk: int = 6000,
    output_dir: str = "/app/skills",
) -> Dict[str, Any]:
    """
    上传 PDF 并触发 to-skills 流水线，轮询等待完成。

    Args:
        pdf_path:             PDF 文件绝对路径
        skill_prefix:         技能 ID 前缀
        max_tokens_per_chunk: 分块 Token 上限
        output_dir:           技能输出目录

    Returns:
        PDFDocument 状态记录
    """
    try:
        import httpx
    except ImportError:
        return {"error": "缺少依赖: httpx. 请在容器内安装 httpx"}

    import os
    if not os.path.exists(pdf_path):
        return {"error": f"文件不存在: {pdf_path}"}

    # Step 1: 上传 PDF
    try:
        with open(pdf_path, "rb") as f:
            upload_resp = httpx.post(
                f"{_API_BASE}/pdf/upload",
                files={"file": (os.path.basename(pdf_path), f, "application/pdf")},
                timeout=60,
            )
        upload_resp.raise_for_status()
        upload_data = upload_resp.json()
        doc_id = upload_data["doc_id"]
    except Exception as e:
        return {"error": f"上传 PDF 失败: {e}"}

    # Step 2: 触发流水线
    try:
        trigger_resp = httpx.post(
            f"{_API_BASE}/pdf/to-skills",
            json={
                "doc_id": doc_id,
                "skill_prefix": skill_prefix,
                "max_tokens_per_chunk": max_tokens_per_chunk,
                "output_dir": output_dir,
            },
            timeout=30,
        )
        trigger_resp.raise_for_status()
    except Exception as e:
        return {"error": f"触发流水线失败: {e}"}

    # Step 3: 轮询状态
    waited = 0
    while waited < _MAX_WAIT:
        time.sleep(_POLL_INTERVAL)
        waited += _POLL_INTERVAL
        try:
            status_resp = httpx.get(
                f"{_API_BASE}/pdf/status/{doc_id}",
                timeout=15,
            )
            status_resp.raise_for_status()
            status_data = status_resp.json()
            current_status = status_data.get("status", "pending")

            if current_status == "completed":
                return status_data
            elif current_status == "failed":
                return {
                    "error": f"流水线失败: {status_data.get('error_msg', '未知错误')}",
                    "doc_id": doc_id,
                }
        except Exception as e:
            pass  # 忽略临时网络错误，继续轮询

    return {
        "error": f"等待超时（{_MAX_WAIT}s），doc_id={doc_id}",
        "doc_id": doc_id,
    }


def main(params: dict) -> dict:
    """
    主入口函数。

    Args:
        params: {
            "pdf_path": str,                # 必填
            "skill_prefix": str,            # 可选，默认 ""
            "max_tokens_per_chunk": int,    # 可选，默认 6000
            "output_dir": str,              # 可选，默认 "/app/skills"
        }

    Returns:
        流水线执行结果
    """
    pdf_path = params.get("pdf_path")
    if not pdf_path:
        return {"error": "必须提供 pdf_path 参数"}

    return upload_and_process(
        pdf_path=pdf_path,
        skill_prefix=params.get("skill_prefix", ""),
        max_tokens_per_chunk=params.get("max_tokens_per_chunk", 6000),
        output_dir=params.get("output_dir", "/app/skills"),
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
