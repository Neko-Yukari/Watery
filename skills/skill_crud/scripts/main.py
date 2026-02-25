#!/usr/bin/env python3
"""
技能脚本：技能库管理（元技能 / Meta-skill）
描述：允许 AI Agent 在运行时自主创建、更新、删除技能库中的技能，实现知识自我进化。
通过调用本地 FastAPI 后端的 Skills CRUD API 完成操作。
"""
import json
import sys
from typing import Any, Dict, Optional


_API_BASE = "http://localhost:18000/api/v1"


def create_skill(skill_data: dict) -> dict:
    """
    创建新技能。

    Required fields: id, name, description, language, entrypoint
    Optional fields: parameters_schema, script_content
    """
    try:
        import httpx
        resp = httpx.post(
            f"{_API_BASE}/skills",
            json=skill_data,
            timeout=30,
        )
        if resp.status_code == 409:
            return {"status": "skipped", "message": f"技能 '{skill_data.get('id')}' 已存在"}
        resp.raise_for_status()
        return {"status": "success", "skill_id": skill_data.get("id"), **resp.json()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def update_skill(skill_id: str, skill_data: dict) -> dict:
    """
    更新已有技能（PATCH 语义，只更新提供的字段）。

    Available fields: name, description, language, entrypoint,
                      parameters_schema, script_content, tags
    """
    try:
        import httpx
        resp = httpx.put(
            f"{_API_BASE}/skills/{skill_id}",
            json=skill_data,
            timeout=30,
        )
        if resp.status_code == 404:
            return {"status": "error", "message": f"技能 '{skill_id}' 不存在"}
        resp.raise_for_status()
        return {"status": "success", "skill_id": skill_id, **resp.json()}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def delete_skill(skill_id: str) -> dict:
    """删除指定技能。"""
    try:
        import httpx
        resp = httpx.delete(
            f"{_API_BASE}/skills/{skill_id}",
            timeout=30,
        )
        if resp.status_code == 404:
            return {"status": "error", "message": f"技能 '{skill_id}' 不存在"}
        resp.raise_for_status()
        return {"status": "success", "skill_id": skill_id, "message": "技能已删除"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def main(params: dict) -> dict:
    """
    主入口函数。

    Args:
        params: {
            "operation": "create" | "update" | "delete",   # 必填
            "skill_id": str,                                # update/delete 必填
            "skill_data": dict,                             # create/update 时提供
        }

    Returns:
        操作结果 dict
    """
    operation = params.get("operation")
    if not operation:
        return {"status": "error", "message": "必须提供 operation 参数（create/update/delete）"}

    operation = operation.lower()
    skill_id = params.get("skill_id")
    skill_data = params.get("skill_data", {})

    if operation == "create":
        if not skill_data:
            return {"status": "error", "message": "create 操作必须提供 skill_data"}
        required = {"id", "name", "description", "language", "entrypoint"}
        missing = required - set(skill_data.keys())
        if missing:
            return {"status": "error", "message": f"skill_data 缺少必要字段: {missing}"}
        return create_skill(skill_data)

    elif operation == "update":
        if not skill_id:
            return {"status": "error", "message": "update 操作必须提供 skill_id"}
        if not skill_data:
            return {"status": "error", "message": "update 操作必须提供 skill_data"}
        return update_skill(skill_id, skill_data)

    elif operation == "delete":
        if not skill_id:
            return {"status": "error", "message": "delete 操作必须提供 skill_id"}
        return delete_skill(skill_id)

    else:
        return {
            "status": "error",
            "message": f"未知操作: '{operation}'，支持 create / update / delete"
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
