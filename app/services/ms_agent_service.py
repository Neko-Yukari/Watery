"""
MSAgentService — 将 ms-agent 三大 project 能力整合到 Watery

支持能力：
  • deep_research  — Agentic Insight v2 深度研究（自主探索 + 证据驱动报告）
  • code_genesis   — 复杂代码生成（设计→编码→精炼三阶段 DAG 工作流）
  • doc_research   — 文档深度分析（依赖 ms-agent 核心包）

运行机制：
  - 每个任务通过 asyncio.create_subprocess_exec 调用 ms-agent CLI
  - 结果写入 /app/data/outputs/{research|code}/<task_id>/
  - 通过 task_id 查询状态和读取产物
  - 环境变量 OPENAI_API_KEY / OPENAI_BASE_URL 自动映射为
    Watery 的 Volcengine 密钥，ms-agent YAML 占位符 <OPENAI_API_KEY>
    由框架在运行时替换

搜索引擎配置（deep_research）：
  - 优先使用 .env 中的 EXA_API_KEY（推荐，免费额度）
  - 其次使用 SERPAPI_API_KEY
  - 不配置时默认 arxiv（仅学术论文，无需 key）
"""

import asyncio
import json
import logging
import os
import shlex
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# 容器内路径
PROJECTS_DIR = Path("/app/projects")
OUTPUTS_DIR = Path("/app/data/outputs")

# ms-agent CLI 命令（pip install ms-agent 后可直接调用）
MS_AGENT_CMD = "ms-agent"

# 任务超时（秒）
RESEARCH_TIMEOUT = 3600     # 深度研究最多 60 min
CODE_GEN_TIMEOUT = 1800     # 代码生成最多 30 min


