"""
Run Python Snippet 技能脚本
接收 JSON 参数，在子进程中执行 Python 代码片段，返回 JSON 结果。

调用方式：
    python scripts/main.py '{"code": "print(1 + 1)", "timeout": 10}'
"""

import json
import subprocess
import sys
import textwrap


def main():
    params = {}
    if len(sys.argv) > 1:
        try:
            params = json.loads(sys.argv[1])
        except json.JSONDecodeError:
            print(json.dumps({"stdout": "", "stderr": "Invalid JSON params", "exit_code": 1}))
            return

    code: str = params.get("code", "")
    timeout: int = min(int(params.get("timeout", 10)), 60)  # 最大 60s 限制

    if not code.strip():
        print(json.dumps({"stdout": "", "stderr": "No code provided", "exit_code": 1}))
        return

    # 去除公共缩进，兼容多行代码字符串
    code = textwrap.dedent(code)

    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        result = {
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "exit_code": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        result = {
            "stdout": "",
            "stderr": f"Execution timed out after {timeout}s",
            "exit_code": 124,
        }
    except Exception as e:
        result = {
            "stdout": "",
            "stderr": str(e),
            "exit_code": 1,
        }

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
