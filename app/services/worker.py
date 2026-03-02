import asyncio
import json
import logging
import re
import uuid
from typing import Any, Dict, Optional

from app.services.orchestrator import orchestrator
from app.services.executor import skill_executor
from app.services.model_router import model_router
from app.models.database import ErrorEntry, Task, SkillMetadata
from app.models.schemas import Message

logger = logging.getLogger(__name__)

# 知识缺口检测阈值：ChromaDB L2 距离 > 此值则判定为知识缺口
KNOWLEDGE_GAP_THRESHOLD = 1.5

# 自修正研究轮询间隔（秒）
AMENDMENT_POLL_INTERVAL = 60

# 报告蒸馏最大字符数（避免 prompt 过长）
REPORT_MAX_CHARS = 8000

# ---- 模块级共享状态（所有 WorkerAgent 实例共用）----
# research_task_id -> original_query
_pending_amendments: Dict[str, str] = {}


def _increment_hit_count(entry_id: str) -> None:
    """原子性递增 ErrorEntry 的 hit_count（Phase 8）。"""
    try:
        from sqlmodel import Session
        from app.core.db import engine
        with Session(engine) as session:
            entry = session.get(ErrorEntry, entry_id)
            if entry:
                entry.hit_count = (entry.hit_count or 0) + 1
                session.add(entry)
                session.commit()
    except Exception:
        pass