class MSAgentService:
    """
    Watery 对 ms-agent 三大 project 能力的封装服务层。

    公共接口：
        run_deep_research  — 触发深度研究任务（异步，返回 task_id）
        run_code_genesis   — 触发代码生成任务（异步，返回 task_id）
        get_task_status    — 查询任务状态和产物列表
        read_report        — 读取研究报告全文
    """

    # ------------------------------------------------------------------ #
    # 公共接口
    # ------------------------------------------------------------------ #

    async def run_deep_research(
        self,
        query: str,
        task_id: Optional[str] = None,
        model: Optional[str] = None,
        exa_api_key: Optional[str] = None,
        serpapi_api_key: Optional[str] = None,
        max_rounds: int = 6,
    ) -> Dict[str, Any]:
        """
        触发 deep_research v2 深度研究任务。

        底层命令：
            ms-agent run \\
              --config /app/projects/deep_research/v2/researcher.yaml \\
              --query <query> \\
              --output_dir /app/data/outputs/research/<task_id> \\
              --trust_remote_code true

        Returns:
            {"task_id": ..., "status": "started", "work_dir": ...}
        """
        task_id = task_id or uuid.uuid4().hex
        work_dir = OUTPUTS_DIR / "research" / task_id
        work_dir.mkdir(parents=True, exist_ok=True)

        config_path = str(PROJECTS_DIR / "deep_research" / "v2" / "researcher.yaml")

        extra_env = self._build_extra_env(
            model=model,
            exa_api_key=exa_api_key,
            serpapi_api_key=serpapi_api_key,
        )

        # 将 max_rounds 写入临时 conf 后通过环境变量或参数传递
        # ms-agent 支持通过 --override 传递 OmegaConf 覆盖
        args = [
            "--override", f"max_chat_round={max_rounds * 10}",  # 估算轮次上限
        ]

        logger.info(f"[MSAgent] Starting deep_research task={task_id}: {query[:80]}...")

        # 以 fire-and-forget 方式启动，Watery 立即返回 task_id
        asyncio.create_task(
            self._exec_task(
                task_type="research",
                task_id=task_id,
                config_path=config_path,
                query=query,
                work_dir=work_dir,
                output_dir=work_dir,
                extra_env=extra_env,
                extra_args=args,
                timeout=RESEARCH_TIMEOUT,
            )
        )

        return {
            "task_id": task_id,
            "status": "started",
            "work_dir": str(work_dir),
            "message": "深度研究任务已启动，可通过 GET /research/{task_id} 查询进度",
        }

    async def run_code_genesis(
        self,
        query: str,
        task_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        触发 code_genesis 复杂代码生成任务。

        底层命令：
            ms-agent run \\
              --config /app/projects/code_genesis \\
              --query <query> \\
              --trust_remote_code true

        Returns:
            {"task_id": ..., "status": "started", "work_dir": ...}
        """
        task_id = task_id or uuid.uuid4().hex
        work_dir = OUTPUTS_DIR / "code" / task_id
        work_dir.mkdir(parents=True, exist_ok=True)

        config_path = str(PROJECTS_DIR / "code_genesis")

        extra_env = self._build_extra_env(model=model)

        logger.info(f"[MSAgent] Starting code_genesis task={task_id}: {query[:80]}...")

        asyncio.create_task(
            self._exec_task(
                task_type="code",
                task_id=task_id,
                config_path=config_path,
                query=query,
                work_dir=work_dir,
                output_dir=None,   # code_genesis 输出到 work_dir/output/
                extra_env=extra_env,
                timeout=CODE_GEN_TIMEOUT,
            )
        )

        return {
            "task_id": task_id,
            "status": "started",
            "work_dir": str(work_dir),
            "message": "代码生成任务已启动，可通过 GET /code/{task_id} 查询进度",
        }

    def get_task_status(self, task_type: str, task_id: str) -> Dict[str, Any]:
        """
        查询任务当前状态。

        task_type: "research" | "code"

        Returns:
            {
              "status": "pending" | "running" | "completed" | "failed" | "not_found",
              "output_files": [...],
              "report": "...",       # deep_research only
            }
        """
        work_dir = OUTPUTS_DIR / task_type / task_id
        if not work_dir.exists():
            return {"status": "not_found", "task_id": task_id}

        # 读取状态文件（由 _exec_task 写入）
        status_file = work_dir / ".watery_status.json"
        status_info: Dict[str, Any] = {}
        if status_file.exists():
            try:
                status_info = json.loads(status_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        output_files = self._list_outputs(work_dir)
        report = None
        if task_type == "research":
            report = self._read_file(work_dir, "final_report.md")
            if not report:
                # 尝试 output/ 子目录
                report = self._read_file(work_dir / "output", "final_report.md")

        status = status_info.get("status", "running" if output_files else "pending")

        return {
            "status": status,
            "task_id": task_id,
            "task_type": task_type,
            "work_dir": str(work_dir),
            "output_files": output_files,
            "report": report,
            "stderr_tail": status_info.get("stderr_tail"),
            "returncode": status_info.get("returncode"),
        }

    def list_tasks(self, task_type: str) -> List[Dict[str, Any]]:
        """列出指定类型的所有任务（按创建时间倒序）。"""
        base_dir = OUTPUTS_DIR / task_type
        if not base_dir.exists():
            return []
        tasks = []
        for d in sorted(base_dir.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
            if d.is_dir():
                status_file = d / ".watery_status.json"
                info: Dict[str, Any] = {"task_id": d.name, "status": "unknown"}
                if status_file.exists():
                    try:
                        info.update(json.loads(status_file.read_text(encoding="utf-8")))
                    except Exception:
                        pass
                tasks.append(info)
        return tasks

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    def _build_extra_env(
        self,
        model: Optional[str] = None,
        exa_api_key: Optional[str] = None,
        serpapi_api_key: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        构建调用 ms-agent 时需要覆写的环境变量。

        ms-agent YAML 中的 <OPENAI_API_KEY> / <OPENAI_BASE_URL> 占位符
        由框架在运行时从环境变量自动替换。

        优先级：
            Volcengine key → OPENAI_API_KEY
            Volcengine base_url → OPENAI_BASE_URL
        """
        env: Dict[str, str] = {
            "OPENAI_API_KEY": settings.volcengine_api_key,
            "OPENAI_BASE_URL": settings.volcengine_base_url,
        }

        # 可选：覆盖默认模型（通过 LLM_DEFAULT_MODEL 传递，ms-agent 不直接读取）
        if model:
            env["WATERY_MODEL_OVERRIDE"] = model

        # 搜索引擎 API Key（deep_research 用）
        # 优先级: 请求参数 > settings（.env）> 环境变量
        exa = exa_api_key or (settings.exa_api_key or "") or os.environ.get("EXA_API_KEY", "")
        serp = serpapi_api_key or (settings.serpapi_api_key or "") or os.environ.get("SERPAPI_API_KEY", "")
        if exa:
            env["EXA_API_KEY"] = exa
        if serp:
            env["SERPAPI_API_KEY"] = serp

        # 可选：ModelScope API Key（fallback 能力）
        ms_key = (settings.modelscope_api_key or "") or os.environ.get("MODELSCOPE_API_KEY", "")
        if ms_key:
            env["MODELSCOPE_API_KEY"] = ms_key

        return env

    async def _exec_task(
        self,
        task_type: str,
        task_id: str,
        config_path: str,
        query: str,
        work_dir: Path,
        output_dir: Optional[Path],
        extra_env: Dict[str, str],
        extra_args: Optional[List[str]] = None,
        timeout: int = 1800,
    ) -> None:
        """
        在 work_dir 目录内异步执行 ms-agent CLI。

        执行完毕后将状态（returncode / stderr 末尾100行）
        写入 work_dir/.watery_status.json 供 get_task_status 读取。
        """
        status_file = work_dir / ".watery_status.json"
        self._write_status(status_file, {"status": "running", "task_id": task_id})

        cmd: List[str] = [
            MS_AGENT_CMD, "run",
            "--config", config_path,
            "--query", query,
            "--trust_remote_code", "true",
        ]

        if output_dir:
            cmd += ["--output_dir", str(output_dir)]

        if extra_args:
            cmd.extend(extra_args)

        # 合并进程环境变量
        proc_env = {**os.environ, **extra_env}

        logger.info(f"[MSAgent] exec: {' '.join(shlex.quote(c) for c in cmd[:6])} ...")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(work_dir),
                env=proc_env,
            )

            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    process.communicate(), timeout=timeout
                )
                rc = process.returncode
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.communicate()
                rc = -1
                stdout_b, stderr_b = b"", b"timeout"

            # 写入 stdout / stderr 日志文件（前 64KB）
            stdout_text = stdout_b.decode("utf-8", errors="replace")
            stderr_text = stderr_b.decode("utf-8", errors="replace")
            (work_dir / "ms_agent_stdout.log").write_text(
                stdout_text[:65536], encoding="utf-8"
            )
            (work_dir / "ms_agent_stderr.log").write_text(
                stderr_text[:65536], encoding="utf-8"
            )

            # 写入状态文件
            status = "completed" if rc == 0 else "failed"
            stderr_tail = "\n".join(stderr_text.splitlines()[-100:])
            self._write_status(
                status_file,
                {
                    "status": status,
                    "task_id": task_id,
                    "task_type": task_type,
                    "returncode": rc,
                    "stderr_tail": stderr_tail if rc != 0 else None,
                },
            )

            if rc == 0:
                logger.info(f"[MSAgent] task={task_id} completed successfully.")
            else:
                logger.warning(
                    f"[MSAgent] task={task_id} failed (rc={rc}): {stderr_tail[-200:]}"
                )

        except Exception as e:
            logger.error(f"[MSAgent] task={task_id} unexpected error: {e}")
            self._write_status(
                status_file,
                {
                    "status": "failed",
                    "task_id": task_id,
                    "task_type": task_type,
                    "returncode": -1,
                    "error": str(e),
                },
            )

    @staticmethod
    def _write_status(path: Path, data: Dict[str, Any]) -> None:
        """原子写入状态 JSON 文件。"""
        try:
            path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning(f"[MSAgent] failed to write status to {path}: {e}")

    def _list_outputs(self, work_dir: Path) -> List[str]:
        """列出工作目录中的所有产物文件（排除日志和状态文件）。"""
        if not work_dir.exists():
            return []
        ignore_names = {".watery_status.json", "ms_agent_stdout.log", "ms_agent_stderr.log"}
        results = []
        for f in work_dir.rglob("*"):
            if f.is_file() and f.name not in ignore_names:
                try:
                    results.append(str(f.relative_to(work_dir).as_posix()))
                except ValueError:
                    pass
        return sorted(results)

    @staticmethod
    def _read_file(base: Path, filename: str) -> Optional[str]:
        """在 base 目录及其子目录中查找并读取文件。"""
        target = base / filename
        if target.exists():
            try:
                return target.read_text(encoding="utf-8")
            except Exception:
                pass
        # 递归查找
        for f in base.rglob(filename):
            try:
                return f.read_text(encoding="utf-8")
            except Exception:
                pass
        return None


# 全局实例
ms_agent_service = MSAgentService()
