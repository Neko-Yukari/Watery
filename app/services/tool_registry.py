"""
ToolRegistry — Skills → OpenAI Function Definitions 转换层

功能：
  - 将 SQLite 中注册的 SkillMetadata 转换为 OpenAI tool calling 所需的 `tools` 参数格式
  - 提供 TTL 缓存（默认 30s），避免每次对话都查库
  - 提供 `invalidate_cache()` 接口，供 Skills CRUD 端点在修改后主动刷新
  - 分离 sanitize 逻辑，保证 function name 满足 OpenAI 约束（^[a-zA-Z0-9_-]{1,64}$）

用法：
    from app.services.tool_registry import tool_registry

    tools = tool_registry.get_tool_definitions()
    skill = tool_registry.get_tool_by_name("hello_world")
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional

from sqlmodel import Session, select

from app.core.db import engine
from app.models.database import SkillMetadata

logger = logging.getLogger(__name__)

# 缓存 TTL（秒）
_CACHE_TTL = 30.0


class ToolRegistry:
    """
    将 SQLite 中注册的 Skills 转换为 OpenAI Function Calling 工具定义列表。

    线程安全说明：本类仅维护 Python 内存缓存，asyncio 单线程环境中无竞争风险。
    """

    def __init__(self) -> None:
        self._cache: Optional[List[Dict[str, Any]]] = None
        self._cache_time: float = 0.0
        # fn_name → SkillMetadata 映射，随缓存同步刷新
        self._name_to_skill: Dict[str, SkillMetadata] = {}

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _sanitize_name(skill_id: str) -> str:
        """
        将 skill.id 转为合法的 OpenAI function name。

        规则：^[a-zA-Z0-9_-]{1,64}$
        - 非法字符替换为 '_'
        - 截断至 64 字符
        """
        sanitized = re.sub(r"[^a-zA-Z0-9_\-]", "_", skill_id)
        # 保证首字符不是数字（部分 SDK 会校验）
        if sanitized and sanitized[0].isdigit():
            sanitized = "skill_" + sanitized
        return sanitized[:64]

    @staticmethod
    def _build_parameters(parameters_schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """
        将 SkillMetadata.parameters_schema 规范化为 OpenAI function parameters 格式。

        支持两种输入：
        1. 已是 OpenAI 格式（含 "type": "object"）→ 原样返回
        2. 空 dict 或仅含 properties dict → 包装为 object 类型
        """
        if not parameters_schema:
            # 无参数技能：返回空 object schema
            return {"type": "object", "properties": {}}

        if parameters_schema.get("type") == "object":
            # 已符合 OpenAI 格式
            return parameters_schema

        # 兼容旧格式：直接是 {param_name: {type, description}, ...}
        return {
            "type": "object",
            "properties": parameters_schema,
            "required": [],
        }

    def _skill_to_tool_def(self, skill: SkillMetadata) -> Dict[str, Any]:
        """将单个 SkillMetadata 对象转为 OpenAI tool definition dict。"""
        fn_name = self._sanitize_name(skill.id)
        description = skill.description or skill.name or fn_name

        return {
            "type": "function",
            "function": {
                "name": fn_name,
                "description": description,
                "parameters": self._build_parameters(skill.parameters_schema),
            },
        }

    def _refresh(self) -> None:
        """从 SQLite 重新加载所有技能，刷新缓存。"""
        try:
            with Session(engine) as session:
                skills = session.exec(select(SkillMetadata)).all()

            self._name_to_skill.clear()
            tool_defs: List[Dict[str, Any]] = []

            for skill in skills:
                fn_name = self._sanitize_name(skill.id)
                # 如有 id 冲突（不同 id sanitize 后相同），后者覆盖前者并记录警告
                if fn_name in self._name_to_skill:
                    logger.warning(
                        f"ToolRegistry: function name collision '{fn_name}' "
                        f"(skills: {self._name_to_skill[fn_name].id} vs {skill.id}), "
                        "latter overwrites former."
                    )
                self._name_to_skill[fn_name] = skill
                tool_defs.append(self._skill_to_tool_def(skill))

            self._cache = tool_defs
            self._cache_time = time.monotonic()
            logger.debug(f"ToolRegistry: refreshed, {len(tool_defs)} tools loaded.")

        except Exception as exc:
            logger.error(f"ToolRegistry: refresh failed: {exc}")
            # 保留旧缓存宁可过旧，不要崩溃

    # ------------------------------------------------------------------ #
    # 公共接口
    # ------------------------------------------------------------------ #

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        """
        获取所有已注册技能的 OpenAI tool definitions 列表。

        使用 TTL 缓存（30s），频繁调用时不重复查库。
        若技能库为空则返回空列表（chat endpoint 收到空列表时不传 tools 参数给 LLM）。

        Returns:
            List[Dict]  — OpenAI `tools` 参数所需格式的列表
        """
        now = time.monotonic()
        if self._cache is None or (now - self._cache_time) >= _CACHE_TTL:
            self._refresh()
        return self._cache or []

    def get_tool_by_name(self, name: str) -> Optional[SkillMetadata]:
        """
        根据 OpenAI function name 查找对应的 SkillMetadata。

        若缓存为空则先触发刷新。

        Args:
            name: LLM 返回的 tool_call.function.name（已经过 sanitize 的名称）

        Returns:
            SkillMetadata 实例，或 None（未找到）
        """
        if not self._name_to_skill:
            self._refresh()
        return self._name_to_skill.get(name)

    def invalidate_cache(self) -> None:
        """
        主动清除缓存。

        应在以下情况后调用：
        - POST /skills    — 注册新技能
        - DELETE /skills  — 删除技能
        - PUT /skills     — 更新技能
        - POST /skills/load-dir — 批量导入
        """
        self._cache = None
        self._cache_time = 0.0
        self._name_to_skill.clear()
        logger.debug("ToolRegistry: cache invalidated.")


# 全局单例
tool_registry = ToolRegistry()
