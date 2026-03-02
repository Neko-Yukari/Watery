#!/usr/bin/env python3
"""
技能脚本：错题库管理（Error Ledger CRUD）
描述：允许 AI Agent 在对话中自主创建、查询、删除错题库条目。
通过调用本地 FastAPI 后端的 Error Entries API 完成操作。
"""
import json
import sys
from typing import Any, Dict


_API_BASE = "http://localhost:18000/api/v1"


def create_entry(entry_data: dict) -> dict:
    """创建新错题条目。"""
    try:
        import httpx
        resp = httpx.post(
            f"{_API_BASE}/errors/entries",
            json=entry_data,
            timeout=30,
        )
        resp.raise_for_status()
        return {"status": "success", **resp.json()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def list_entries(tags: str = "", severity: str = "", limit: int = 20) -> dict:
    """列出错题条目（支持标签/严重程度筛选）。"""
    try:
        import httpx
        params: dict = {"limit": limit}
        if tags:
            params["tags"] = tags
        if severity:
            params["severity"] = severity
        resp = httpx.get(
            f"{_API_BASE}/errors/entries",
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        return {"status": "success", **resp.json()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def search_entries(keyword: str, limit: int = 20) -> dict:
    """按关键词搜索错题。"""
    try:
        import httpx
        resp = httpx.get(
            f"{_API_BASE}/errors/entries",
            params={"search": keyword, "limit": limit},
            timeout=30,
        )
        resp.raise_for_status()
        return {"status": "success", **resp.json()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def get_entry(entry_id: str) -> dict:
    """获取单条错题详情。"""
    try:
        import httpx
        resp = httpx.get(
            f"{_API_BASE}/errors/entries/{entry_id}",
            timeout=30,
        )
        if resp.status_code == 404:
            return {"status": "error", "message": f"错题 '{entry_id}' 不存在"}
        resp.raise_for_status()
        return {"status": "success", **resp.json()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def delete_entry(entry_id: str) -> dict:
    """删除指定错题。"""
    try:
        import httpx
        resp = httpx.delete(
            f"{_API_BASE}/errors/entries/{entry_id}",
            timeout=30,
        )
        if resp.status_code == 404:
            return {"status": "error", "message": f"错题 '{entry_id}' 不存在"}
        resp.raise_for_status()
        return {"status": "success", "entry_id": entry_id, "message": "错题已删除"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def main(params: dict) -> dict:
    """
    主入口函数。

    Args:
        params: {
            "operation": "create" | "list" | "get" | "delete" | "search",
            "entry_id": str,         # get/delete 必填
            "entry_data": dict,      # create 时必填
            "tags": str,             # list 时可选，逗号分隔
            "severity": str,         # list 时可选
            "search": str,           # search 操作时必填
        }

    Returns:
        操作结果 dict
    """
    operation = params.get("operation")
    if not operation:
        return {"status": "error", "message": "必须提供 operation 参数（create/list/get/delete/search）"}

    operation = operation.lower()
    entry_id = params.get("entry_id")
    entry_data = params.get("entry_data", {})

    if operation == "create":
        if not entry_data:
            return {"status": "error", "message": "create 操作必须提供 entry_data"}
        required = {"title", "context", "correction"}
        missing = required - set(entry_data.keys())
        if missing:
            return {"status": "error", "message": f"entry_data 缺少必要字段: {missing}"}
        return create_entry(entry_data)

    elif operation == "list":
        return list_entries(
            tags=params.get("tags", ""),
            severity=params.get("severity", ""),
        )

    elif operation == "search":
        keyword = params.get("search", "")
        if not keyword:
            return {"status": "error", "message": "search 操作必须提供 search 关键词"}
        return search_entries(keyword)

    elif operation == "get":
        if not entry_id:
            return {"status": "error", "message": "get 操作必须提供 entry_id"}
        return get_entry(entry_id)

    elif operation == "delete":
        if not entry_id:
            return {"status": "error", "message": "delete 操作必须提供 entry_id"}
        return delete_entry(entry_id)

    else:
        return {
            "status": "error",
            "message": f"未知操作: '{operation}'，支持 create / list / get / delete / search",
        }


if __name__ == "__main__":
    params = {}
    if len(sys.argv) > 1:
        try:
            params = json.loads(sys.argv[1])
        except json.JSONDecodeError:
            params = {}
    result = main(params)
    print(json.dumps(result, ensure_ascii=False, indent=2))
