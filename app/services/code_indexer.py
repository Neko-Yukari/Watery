"""
CodeIndexer — 代码语义索引引擎（Phase 11）

职责：
  1. 扫描项目目录下所有 .py 文件
  2. 使用 Python 标准库 ast 模块解析，提取类/函数/方法/常量
  3. 写入 SQLite (CodeSymbol 表) + ChromaDB (code_index_vector 集合)
  4. 支持全量重建和增量更新（基于文件 SHA-256 hash 对比）

零 LLM 消耗：全流程为纯规则解析 + ChromaDB 内置本地 Embedding，
            不调用任何大模型 API。

用法：
    from app.services.code_indexer import code_indexer

    # 全量重建
    stats = await code_indexer.build_full_index()

    # 增量更新（仅处理变更文件）
    stats = await code_indexer.update_incremental()

    # 语义搜索
    results = await code_indexer.search("处理 PDF 上传的函数", top_k=5)
"""

import ast
import asyncio
import hashlib
import logging
import os
import time
from datetime import datetime
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

from sqlmodel import Session, select, delete

from app.core.db import engine, get_chroma_client
from app.models.database import CodeSymbol

logger = logging.getLogger(__name__)


class CodeIndexer:
    """
    代码语义索引引擎。

    零 LLM 消耗：AST 解析为纯规则操作；ChromaDB 使用内置
    all-MiniLM-L6-v2 模型本地 Embedding，无需调用外部 API。
    """

    # 需要索引的目录（相对于项目根）
    INDEX_DIRS = ["app", "ms_agent", "scripts"]

    # 排除的目录/路径片段
    EXCLUDE_PATTERNS = ["__pycache__", ".git", "node_modules", "data", ".venv", "venv"]

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #

    def __init__(self) -> None:
        self.chroma_client = get_chroma_client()
        self._code_col = None  # 延迟初始化，避免构造时阻塞事件循环
        self._project_root = self._detect_project_root()

    def _get_code_col(self):
        """延迟获取/创建 ChromaDB 集合（线程池调用）。"""
        if self._code_col is None:
            self._code_col = self.chroma_client.get_or_create_collection(
                name="code_index_vector"
            )
        return self._code_col

    def _detect_project_root(self) -> str:
        """
        自动检测项目根目录。

        策略（按优先级）：
        1. 环境变量 PROJECT_ROOT
        2. 向上搜索 docker-compose.yml 所在目录
        3. 回退到 /app（Docker 容器默认工作目录）
        """
        env_root = os.environ.get("PROJECT_ROOT")
        if env_root and os.path.isdir(env_root):
            return env_root

        current = os.path.dirname(os.path.abspath(__file__))
        for _ in range(6):
            if os.path.exists(os.path.join(current, "docker-compose.yml")):
                return current
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent

        return "/app" if os.path.exists("/app") else os.getcwd()

    # ------------------------------------------------------------------ #
    # 内部工具：线程池包装器
    # ------------------------------------------------------------------ #

    async def _run_sync(self, fn, *args, **kwargs):
        """在默认线程池中运行同步函数（ChromaDB 调用用）。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

    # ------------------------------------------------------------------ #
    # 文件扫描
    # ------------------------------------------------------------------ #

    def _collect_py_files(self) -> List[Tuple[str, str]]:
        """
        扫描 INDEX_DIRS，返回 (absolute_path, relative_path) 元组列表。
        相对路径相对于项目根目录，使用正斜杠（跨平台一致）。
        """
        result: List[Tuple[str, str]] = []
        for index_dir in self.INDEX_DIRS:
            abs_dir = os.path.join(self._project_root, index_dir)
            if not os.path.isdir(abs_dir):
                continue
            for dirpath, dirnames, filenames in os.walk(abs_dir):
                # 原地修改 dirnames 以跳过排除目录
                dirnames[:] = [
                    d for d in dirnames
                    if not any(pat in d for pat in self.EXCLUDE_PATTERNS)
                ]
                for fname in filenames:
                    if not fname.endswith(".py"):
                        continue
                    abs_path = os.path.join(dirpath, fname)
                    # 转为相对路径（正斜杠）
                    rel_path = os.path.relpath(abs_path, self._project_root).replace("\\", "/")
                    result.append((abs_path, rel_path))
        return result

    @staticmethod
    def _file_hash(abs_path: str) -> str:
        """计算文件 SHA-256 哈希（hex 字符串，前 16 位）。"""
        try:
            with open(abs_path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()[:16]
        except OSError:
            return ""

    # ------------------------------------------------------------------ #
    # AST 解析
    # ------------------------------------------------------------------ #

    def _extract_signature(self, node: ast.AST) -> str:
        """
        从 AST 函数节点提取完整签名字符串。

        示例：
        - 'async def generate(self, messages: List[Message]) -> Dict'
        - 'def _sanitize_name(skill_id: str) -> str'
        """
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return ""
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        args_parts: List[str] = []

        # 位置参数
        all_args = node.args.args
        defaults = node.args.defaults
        # 为默认值对齐到末尾
        defaults_offset = len(all_args) - len(defaults)

        for i, arg in enumerate(all_args):
            part = arg.arg
            if arg.annotation:
                try:
                    part += f": {ast.unparse(arg.annotation)}"
                except Exception:
                    pass
            if i >= defaults_offset:
                try:
                    default_val = ast.unparse(defaults[i - defaults_offset])
                    part += f"={default_val}"
                except Exception:
                    pass
            args_parts.append(part)

        # *args
        if node.args.vararg:
            vararg_str = f"*{node.args.vararg.arg}"
            if node.args.vararg.annotation:
                try:
                    vararg_str += f": {ast.unparse(node.args.vararg.annotation)}"
                except Exception:
                    pass
            args_parts.append(vararg_str)

        # keyword-only 参数
        for kwarg in node.args.kwonlyargs:
            part = kwarg.arg
            if kwarg.annotation:
                try:
                    part += f": {ast.unparse(kwarg.annotation)}"
                except Exception:
                    pass
            args_parts.append(part)

        # **kwargs
        if node.args.kwarg:
            kwarg_str = f"**{node.args.kwarg.arg}"
            if node.args.kwarg.annotation:
                try:
                    kwarg_str += f": {ast.unparse(node.args.kwarg.annotation)}"
                except Exception:
                    pass
            args_parts.append(kwarg_str)

        returns = ""
        if node.returns:
            try:
                returns = f" -> {ast.unparse(node.returns)}"
            except Exception:
                pass

        return f"{prefix} {node.name}({', '.join(args_parts)}){returns}"

    def _extract_decorators(self, node: ast.AST) -> List[str]:
        """提取装饰器字符串列表。"""
        decorators = []
        for dec in getattr(node, "decorator_list", []):
            try:
                decorators.append(f"@{ast.unparse(dec)}")
            except Exception:
                pass
        return decorators

    @staticmethod
    def _get_docstring(node: ast.AST) -> str:
        """安全提取节点的 docstring。"""
        try:
            doc = ast.get_docstring(node)
            return (doc or "").strip()
        except Exception:
            return ""

    def _extract_imports(self, tree: ast.Module) -> List[str]:
        """提取模块级导入的顶层模块名列表（去重）。"""
        imports: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module.split(".")[0])
        return sorted(set(imports))

    def _parse_file(self, abs_path: str, rel_path: str, file_hash: str) -> List[CodeSymbol]:
        """
        解析单个 Python 文件，返回 CodeSymbol 列表。
        遇到 SyntaxError 时返回空列表（容错）。
        """
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError as e:
            logger.warning(f"code_indexer: cannot read {abs_path}: {e}")
            return []

        try:
            tree = ast.parse(source, filename=abs_path)
        except SyntaxError as e:
            logger.warning(f"code_indexer: AST parse failed for {rel_path}: {e}")
            return []

        symbols: List[CodeSymbol] = []
        file_imports = self._extract_imports(tree)

        # 模块级记录（记录文件本身）
        module_doc = self._get_docstring(tree)
        if module_doc:
            symbols.append(
                CodeSymbol(
                    file_path=rel_path,
                    symbol_name=rel_path.replace("/", ".").removesuffix(".py"),
                    symbol_type="module",
                    line_start=1,
                    line_end=len(source.splitlines()),
                    docstring=module_doc,
                    imports=file_imports,
                    file_hash=file_hash,
                )
            )

        # 遍历顶层节点
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                # 类本身
                class_doc = self._get_docstring(node)
                class_end = getattr(node, "end_lineno", node.lineno)
                symbols.append(
                    CodeSymbol(
                        file_path=rel_path,
                        symbol_name=node.name,
                        symbol_type="class",
                        line_start=node.lineno,
                        line_end=class_end,
                        docstring=class_doc,
                        decorators=self._extract_decorators(node),
                        file_hash=file_hash,
                    )
                )
                # 类的方法
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        method_end = getattr(child, "end_lineno", child.lineno)
                        method_doc = self._get_docstring(child)
                        symbols.append(
                            CodeSymbol(
                                file_path=rel_path,
                                symbol_name=f"{node.name}.{child.name}",
                                symbol_type="method",
                                parent_symbol=node.name,
                                line_start=child.lineno,
                                line_end=method_end,
                                signature=self._extract_signature(child),
                                docstring=method_doc,
                                decorators=self._extract_decorators(child),
                                file_hash=file_hash,
                            )
                        )

            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # 顶层函数
                func_end = getattr(node, "end_lineno", node.lineno)
                func_doc = self._get_docstring(node)
                symbols.append(
                    CodeSymbol(
                        file_path=rel_path,
                        symbol_name=node.name,
                        symbol_type="function",
                        line_start=node.lineno,
                        line_end=func_end,
                        signature=self._extract_signature(node),
                        docstring=func_doc,
                        decorators=self._extract_decorators(node),
                        file_hash=file_hash,
                    )
                )

            elif isinstance(node, ast.Assign):
                # 模块级 UPPER_CASE 常量
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id.isupper():
                        symbols.append(
                            CodeSymbol(
                                file_path=rel_path,
                                symbol_name=target.id,
                                symbol_type="global_var",
                                line_start=node.lineno,
                                line_end=getattr(node, "end_lineno", node.lineno),
                                file_hash=file_hash,
                            )
                        )

        return symbols

    # ------------------------------------------------------------------ #
    # ChromaDB 写入（在线程池中执行）
    # ------------------------------------------------------------------ #

    def _chroma_upsert(self, symbols: List[CodeSymbol]) -> None:
        """批量 upsert 符号到 ChromaDB（同步，供 run_executor 调用）。"""
        if not symbols:
            return
        col = self._get_code_col()
        ids, documents, metadatas = [], [], []
        for s in symbols:
            # Embedding 文档：name + signature + docstring 组合，保证语义完整性
            embed_doc = f"{s.symbol_name}\n{s.signature}\n{s.docstring}".strip()
            if not embed_doc:
                embed_doc = s.symbol_name  # 至少有名字
            ids.append(s.id)
            documents.append(embed_doc[:2000])  # 截断防止 embedding 超限
            metadatas.append({
                "file_path": s.file_path,
                "symbol_type": s.symbol_type,
                "line_start": s.line_start,
                "line_end": s.line_end,
            })
        col.upsert(ids=ids, documents=documents, metadatas=metadatas)

    def _chroma_delete_by_file(self, rel_path: str) -> None:
        """删除指定文件的所有 ChromaDB 条目（同步）。"""
        col = self._get_code_col()
        try:
            existing = col.get(where={"file_path": rel_path})
            if existing["ids"]:
                col.delete(ids=existing["ids"])
        except Exception as e:
            logger.warning(f"code_indexer: chroma delete for {rel_path} failed: {e}")

    def _chroma_clear_all(self) -> None:
        """清空 code_index_vector 集合（全量重建时使用）。"""
        try:
            self.chroma_client.delete_collection("code_index_vector")
            self._code_col = None
        except Exception:
            pass
        self._get_code_col()  # 重新创建

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #

    async def build_full_index(self) -> Dict[str, Any]:
        """
        全量索引重建。清空旧数据后扫描所有 .py 文件重新建立索引。
        返回统计信息 {"files_scanned": N, "symbols_indexed": M, "elapsed_ms": T}
        """
        t0 = time.monotonic()
        logger.info("code_indexer: starting full index rebuild...")

        # 1. 清空 SQLite CodeSymbol 表
        with Session(engine) as session:
            session.exec(delete(CodeSymbol))
            session.commit()

        # 2. 清空 ChromaDB 集合
        await self._run_sync(self._chroma_clear_all)

        # 3. 扫描并解析
        py_files = self._collect_py_files()
        all_symbols: List[CodeSymbol] = []

        for abs_path, rel_path in py_files:
            fhash = self._file_hash(abs_path)
            file_symbols = self._parse_file(abs_path, rel_path, fhash)
            all_symbols.extend(file_symbols)

        # 4. 批量写入 SQLite
        with Session(engine) as session:
            session.add_all(all_symbols)
            session.commit()

        # 5. 批量写入 ChromaDB（按批，避免单次 upsert 过大）
        batch_size = 100
        for i in range(0, len(all_symbols), batch_size):
            batch = all_symbols[i: i + batch_size]
            await self._run_sync(self._chroma_upsert, batch)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        stats = {
            "files_scanned": len(py_files),
            "symbols_indexed": len(all_symbols),
            "elapsed_ms": elapsed_ms,
        }
        logger.info(f"code_indexer: full index complete — {stats}")
        return stats

    async def update_incremental(self) -> Dict[str, Any]:
        """
        增量索引更新。仅重新解析 hash 发生变化的文件。
        返回 {"updated_files": N, "new_symbols": M, "removed_symbols": K, "elapsed_ms": T}
        """
        t0 = time.monotonic()

        py_files = self._collect_py_files()

        # 从 SQLite 加载当前每个文件的 hash
        with Session(engine) as session:
            rows = session.exec(
                select(CodeSymbol.file_path, CodeSymbol.file_hash).distinct()
            ).all()
        indexed_hashes: Dict[str, str] = {r[0]: r[1] for r in rows}

        # 当前磁盘上的文件集合
        current_paths = {rel for _, rel in py_files}
        indexed_paths = set(indexed_hashes.keys())

        # 已删除的文件 → 清理
        deleted_paths = indexed_paths - current_paths
        removed = 0
        for rel_path in deleted_paths:
            with Session(engine) as session:
                old = session.exec(
                    select(CodeSymbol).where(CodeSymbol.file_path == rel_path)
                ).all()
                removed += len(old)
                for sym in old:
                    session.delete(sym)
                session.commit()
            await self._run_sync(self._chroma_delete_by_file, rel_path)

        # 比较 hash，找出需要更新的文件
        updated_files = 0
        new_symbols = 0
        for abs_path, rel_path in py_files:
            current_hash = self._file_hash(abs_path)
            if indexed_hashes.get(rel_path) == current_hash:
                continue  # hash 相同，跳过

            # 删除旧符号
            with Session(engine) as session:
                old = session.exec(
                    select(CodeSymbol).where(CodeSymbol.file_path == rel_path)
                ).all()
                for sym in old:
                    session.delete(sym)
                session.commit()
            await self._run_sync(self._chroma_delete_by_file, rel_path)

            # 解析并写入新符号
            file_symbols = self._parse_file(abs_path, rel_path, current_hash)
            if file_symbols:
                with Session(engine) as session:
                    session.add_all(file_symbols)
                    session.commit()
                await self._run_sync(self._chroma_upsert, file_symbols)

            updated_files += 1
            new_symbols += len(file_symbols)

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        stats = {
            "updated_files": updated_files,
            "new_symbols": new_symbols,
            "removed_symbols": removed,
            "elapsed_ms": elapsed_ms,
        }
        if updated_files > 0 or removed > 0:
            logger.info(f"code_indexer: incremental update — {stats}")
        return stats

    async def search(
        self,
        query: str,
        top_k: int = 5,
        symbol_types: Optional[List[str]] = None,
        file_pattern: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        语义搜索代码符号。返回 AI 友好的定位信息列表。

        流程：
        1. ChromaDB 向量搜索 top_k * 3 个候选（扩大召回）
        2. 按 symbol_types 和 file_pattern 过滤
        3. 从 SQLite 补充完整元数据（行号、签名、装饰器等）
        4. 截断至 top_k 返回
        """
        try:
            # 扩大召回，过滤后结果不足 top_k 时有余量
            n_results = min(top_k * 3, 60)

            def _do_query():
                col = self._get_code_col()
                return col.query(query_texts=[query], n_results=n_results)

            chroma_result = await self._run_sync(_do_query)
        except Exception as e:
            logger.warning(f"code_indexer: chroma query failed: {e}")
            return []

        ids = chroma_result.get("ids", [[]])[0]
        distances = chroma_result.get("distances", [[]])[0]
        metadatas = chroma_result.get("metadatas", [[]])[0]

        if not ids:
            return []

        # 从 SQLite 补充完整字段
        results: List[Dict[str, Any]] = []
        with Session(engine) as session:
            for idx, sym_id in enumerate(ids):
                symbol = session.get(CodeSymbol, sym_id)
                if symbol is None:
                    continue

                # 类型过滤
                if symbol_types and symbol.symbol_type not in symbol_types:
                    continue

                # 文件路径前缀过滤
                if file_pattern and not symbol.file_path.startswith(file_pattern):
                    continue

                # 距离转为相关度（ChromaDB L2 距离，越小越相关）
                distance = distances[idx] if idx < len(distances) else 1.0
                relevance = max(0.0, min(1.0, 1.0 - distance / 2.0))

                results.append({
                    "file_path": symbol.file_path,
                    "symbol_name": symbol.symbol_name,
                    "symbol_type": symbol.symbol_type,
                    "parent_symbol": symbol.parent_symbol,
                    "line_start": symbol.line_start,
                    "line_end": symbol.line_end,
                    "signature": symbol.signature,
                    "docstring": symbol.docstring,
                    "decorators": symbol.decorators or [],
                    "relevance_score": round(relevance, 4),
                })

                if len(results) >= top_k:
                    break

        return results

    def get_index_status(self) -> Dict[str, Any]:
        """
        同步获取索引状态摘要。
        返回 {"total_files": N, "total_symbols": M, "stale_files": K, "last_indexed_at": "..."}
        """
        with Session(engine) as session:
            all_symbols = session.exec(select(CodeSymbol)).all()

        total_symbols = len(all_symbols)

        # 按文件统计
        file_hashes: Dict[str, str] = {}
        last_indexed: Optional[datetime] = None
        for sym in all_symbols:
            file_hashes[sym.file_path] = sym.file_hash
            if sym.indexed_at and (last_indexed is None or sym.indexed_at > last_indexed):
                last_indexed = sym.indexed_at

        total_files = len(file_hashes)

        # 检查磁盘文件的 hash 是否已变更（stale 文件数）
        stale = 0
        py_files = self._collect_py_files()
        for abs_path, rel_path in py_files:
            disk_hash = self._file_hash(abs_path)
            if rel_path in file_hashes and file_hashes[rel_path] != disk_hash:
                stale += 1

        return {
            "total_files": total_files,
            "total_symbols": total_symbols,
            "last_indexed_at": last_indexed.isoformat() if last_indexed else None,
            "stale_files": stale,
        }

    # ------------------------------------------------------------------ #
    # 文件监听后台任务（开发环境）
    # ------------------------------------------------------------------ #

    async def start_file_watcher(self, interval: float = 30.0) -> None:
        """
        后台定期检查文件变更并增量更新索引。

        不依赖 watchdog 库，使用简单的定时轮询 + hash 对比。
        interval 默认 30 秒（开发环境适用）。
        """
        logger.info(f"code_indexer: file watcher started (interval={interval}s)")
        while True:
            await asyncio.sleep(interval)
            try:
                stats = await self.update_incremental()
                if stats.get("updated_files", 0) > 0 or stats.get("removed_symbols", 0) > 0:
                    logger.info(f"code_indexer: auto-updated — {stats}")
            except Exception as e:
                logger.warning(f"code_indexer: auto-update failed (non-fatal): {e}")


# ---- 模块级单例 ----
code_indexer = CodeIndexer()