class WorkerAgent:
    """
    Worker Agent 执行引擎。

    执行流程（每次认领到任务后）：
    1. 通过 MemoryRetriever 语义检索最匹配的技能（Skills），同时获取距离分数。
    2. 检测知识缺口：若相似度低于阈值，Fire-and-forget 触发 ms-agent deep_research，
       由 Worker-01 后台轮询，报告完成后自动蒸馏为技能（B-1/B-2）。
    3. 若找到 executable 技能 → 调用 SkillExecutor 真实执行脚本。
       若找到 knowledge 技能 → 将知识内容注入 LLM 上下文（B-3）。
    4. 若无匹配技能或执行失败 → Fallback 到 LLM 推理（注入历史错误警告）。
    5. 汇报结果给 Orchestrator（完成/失败）。
    """

    def __init__(self, name: str = "Worker-01"):
        self.name = name
        self.running = False

    async def start(self):
        """轮询任务队列，持续认领并执行任务。"""
        self.running = True
        logger.info(f"{self.name} started.")

        # 仅 Worker-01 负责轮询自修正研究任务（避免多 Worker 重复处理）
        if self.name == "Worker-01":
            asyncio.create_task(self._poll_amendment_tasks())
            logger.info("Worker-01: 自修正研究轮询已启动。")

        while self.running:
            task = await orchestrator.claim_task()
            if task:
                logger.info(f"{self.name} claimed task: {task.id[:8]}... [{task.description[:50]}]")
                await self.execute_task(task)
            else:
                await asyncio.sleep(2)

    async def execute_task(self, task: Task):
        """执行单个任务的完整逻辑（含知识缺口检测 + B-3 知识注入 + 自修正）。"""
        try:
            from app.services.memory_retriever import memory_retriever
            from sqlmodel import Session, select
            from app.core.db import engine

            # ---- Step 1: RAG 语义检索（含距离分数）----
            context = await memory_retriever.retrieve_context(
                task.description, top_k_skills=1, top_k_errors=3
            )
            skill_ids: list = context.get("relevant_skills_ids", [])
            skill_distances: list = context.get("skill_distances", [])
            error_warnings: list = context.get("error_warnings", [])

            # ---- Step 1.5: Phase 8 — 按 Skill.error_tags 精确检索相关错题 ----
            targeted_error_warnings: list = []
            if skill_ids:
                top_skill_id = skill_ids[0]
                with Session(engine) as session:
                    skill_meta_for_tags = session.exec(
                        select(SkillMetadata).where(SkillMetadata.id == top_skill_id)
                    ).first()

                if skill_meta_for_tags:
                    skill_error_tags = getattr(skill_meta_for_tags, "error_tags", []) or []
                    if skill_error_tags:
                        tagged_errors = await memory_retriever.retrieve_errors_by_tags(
                            task_description=task.description,
                            tags=skill_error_tags,
                            top_k=3,
                        )
                        for te in tagged_errors:
                            tag_label = ",".join(te["tags"]) if te["tags"] else "general"
                            targeted_error_warnings.append(
                                f"[{tag_label}] {te['context']}\n→ Correction: {te['correction']}"
                            )
                            _increment_hit_count(te["entry_id"])
                        if targeted_error_warnings:
                            logger.info(
                                f"{self.name} Phase8: retrieved {len(targeted_error_warnings)} "
                                f"tagged errors (tags={skill_error_tags}) for task {task.id[:8]}"
                            )

            # 合并：优先使用标签化错题，语义错题作为补充
            final_error_warnings = targeted_error_warnings if targeted_error_warnings else error_warnings

            # ---- Step 2: 知识缺口检测 ----
            knowledge_gap = self._detect_knowledge_gap(skill_ids, skill_distances)
            if knowledge_gap:
                logger.info(
                    f"{self.name} detected knowledge gap for task {task.id[:8]} "
                    f"(best_distance={skill_distances[0] if skill_distances else 'N/A'}). "
                    "Triggering deep_research amendment (fire-and-forget)..."
                )
                # B-1: Fire-and-forget 触发 ms-agent 深度研究
                await self._attempt_self_amendment(task.description)

            result: Any = None
            execution_mode: str = "llm_fallback"
            injected_knowledge: str = ""

            # ---- Step 3: 尝试调用 SkillExecutor 或注入知识内容 ----
            if skill_ids:
                top_skill_id = skill_ids[0]
                with Session(engine) as session:
                    skill_meta = session.exec(
                        select(SkillMetadata).where(SkillMetadata.id == top_skill_id)
                    ).first()

                if skill_meta:
                    # B-3: 文档型技能 — 将知识内容注入 LLM 上下文，不执行脚本
                    if getattr(skill_meta, "skill_type", "executable") == "knowledge":
                        knowledge_content = getattr(skill_meta, "knowledge_content", "") or ""
                        if knowledge_content:
                            injected_knowledge = knowledge_content
                            execution_mode = f"knowledge:{skill_meta.id}"
                            logger.info(
                                f"{self.name} injecting knowledge skill '{skill_meta.id}' "
                                f"into LLM context for task {task.id[:8]}"
                            )
                        else:
                            logger.warning(
                                f"{self.name} knowledge skill '{skill_meta.id}' "
                                "has no knowledge_content, falling back to LLM."
                            )
                    else:
                        # Executable 技能 — 调用 SkillExecutor 执行脚本
                        logger.info(
                            f"{self.name} matched skill '{skill_meta.id}' "
                            f"[{skill_meta.language}] for task {task.id[:8]}"
                        )
                        exec_result = await skill_executor.run(
                            language=skill_meta.language,
                            entrypoint=skill_meta.entrypoint,
                            params={"description": task.description},
                        )
                        if exec_result["status"] == "success":
                            result = exec_result.get("result", "")
                            execution_mode = f"skill:{skill_meta.id}"
                            logger.info(f"{self.name} skill execution succeeded for task {task.id[:8]}")
                        else:
                            logger.warning(
                                f"{self.name} skill execution failed: {exec_result.get('message')}, "
                                "falling back to LLM"
                            )

            # ---- Step 4: LLM Fallback（含知识注入）----
            if result is None:
                error_hint = (
                    "\n".join(final_error_warnings)
                    if final_error_warnings
                    else "无历史错误记录。"
                )
                knowledge_hint = (
                    f"\n\n## 相关知识（来自知识库）\n{injected_knowledge}"
                    if injected_knowledge
                    else ""
                )
                system_prompt = (
                    f"你是执行单元 {self.name}。\n"
                    f"你的当前任务是：\n{task.description}\n\n"
                    f"相关历史错误提示（供参考，避免重蹈覆辙）：\n{error_hint}"
                    f"{knowledge_hint}\n\n"
                    f"请直接执行该任务并返回结果。"
                )
                messages = [
                    Message(role="system", content=system_prompt),
                    Message(role="user", content="开始执行。"),
                ]
                response = await model_router.generate(messages=messages)
                result = response.content

            # ---- Step 5: 汇报完成 ----
            await orchestrator.complete_task(
                task.id,
                {"execution_mode": execution_mode, "result": result},
            )
            logger.info(
                f"{self.name} completed task {task.id[:8]} via [{execution_mode}]"
            )

        except Exception as e:
            logger.error(f"Worker {self.name} unhandled error on task {task.id}: {e}")
            await orchestrator.fail_task(task.id, str(e))

    # ------------------------------------------------------------------ #
    # 知识缺口检测（Phase 4c-3）
    # ------------------------------------------------------------------ #

    def _detect_knowledge_gap(self, skill_ids: list, skill_distances: list) -> bool:
        """
        判断是否存在知识缺口。

        条件（满足任一即判定）：
        - 没有找到任何技能（skill_ids 为空）
        - 最佳匹配技能的 L2 距离 > KNOWLEDGE_GAP_THRESHOLD

        Args:
            skill_ids:       RAG 检索返回的技能 ID 列表。
            skill_distances: 对应的 L2 距离列表（越小越相似）。

        Returns:
            True — 存在知识缺口，需要自我修正。
        """
        if not skill_ids:
            return True
        if skill_distances and skill_distances[0] > KNOWLEDGE_GAP_THRESHOLD:
            return True
        return False

    async def _attempt_self_amendment(self, task_description: str):
        """
        知识自我修正（Phase 6 B-1）。

        流程：
        1. 调用 ms_agent_service.run_deep_research() 触发深度研究（非阻塞）。
        2. 将 research task_id 存入全局 _pending_amendments 字典。
        3. Worker-01 的 _poll_amendment_tasks() 定期检查完成状态。
        4. 研究完成后由 _distill_report_to_skills() 蒸馏为技能。
        5. 任何失败均不影响当前任务继续执行（容错设计）。
        """
        # 防止重复研究相同查询
        if task_description in _pending_amendments.values():
            logger.debug(f"{self.name} amendment already pending for similar query, skipping.")
            return

        try:
            from app.services.ms_agent_service import ms_agent_service
            result = await ms_agent_service.run_deep_research(
                query=task_description,
                max_rounds=4,
            )
            research_task_id = result["task_id"]
            _pending_amendments[research_task_id] = task_description
            logger.info(
                f"{self.name} self-amendment: deep_research started "
                f"task_id={research_task_id[:8]}... query='{task_description[:50]}'"
            )
        except Exception as e:
            logger.warning(f"{self.name} self-amendment: failed to start deep_research (non-blocking): {e}")

    # ------------------------------------------------------------------ #
    # B-1: 后台轮询自修正研究任务（仅 Worker-01 运行）
    # ------------------------------------------------------------------ #

    async def _poll_amendment_tasks(self):
        """
        定期轮询 _pending_amendments 中未完成的研究任务。

        - 每 60 秒检查一次。
        - completed → 触发 _distill_report_to_skills。
        - failed     → 记录警告并移除。
        - running/pending → 继续等待。
        """
        logger.info("Worker-01: _poll_amendment_tasks loop started.")
        while self.running:
            await asyncio.sleep(AMENDMENT_POLL_INTERVAL)

            if not _pending_amendments:
                continue

            from app.services.ms_agent_service import ms_agent_service
            completed_ids = []

            for task_id, original_query in list(_pending_amendments.items()):
                try:
                    status = ms_agent_service.get_task_status("research", task_id)
                    current_status = status.get("status", "unknown")

                    if current_status == "completed":
                        report = status.get("report", "")
                        if report:
                            logger.info(
                                f"Worker-01: research {task_id[:8]} completed, "
                                "starting skill distillation..."
                            )
                            await self._distill_report_to_skills(task_id, report, original_query)
                        else:
                            logger.warning(
                                f"Worker-01: research {task_id[:8]} completed "
                                "but report is empty, skipping distillation."
                            )
                        completed_ids.append(task_id)

                    elif current_status == "failed":
                        logger.warning(
                            f"Worker-01: research {task_id[:8]} failed, "
                            f"stderr: {status.get('stderr_tail', '')[:100]}"
                        )
                        completed_ids.append(task_id)

                except Exception as e:
                    logger.warning(f"Worker-01: poll error for task {task_id[:8]}: {e}")

            for tid in completed_ids:
                _pending_amendments.pop(tid, None)

    # ------------------------------------------------------------------ #
    # B-2: 研究报告 → 技能蒸馏器
    # ------------------------------------------------------------------ #

    async def _distill_report_to_skills(
        self, task_id: str, report_content: str, original_query: str
    ):
        """
        将 deep_research 产出的报告蒸馏为技能并注册到系统（Phase 6 B-2）。

        流程：
        1. 将报告整体注册为一个「文档型技能」（skill_type=knowledge）。
        2. 调用 LLM 提炼可执行知识点，生成 executable 技能草案并注册（容错）。
        """
        short_id = task_id[:6]
        knowledge_skill_id = f"research-knowledge-{short_id}"

        # ---- 步骤 1: 注册文档型技能（立即可用）----
        try:
            from sqlmodel import Session, select
            from app.core.db import engine
            from app.models.schemas import SkillManifest
            from app.services.memory_retriever import memory_retriever
            from app.services.tool_registry import tool_registry

            with Session(engine) as session:
                existing = session.exec(
                    select(SkillMetadata).where(SkillMetadata.id == knowledge_skill_id)
                ).first()

            if not existing:
                knowledge_skill = SkillMetadata(
                    id=knowledge_skill_id,
                    name=f"研究知识: {original_query[:30]}",
                    language="python",
                    entrypoint="",
                    description=f"深度研究报告 | 查询: {original_query}",
                    skill_type="knowledge",
                    knowledge_content=report_content[:20000],
                )
                with Session(engine) as session:
                    session.add(knowledge_skill)
                    session.commit()

                manifest = SkillManifest(
                    id=knowledge_skill_id,
                    name=knowledge_skill.name,
                    description=knowledge_skill.description,
                    language="python",
                    content="",
                )
                await memory_retriever.add_skill(manifest)
                tool_registry.invalidate_cache()
                logger.info(
                    f"Distiller: registered knowledge skill '{knowledge_skill_id}' "
                    f"({len(report_content)} chars)"
                )
        except Exception as e:
            logger.warning(f"Distiller: failed to register knowledge skill: {e}")

        # ---- 步骤 2: LLM 提炼可执行技能草案（容错）----
        skill_drafts = []
        try:
            distill_prompt = (
                f"你是知识工程师。以下是关于「{original_query}」的深度研究报告摘要。\n\n"
                f"报告内容（最多 {REPORT_MAX_CHARS} 字）：\n"
                f"{report_content[:REPORT_MAX_CHARS]}\n\n"
                "请从报告中提取 1-3 个最有价值的可操作知识点，每个知识点生成一个技能定义。\n"
                "只输出 JSON 数组（不要 markdown 代码块），若无可提炼内容返回 []：\n"
                '[{"id": "kebab-case-id", "name": "中文名", '
                '"description": "技能描述", "language": "python", '
                '"entrypoint": "scripts/main.py", '
                '"script_content": "# 执行逻辑 Python 代码", "parameters_schema": {}}]'
            )
            response = await model_router.generate(
                messages=[
                    Message(role="system", content="只输出 JSON 数组，不要任何 markdown 格式。"),
                    Message(role="user", content=distill_prompt),
                ],
                temperature=0.2,
                max_tokens=2000,
            )
            raw = re.sub(r"^```(?:json)?\s*", "", (response.content or "").strip())
            raw = re.sub(r"\s*```$", "", raw)
            skill_drafts = json.loads(raw)
            if not isinstance(skill_drafts, list):
                skill_drafts = []
        except Exception as e:
            logger.warning(f"Distiller: LLM distillation failed (non-blocking): {e}")

        registered_count = 0
        for draft in skill_drafts[:3]:
            try:
                raw_id = re.sub(r"[^a-z0-9-]", "-", str(draft.get("id", "patch")).lower())
                skill_id = f"{raw_id}-{short_id}"

                from sqlmodel import Session, select
                from app.core.db import engine
                from app.models.schemas import SkillManifest
                from app.services.memory_retriever import memory_retriever
                from app.services.tool_registry import tool_registry
                import os

                with Session(engine) as session:
                    if session.exec(
                        select(SkillMetadata).where(SkillMetadata.id == skill_id)
                    ).first():
                        continue

                script_content = draft.get("script_content", "")
                entrypoint = draft.get("entrypoint", "scripts/main.py")
                if script_content and entrypoint:
                    script_path = os.path.join("/app", "skills", skill_id, entrypoint)
                    os.makedirs(os.path.dirname(script_path), exist_ok=True)
                    with open(script_path, "w", encoding="utf-8") as f:
                        f.write(script_content)
                    entrypoint = os.path.relpath(script_path, "/app")

                skill_meta = SkillMetadata(
                    id=skill_id,
                    name=draft.get("name", skill_id),
                    language=draft.get("language", "python"),
                    entrypoint=entrypoint,
                    description=draft.get("description", ""),
                    parameters_schema=draft.get("parameters_schema", {}),
                    skill_type="executable",
                )
                with Session(engine) as session:
                    session.add(skill_meta)
                    session.commit()

                manifest = SkillManifest(
                    id=skill_id,
                    name=skill_meta.name,
                    description=skill_meta.description,
                    language=skill_meta.language,
                    content=script_content,
                )
                await memory_retriever.add_skill(manifest)
                tool_registry.invalidate_cache()
                registered_count += 1
                logger.info(f"Distiller: registered executable skill '{skill_id}'")
            except Exception as e:
                logger.warning(f"Distiller: failed to register skill draft: {e}")

        logger.info(
            f"Distiller: task {task_id[:8]} done — "
            f"1 knowledge skill + {registered_count} executable skill(s) registered."
        )


# 保留单实例引用，向后兼容（实际由 main.py 启动多个）
worker_agent = WorkerAgent()
