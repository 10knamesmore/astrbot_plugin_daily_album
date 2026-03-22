from __future__ import annotations

import ast
import importlib
import importlib.util
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

from astrbot.api import logger

from .base import AlbumInfo, AlbumSource

_REQUIRED = {"album_name", "artist"}
_ALLOWED = {f.name for f in fields(AlbumInfo)}


def _extract_requirements(script_path: str) -> list[str]:
    """静态解析脚本，提取顶层 REQUIREMENTS 列表，失败返回空列表。"""
    try:
        source = Path(script_path).read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception:
        return []

    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == "REQUIREMENTS"
                for t in node.targets
            )
            and isinstance(node.value, ast.List)
        ):
            return [
                elt.value
                for elt in node.value.elts
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
            ]
    return []


def _ensure_requirements(requirements: list[str]) -> None:
    """检查并安装缺失的依赖包。"""
    for req in requirements:
        # 取包名部分（去掉版本约束）用于 import 检测
        pkg = req.split("==")[0].split(">=")[0].split("<=")[0].split("!=")[0].split("~=")[0].strip()
        # 包名可能含连字符（install 用 foo-bar，import 用 foo_bar）
        import_name = pkg.replace("-", "_")
        try:
            importlib.import_module(import_name)
        except ImportError:
            logger.info(f"[DailyAlbum] 安装依赖：{req}")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", req],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.error(
                    f"[DailyAlbum] 安装 {req} 失败：{result.stderr.strip()}"
                )
            else:
                logger.info(f"[DailyAlbum] {req} 安装成功")


class ScriptSource(AlbumSource):
    def __init__(self, config: dict) -> None:
        self._config = config

    @property
    def source_name(self) -> str:
        return "script"

    async def fetch(
        self,
        prompt: str,
        history: list[AlbumInfo],
    ) -> AlbumInfo | None:
        files: list = self._config.get("source_script", {}).get("script_file", [])
        script_path = files[0].strip() if files else ""
        if not script_path:
            logger.error("[DailyAlbum] 未上传自定义脚本文件")
            return None

        # 安装脚本声明的外部依赖
        requirements = _extract_requirements(script_path)
        if requirements:
            _ensure_requirements(requirements)

        try:
            spec = importlib.util.spec_from_file_location("_daily_album_script", script_path)
            if spec is None or spec.loader is None:
                logger.error(f"[DailyAlbum] 无法加载脚本：{script_path}")
                return None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:
            logger.error(f"[DailyAlbum] 加载脚本失败：{e}")
            return None

        fetch_fn = getattr(module, "fetch_album", None)
        if fetch_fn is None:
            logger.error(f"[DailyAlbum] 脚本 {script_path} 中未找到 fetch_album 函数")
            return None

        history_dicts = [
            {f.name: getattr(a, f.name) for f in fields(AlbumInfo)}
            for a in history
        ]

        try:
            result = await fetch_fn(prompt, history_dicts)
        except Exception as e:
            logger.error(f"[DailyAlbum] 脚本 fetch_album 抛出异常：{e}")
            return None

        if not isinstance(result, dict):
            logger.error(f"[DailyAlbum] 脚本返回值不是 dict：{type(result)}")
            return None

        missing = _REQUIRED - result.keys()
        if missing:
            logger.error(f"[DailyAlbum] 脚本返回值缺少必填字段：{missing}")
            return None

        _LIST_FIELDS = {"artist", "genre"}
        kwargs: dict = {}
        for k, v in result.items():
            if k not in _ALLOWED:
                continue
            if k in _LIST_FIELDS:
                kwargs[k] = [str(v)] if isinstance(v, str) else [str(x) for x in v]
            else:
                kwargs[k] = str(v)
        return AlbumInfo(**kwargs)
