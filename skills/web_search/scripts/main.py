#!/usr/bin/env python3
"""
技能脚本：联网搜索（Web Search）
描述：通过 EXA API 或 SerpAPI 搜索互联网获取实时信息。
优先使用 EXA（语义搜索），不可用时 fallback 到 SerpAPI。

依赖说明：仅使用 httpx（已在项目依赖中），无需额外安装。
"""
import json
import os
import sys
from typing import Any, Dict, List, Optional


def _progress(msg: str) -> None:
    """写心跳到 stderr，刷新 SkillExecutor 的空闲计时器。"""
    print(f"[progress] {msg}", file=sys.stderr, flush=True)


def _search_exa(query: str, num_results: int = 5, include_content: bool = True) -> dict:
    """
    使用 EXA API 进行语义搜索。

    EXA API 文档: https://docs.exa.ai
    """
    api_key = os.environ.get("EXA_API_KEY", "")
    if not api_key:
        return {"status": "unavailable", "message": "EXA_API_KEY 未配置"}

    try:
        import httpx
        _progress(f"EXA search: {query[:50]}...")

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload: dict = {
            "query": query,
            "num_results": min(num_results, 10),
            "use_autoprompt": True,
        }

        # 如果需要正文内容，使用 /search + contents
        if include_content:
            payload["contents"] = {
                "text": {"max_characters": 1500}
            }

        resp = httpx.post(
            "https://api.exa.ai/search",
            headers=headers,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("results", []):
            result_item: dict = {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "score": item.get("score"),
            }
            if include_content and item.get("text"):
                result_item["summary"] = item["text"][:1500]
            if item.get("published_date"):
                result_item["published_date"] = item["published_date"]
            results.append(result_item)

        return {
            "status": "success",
            "engine": "exa",
            "query": query,
            "results_count": len(results),
            "results": results,
        }

    except Exception as e:
        return {"status": "error", "engine": "exa", "message": str(e)}


def _search_serpapi(query: str, num_results: int = 5) -> dict:
    """
    使用 SerpAPI (Google Search) 进行关键词搜索。

    SerpAPI 文档: https://serpapi.com/search-api
    """
    api_key = os.environ.get("SERPAPI_API_KEY", "")
    if not api_key:
        return {"status": "unavailable", "message": "SERPAPI_API_KEY 未配置"}

    try:
        import httpx
        _progress(f"SerpAPI search: {query[:50]}...")

        params = {
            "q": query,
            "api_key": api_key,
            "engine": "google",
            "num": min(num_results, 10),
            "hl": "zh-cn",
        }

        resp = httpx.get(
            "https://serpapi.com/search.json",
            params=params,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("organic_results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "summary": item.get("snippet", ""),
            })

        return {
            "status": "success",
            "engine": "serpapi",
            "query": query,
            "results_count": len(results),
            "results": results[:num_results],
        }

    except Exception as e:
        return {"status": "error", "engine": "serpapi", "message": str(e)}


def main(params: dict) -> dict:
    """
    主入口函数。

    Args:
        params: {
            "query": str,              # 必填：搜索查询
            "num_results": int,        # 可选：结果数量（默认 5）
            "search_type": str,        # 可选：auto/keyword/neural
            "include_content": bool,   # 可选：是否包含正文（默认 true）
        }

    Returns:
        搜索结果 dict
    """
    query = params.get("query", "").strip()
    if not query:
        return {"status": "error", "message": "必须提供 query 搜索关键词"}

    num_results = min(int(params.get("num_results", 5)), 10)
    search_type = params.get("search_type", "auto")
    include_content = params.get("include_content", True)

    # 策略：EXA 优先 → SerpAPI fallback
    if search_type == "keyword":
        # 明确要求关键词搜索，先 SerpAPI
        result = _search_serpapi(query, num_results)
        if result["status"] == "success":
            return result
        # fallback 到 EXA
        return _search_exa(query, num_results, include_content)

    else:
        # auto 或 neural：先 EXA
        result = _search_exa(query, num_results, include_content)
        if result["status"] == "success":
            return result
        # fallback 到 SerpAPI
        serpapi_result = _search_serpapi(query, num_results)
        if serpapi_result["status"] == "success":
            return serpapi_result

        # 两者都不可用
        return {
            "status": "error",
            "message": "搜索引擎不可用。请检查 EXA_API_KEY 或 SERPAPI_API_KEY 环境变量配置。",
            "exa_detail": result.get("message", ""),
            "serpapi_detail": serpapi_result.get("message", ""),
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
