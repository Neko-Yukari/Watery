"""
代码语义搜索技能脚本（Phase 11）。

通过调用本地 API /api/v1/code-index/search 实现，保持 Skill 进程隔离原则。
输出格式为 AI 友好的结构化文本，便于 LLM 直接理解和引用。

调用方式：
    python main.py '{"query": "处理 PDF 上传的函数", "top_k": 5}'
"""

import json
import sys
import os

try:
    import httpx
except ImportError:
    print(json.dumps({"error": "缺少依赖：httpx，请在容器中运行此技能。"}))
    sys.exit(1)

_API_BASE = os.environ.get("WATERY_API_BASE", "http://localhost:18000/api/v1")


def main() -> None:
    # 解析参数
    if len(sys.argv) < 2:
        print(json.dumps({"error": "缺少参数，用法：python main.py '{\"query\": \"...\"}'"} ))
        sys.exit(1)

    try:
        params = json.loads(sys.argv[1])
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"参数 JSON 解析失败: {e}"}))
        sys.exit(1)

    query = params.get("query", "").strip()
    if not query:
        print(json.dumps({"error": "query 参数不能为空"}))
        sys.exit(1)

    payload: dict = {
        "query": query,
        "top_k": int(params.get("top_k", 5)),
    }
    if params.get("symbol_types"):
        payload["symbol_types"] = params["symbol_types"]
    if params.get("file_pattern"):
        payload["file_pattern"] = params["file_pattern"]

    # 调用本地 API
    try:
        resp = httpx.post(
            f"{_API_BASE}/code-index/search",
            json=payload,
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.ConnectError:
        print(json.dumps({"error": "无法连接到 Watery API（localhost:18000），请确保服务正在运行。"}))
        sys.exit(1)
    except httpx.HTTPStatusError as e:
        print(json.dumps({"error": f"API 返回错误: {e.response.status_code} — {e.response.text[:200]}"}))
        sys.exit(1)
    except Exception as e:
        print(json.dumps({"error": f"请求失败: {e}"}))
        sys.exit(1)

    results = data.get("results", [])
    total_indexed = data.get("total_indexed", 0)

    if not results:
        print(f"未找到与 '{query}' 相关的代码符号（索引共 {total_indexed} 个符号）。\n"
              "建议：\n"
              "1. 尝试换一种描述，如用英文或更具体的功能描述\n"
              "2. 检查索引是否最新：POST /api/v1/code-index/update\n"
              "3. 扩大 top_k 或去掉 file_pattern/symbol_types 过滤")
        return

    # 格式化为 AI 友好的结构化文本
    lines = [
        f"在 {total_indexed} 个已索引符号中，"
        f"找到 {len(results)} 个与 '{query}' 相关的结果：",
        "",
    ]
    for i, r in enumerate(results, 1):
        sym_type_label = {
            "function": "函数",
            "method": "方法",
            "class": "类",
            "module": "模块",
            "global_var": "常量",
        }.get(r.get("symbol_type", ""), r.get("symbol_type", ""))

        docstring_preview = (r.get("docstring") or "（无文档字符串）")[:120]
        if len(r.get("docstring") or "") > 120:
            docstring_preview += "..."

        lines.append(
            f"{i}. [{sym_type_label}] {r['symbol_name']}"
        )
        lines.append(
            f"   📄 文件: {r['file_path']}  行: {r['line_start']}-{r['line_end']}"
        )
        if r.get("signature"):
            lines.append(f"   📝 签名: {r['signature']}")
        lines.append(f"   💬 说明: {docstring_preview}")
        lines.append(f"   📊 相关度: {r.get('relevance_score', 0):.2f}")
        lines.append("")

    lines.append(
        "提示：使用 file_path + line_start~line_end 可精准读取上述代码片段，"
        "避免读取整个文件以节省 Token。"
    )

    print("\n".join(lines))


if __name__ == "__main__":
    main()
