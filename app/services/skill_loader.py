"""
SkillLoader — 双格式技能加载器

支持两种技能目录格式，优先 ms-agent 原生格式：

【格式 A — ms-agent 原生（优先）】
    skill-name/
    ├── META.yaml          # 技能元数据（ms-agent 规范）
    ├── SKILL.md           # 纯 Markdown 技能文档（无 frontmatter）
    ├── scripts/
    │   └── main.py
    ├── references/        # 参考文档（可选）
    ├── resources/         # 资源文件（可选）
    └── requirements.txt   # 依赖（可选）

【META.yaml 格式】（ms-agent 原生）：
    name: 技能名称
    description: 技能描述
    version: "1.0.0"
    author: author
    language: python
    entrypoint: scripts/main.py
    parameters_schema:
      type: object
      properties:
        key:
          type: string
          description: 说明
    tags:
      - coding

【格式 B — Watery Legacy（向后兼容）】（无 META.yaml 时 fallback）
    skill-name/
    ├── SKILL.md           # 含 YAML frontmatter
    └── scripts/
        └── main.py

    SKILL.md 格式：
        ---
        name: 技能名称
        description: 技能描述
        language: python
        entrypoint: scripts/main.py
        parameters_schema: ...
        tags: [...]
        ---
        # 正文 Markdown
        ...
"""

import logging
import os
import re
from typing import Optional

import yaml

from app.models.schemas import SkillCreate

logger = logging.getLogger(__name__)


