import os
import json
import logging
import asyncio
import time as _time
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


class SkillExecutor:
    """
    负责跨语言执行具体技能脚本，支持 Python / Shell / Node.js。

    超时策略（Phase 10 升级 — 主动空闲检测）：
    ┌───────────────────────────────────────────────────────────────┐
    │ idle_timeout（默认 30s）                                      │
    │   进程 stdout/stderr 持续无输出超过此时间 → 判定"卡死" → kill  │
    │   长耗时技能可通过 stderr 写入心跳刷新计时器                   │
    │                                                               │
    │ max_timeout（默认 300s）                                      │
    │   绝对安全上限，无论是否有输出，超过即 kill                    │
    └───────────────────────────────────────────────────────────────┘
    技能脚本心跳协议：向 stderr 写入任意内容（推荐 [progress] 前缀）。
    """

    async def run(
        self,
        language: str,
        entrypoint: str,
        params: Dict[str, Any],
        timeout: int = 300,
        idle_timeout: int = 30,
    ) -> Dict[str, Any]:
        """
        执行特定语言的脚本，将参数以 JSON 字符串形式传入，并返回执行结果。

        Args:
            language:     脚本语言，支持 "python" / "shell" / "sh" / "nodejs" / "node"。
            entrypoint:   脚本路径（相对于项目根目录或绝对路径）。
            params:       传递给脚本的参数字典，会被序列化为 JSON 字符串作为首个命令行参数。
            timeout:      绝对最大超时秒数（安全网），默认 300s。
            idle_timeout: 空闲超时秒数（无输出即判死），默认 30s。

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

            stdout_bytes, stderr_bytes, reason = await self._monitored_communicate(
                process=process,
                entrypoint=entrypoint,
                max_timeout=timeout,
                idle_timeout=idle_timeout,
            )

            if reason == "idle_timeout":
                stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
                detail = f" (last stderr: {stderr_text[-200:]})" if stderr_text else ""
                return {
                    "status": "error",
                    "message": f"Skill idle timeout: no output for {idle_timeout}s{detail}",
                }
            if reason == "max_timeout":
                return {
                    "status": "error",
                    "message": f"Skill absolute timeout: exceeded {timeout}s",
                }

            if process.returncode == 0:
                output = stdout_bytes.decode("utf-8", errors="replace").strip()
                try:
                    return {"status": "success", "result": json.loads(output)}
                except json.JSONDecodeError:
                    return {"status": "success", "result": output}
            else:
                error_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
                logger.error(f"Skill execution failed (rc={process.returncode}): {error_msg}")
                return {"status": "error", "message": error_msg}

        except Exception as e:
            logger.error(f"Executor unexpected error: {str(e)}")
            return {"status": "error", "message": str(e)}

    # ------------------------------------------------------------------ #
    # 核心：带空闲检测的子进程通信
    # ------------------------------------------------------------------ #

    async def _monitored_communicate(
        self,
        process: asyncio.subprocess.Process,
        entrypoint: str,
        max_timeout: int,
        idle_timeout: int,
    ) -> Tuple[bytes, bytes, str]:
        """
        替代 process.communicate()，实现双层超时检测。

        工作原理：
        1. 两个 reader 协程分别异步读取 stdout / stderr
        2. 每次读到数据就刷新 last_activity 时间戳
        3. watchdog 协程每秒检查一次：
           - 空闲超时（last_activity 距今 > idle_timeout）→ kill
           - 绝对超时（启动距今 > max_timeout）→ kill
        4. 进程正常退出 → watchdog 自动结束

        Args:
            process:     asyncio 子进程对象
            entrypoint:  脚本路径（仅用于日志）
            max_timeout: 绝对超时秒数
            idle_timeout: 空闲超时秒数

        Returns:
            (stdout_bytes, stderr_bytes, reason)
            reason: "completed" | "idle_timeout" | "max_timeout"
        """
        start = _time.monotonic()
        last_activity = _time.monotonic()

        stdout_parts: List[bytes] = []
        stderr_parts: List[bytes] = []

        async def _drain(stream: asyncio.StreamReader, parts: List[bytes]) -> None:
            """持续读取流数据，每次读到内容就刷新活跃时间。"""
            nonlocal last_activity
            try:
                while True:
                    chunk = await stream.read(8192)
                    if not chunk:
                        break  # EOF — 流关闭
                    parts.append(chunk)
                    last_activity = _time.monotonic()
            except (asyncio.CancelledError, Exception):
                pass

        async def _watchdog() -> str:
            """每秒检查一次超时条件，返回终止原因。"""
            nonlocal last_activity
            while process.returncode is None:
                await asyncio.sleep(1)
                now = _time.monotonic()

                if now - last_activity > idle_timeout:
                    logger.warning(
                        f"Skill idle timeout ({idle_timeout}s no output): {entrypoint}"
                    )
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    return "idle_timeout"

                if now - start > max_timeout:
                    logger.warning(
                        f"Skill max timeout ({max_timeout}s): {entrypoint}"
                    )
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    return "max_timeout"

            return "completed"

        # 启动三个并发协程
        task_out = asyncio.create_task(_drain(process.stdout, stdout_parts))
        task_err = asyncio.create_task(_drain(process.stderr, stderr_parts))
        task_dog = asyncio.create_task(_watchdog())

        # 等待 watchdog 返回（它会在进程退出或超时后结束）
        reason = await task_dog

        # 给 reader 一点时间收集残留数据
        await asyncio.sleep(0.1)
        for t in (task_out, task_err):
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

        # 确保进程完全终止
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass

        return b"".join(stdout_parts), b"".join(stderr_parts), reason


skill_executor = SkillExecutor()
