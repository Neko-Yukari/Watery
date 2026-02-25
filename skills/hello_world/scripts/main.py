"""
Hello World 技能脚本
接收 JSON 参数（第一个命令行参数），输出 JSON 结果到 stdout。

调用方式：
    python scripts/main.py '{"name": "Watery"}'
"""

import json
import sys


def main():
    params = {}
    if len(sys.argv) > 1:
        try:
            params = json.loads(sys.argv[1])
        except json.JSONDecodeError:
            params = {}

    name = params.get("name", "World")
    result = {"message": f"Hello, {name}!"}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
