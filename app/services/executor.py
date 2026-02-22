import subprocess
import os
import json
import logging
import asyncio
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class SkillExecutor:
    """负责跨语言执行具体技能脚本"""
    
    async def run(self, language: str, entrypoint: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        执行特定语言的脚本，并将参数传递给它。
        """
        try:
            # 准备参数 JSON 传给子进程
            params_json = json.dumps(params)
            
            # 根据语言构造执行命令
            if language == "python":
                command = ["python", entrypoint, params_json]
            elif language == "shell" or language == "sh":
                command = ["bash", entrypoint, params_json]
            elif language == "nodejs" or language == "node":
                command = ["node", entrypoint, params_json]
            else:
                return {"status": "error", "message": f"Unsupported language: {language}"}
            
            logger.info(f"Running skill: {' '.join(command)}")
            
            # 使用异步 subprocess 执行
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate()
            
            if process.returncode == 0:
                output = stdout.decode().strip()
                try:
                    # 尝试解析 JSON 结果
                    return {"status": "success", "result": json.loads(output)}
                except:
                    # 如果不是 JSON，则返回原始字符串
                    return {"status": "success", "result": output}
            else:
                error_msg = stderr.decode().strip()
                logger.error(f"Skill execution failed: {error_msg}")
                return {"status": "error", "message": error_msg}
                
        except Exception as e:
            logger.error(f"Executor error: {str(e)}")
            return {"status": "error", "message": str(e)}

skill_executor = SkillExecutor()
