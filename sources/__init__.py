from __future__ import annotations

import random
from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context

from .base import AlbumInfo, AlbumSource
from .llm import LLMSource
from .query_extractor import extract_search_query
from .script import ScriptSource
from .web_search import WebSearchSource

__all__ = ["AlbumInfo", "AlbumSource", "select_source", "extract_search_query"]


# 来源名 → (config 子键, 默认启用, 默认权重)
_SOURCE_DEFS: list[tuple[str, str, bool, int]] = [
    ("llm", "source_llm", True, 1),
    ("web_search", "source_web_search", True, 2),
    ("script", "source_script", False, 1),
]


def select_source(context: Context, config: dict[str, Any]) -> AlbumSource:
    """根据各来源的 enabled/weight 配置加权随机选择一个来源实例。"""
    candidates: list[str] = []
    weights: list[int] = []

    for name, cfg_key, default_enabled, default_weight in _SOURCE_DEFS:
        sub: dict[str, Any] = config.get(cfg_key, {})
        if sub.get(f"source_{name}_enabled", default_enabled):
            weight: int = max(1, int(sub.get(f"source_{name}_weight", default_weight)))
            candidates.append(name)
            weights.append(weight)

    if not candidates:
        logger.warning("[DailyAlbum] 所有来源均已禁用，回退到 llm")
        return LLMSource(context, config)

    chosen: str = random.choices(candidates, weights=weights, k=1)[0]
    logger.info(
        f"[DailyAlbum] 本次选择来源：{chosen}  "
        f"（候选：{list(zip(candidates, weights))}）"
    )

    match chosen:
        case "llm":
            return LLMSource(context, config)
        case "web_search":
            return WebSearchSource(context, config)
        case "script":
            return ScriptSource(config)
        case _:
            return LLMSource(context, config)
