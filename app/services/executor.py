import os
import json
import logging
import asyncio
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class SkillExecutor:
    """负责跨语言执行具体技能脚本，支持 Python / Shell / Node.js。"""

    async def run(
        self,
        language: str,
        entrypoint: str,
        params: Dict[str, Any],
        timeout: int = 60,
    ) -> Dict[str, Any]:
        """
        执行特定语言的脚本，将参数以 JSON 字符串形式传入，并返回执行结果。

        Args:
            language:   脚本语言，支持 "python" / "shell" / "sh" / "nodejs" / "node"。
            entrypoint: 脚本路径（相对于项目根目录或绝对路径）。
            params:     传递给脚本的参数字典，会被序列化为 JSON 字符串作为首个命令行参数。
            timeout:    执行超时秒数，超时后强制终止进程，默认 60s。

        Returns:
            {"status": "success", "result": <Any>} 或
            {"status": "error",   "message": <str>}
        """
        try:
            params_json = json.dumps(params, ensure_ascii=False)

            if language == "python":
                command = ["python", entrypoint, params_json]
            elif language in ("shell", "sh"):
                command = ["bash", entrypoint, params_json]
            elif language in ("nodejs", "node"):
                command = ["node", entrypoint, params_json]
            else:
                return {"status": "error", "message": f"Unsupported language: {language}"}

            # 工作目录设为脚本所在目录（如果文件存在的话）
            cwd = None
            if os.path.isfile(entrypoint):
                cwd = os.path.dirname(os.path.abspath(entrypoint)) or None

            logger.info(f"Running skill [{language}]: {' '.join(command)}")

            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.communicate()
                logger.error(f"Skill execution timed out after {timeout}s: {entrypoint}")
                return {"status": "error", "message": f"Execution timed out after {timeout}s"}

            if process.returncode == 0:
                output = stdout.decode("utf-8", errors="replace").strip()
                try:
                    return {"status": "success", "result": json.loads(output)}
                except json.JSONDecodeError:
                    return {"status": "success", "result": output}
            else:
                error_msg = stderr.decode("utf-8", errors="replace").strip()
                logger.error(f"Skill execution failed (rc={process.returncode}): {error_msg}")
                return {"status": "error", "message": error_msg}

        except Exception as e:
            logger.error(f"Executor unexpected error: {str(e)}")
            return {"status": "error", "message": str(e)}


skill_executor = SkillExecutor()