class SkillLoader:
    """
    扫描技能目录，将每个子目录内的技能定义解析为 SkillCreate 对象。

    解析优先级：
      1. META.yaml（ms-agent 原生格式）
      2. SKILL.md YAML frontmatter（Watery Legacy 格式）
    """

    # ------------------------------------------------------------------ #
    # 公共接口
    # ------------------------------------------------------------------ #

    def load_one(self, skill_dir: str) -> Optional[SkillCreate]:
        """
        从单个技能目录解析技能定义，返回 SkillCreate 对象。
        若目录不存在或格式有误则返回 None 并记录警告。

        Args:
            skill_dir: 技能目录的绝对或相对路径。
        """
        skill_dir = os.path.abspath(skill_dir)

        # ---- 格式 A：META.yaml（ms-agent 原生）----
        meta_yaml_path = os.path.join(skill_dir, "META.yaml")
        if os.path.isfile(meta_yaml_path):
            return self._load_from_meta_yaml(skill_dir, meta_yaml_path)

        # ---- 格式 B：SKILL.md frontmatter（Watery Legacy）----
        skill_md_path = os.path.join(skill_dir, "SKILL.md")
        if os.path.isfile(skill_md_path):
            return self._load_from_skill_md(skill_dir, skill_md_path)

        logger.warning(
            f"SkillLoader: neither META.yaml nor SKILL.md found in {skill_dir}"
        )
        return None

    def load_dir(self, skills_root: str) -> list[SkillCreate]:
        """
        扫描 skills_root 下的所有子目录，解析含有技能定义文件的目录。
        返回成功解析的 SkillCreate 列表。

        Args:
            skills_root: 技能根目录（容器内路径，如 /app/skills）。
        """
        results: list[SkillCreate] = []
        if not os.path.isdir(skills_root):
            logger.warning(f"SkillLoader: skills root directory not found: {skills_root}")
            return results

        for entry in sorted(os.scandir(skills_root), key=lambda e: e.name):
            if not entry.is_dir():
                continue
            skill = self.load_one(entry.path)
            if skill:
                results.append(skill)
                logger.info(
                    f"SkillLoader: loaded skill '{skill.id}' from {entry.path}"
                )

        logger.info(f"SkillLoader: {len(results)} skill(s) loaded from {skills_root}")
        return results

    # ------------------------------------------------------------------ #
    # 内部：格式 A — META.yaml（ms-agent 原生）
    # ------------------------------------------------------------------ #

    def _load_from_meta_yaml(
        self, skill_dir: str, meta_yaml_path: str
    ) -> Optional[SkillCreate]:
        """从 META.yaml + SKILL.md（纯 Markdown）加载技能。"""
        try:
            with open(meta_yaml_path, "r", encoding="utf-8") as f:
                meta = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"SkillLoader: cannot parse META.yaml at {meta_yaml_path}: {e}")
            return None

        name = meta.get("name")
        description = meta.get("description", "")
        if not name:
            logger.warning(f"SkillLoader: 'name' is required in {meta_yaml_path}")
            return None

        dir_name = os.path.basename(skill_dir)
        skill_id = self._to_id(meta.get("id") or dir_name)
        language = meta.get("language", "python")
        entrypoint_rel = meta.get("entrypoint", f"scripts/main.{self._ext(language)}")
        entrypoint = os.path.join(skill_dir, entrypoint_rel)
        parameters_schema = meta.get("parameters_schema") or {}

        # 将 SKILL.md 正文追加到 description（如果存在）
        skill_md_path = os.path.join(skill_dir, "SKILL.md")
        full_description = description
        if os.path.isfile(skill_md_path):
            try:
                with open(skill_md_path, "r", encoding="utf-8") as f:
                    md_body = f.read().strip()
                if md_body:
                    full_description = f"{description}\n\n---\n{md_body}"
            except OSError:
                pass

        # 读取脚本内容（可选）
        script_content: Optional[str] = None
        if os.path.isfile(entrypoint):
            try:
                with open(entrypoint, "r", encoding="utf-8") as sf:
                    script_content = sf.read()
            except OSError:
                pass

        logger.debug(f"SkillLoader: loaded skill '{skill_id}' via META.yaml format")
        return SkillCreate(
            id=skill_id,
            name=name,
            description=full_description,
            language=language,
            entrypoint=entrypoint,
            parameters_schema=parameters_schema,
            script_content=script_content,
        )

    # ------------------------------------------------------------------ #
    # 内部：格式 B — SKILL.md frontmatter（Watery Legacy）
    # ------------------------------------------------------------------ #

    def _load_from_skill_md(
        self, skill_dir: str, skill_md_path: str
    ) -> Optional[SkillCreate]:
        """从 SKILL.md YAML frontmatter 加载技能（向后兼容）。"""
        try:
            with open(skill_md_path, "r", encoding="utf-8") as f:
                raw = f.read()
        except OSError as e:
            logger.error(f"SkillLoader: cannot read {skill_md_path}: {e}")
            return None

        frontmatter, body = self._parse_frontmatter(raw)
        if frontmatter is None:
            logger.warning(
                f"SkillLoader: missing YAML frontmatter in {skill_md_path} "
                "(and no META.yaml found). Skipping."
            )
            return None

        name = frontmatter.get("name")
        description = frontmatter.get("description")
        if not name or not description:
            logger.warning(
                f"SkillLoader: 'name' and 'description' are required in {skill_md_path}"
            )
            return None

        dir_name = os.path.basename(skill_dir)
        skill_id = self._to_id(dir_name)
        language = frontmatter.get("language", "python")
        entrypoint_rel = frontmatter.get("entrypoint", f"scripts/main.{self._ext(language)}")
        entrypoint = os.path.join(skill_dir, entrypoint_rel)
        parameters_schema = frontmatter.get("parameters_schema") or {}

        # 将 SKILL.md 正文附加到 description，扩充语义上下文
        full_description = description
        if body.strip():
            full_description = f"{description}\n\n---\n{body.strip()}"

        # 读取脚本内容（可选）
        script_content: Optional[str] = None
        if os.path.isfile(entrypoint):
            try:
                with open(entrypoint, "r", encoding="utf-8") as sf:
                    script_content = sf.read()
            except OSError:
                pass

        logger.debug(f"SkillLoader: loaded skill '{skill_id}' via SKILL.md frontmatter (legacy)")
        return SkillCreate(
            id=skill_id,
            name=name,
            description=full_description,
            language=language,
            entrypoint=entrypoint,
            parameters_schema=parameters_schema,
            script_content=script_content,
        )

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[Optional[dict], str]:
        """
        解析 YAML frontmatter（---...---），返回 (dict, body)。
        若无 frontmatter 返回 (None, 原文)。
        """
        pattern = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)
        m = pattern.match(text.lstrip())
        if not m:
            return None, text
        try:
            meta = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError as e:
            logger.error(f"SkillLoader: YAML parse error: {e}")
            return None, text
        return meta, m.group(2)

    @staticmethod
    def _to_id(name: str) -> str:
        """将目录名/ID 转为合法的 snake_case ID，仅保留字母数字和下划线。"""
        return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")

    @staticmethod
    def _ext(language: str) -> str:
        return {
            "python": "py",
            "shell": "sh",
            "sh": "sh",
            "nodejs": "js",
            "node": "js",
        }.get(language, "py")


skill_loader = SkillLoader()

