from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime
from typing import Any, get_origin, get_type_hints

from astrbot.api import logger
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context

from .base import AlbumInfo, AlbumSource

SYSTEM_PROMPT: str = "你是专业的音乐编辑。你的输出必须是合法 JSON 对象，不含任何其他内容。"


def _make_output_format() -> str:
    """根据 AlbumInfo 字段定义自动生成 JSON 输出格式示例。"""
    hints: dict[str, Any] = get_type_hints(AlbumInfo)
    example: dict[str, str | list[str]] = {
        f.name: ["..."] if get_origin(hints.get(f.name)) is list else "..."
        for f in dataclasses.fields(AlbumInfo)
    }
    return json.dumps(example, ensure_ascii=False)


OUTPUT_FORMAT: str = _make_output_format()


def _to_str_list(v: object) -> list[str]:
    """将 LLM 返回的 str 或 list 统一转为 list[str]。"""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def _parse_album_json(text: str) -> dict[str, Any] | None:
    """三级回退 JSON 解析"""
    # 1. 直接解析
    try:
        return json.loads(text)
    except Exception:
        pass
    # 2. 提取 ```json ... ``` 块
    m: re.Match[str] | None = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 3. 提取第一个 {...}
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return None


class LLMSource(AlbumSource):
    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        self._context: Context = context
        self._config: dict[str, Any] = config

    @property
    def source_name(self) -> str:
        return "llm"

    def _build_prompt(
        self,
        recommend_prompt: str,
        history: list[AlbumInfo],
        max_history: int,
        search_snippets: str = "",
    ) -> str:
        today: str = datetime.now().strftime("%Y年%m月%d日")
        recent: list[AlbumInfo] = history[-max_history:] if history else []
        history_lines: str = (
            "\n".join(f"- {a.album_name} / {', '.join(a.artist)}" for a in recent)
            or "（暂无）"
        )

        parts: list[str] = [
            f"今天是 {today}，请推荐今日专辑。",
            "",
            "【推荐要求】",
            recommend_prompt,
            "",
            "【已推荐历史（请勿重复）】",
            history_lines,
        ]

        if search_snippets:
            parts += ["", "【网络参考信息】", search_snippets]

        parts += [
            "",
            "【输出格式】",
            OUTPUT_FORMAT,
        ]

        return "\n".join(parts)

    async def fetch(
        self,
        prompt: str,
        history: list[AlbumInfo],
        search_snippets: str = "",
    ) -> AlbumInfo | None:
        provider = self._context.get_using_provider()
        if not provider:
            logger.error("[DailyAlbum] 无可用 LLM Provider")
            return None

        max_history: int = int(self._config.get("max_history_in_prompt", 30))
        user_prompt: str = self._build_prompt(
            prompt, history, max_history, search_snippets
        )

        try:
            resp: LLMResponse = await self._context.llm_generate(
                chat_provider_id=provider.meta().id,
                prompt=user_prompt,
                system_prompt=SYSTEM_PROMPT,
            )
            text: str = resp.completion_text.strip()
        except Exception as e:
            logger.error(f"[DailyAlbum] LLM 调用失败：{e}")
            return None

        data: dict[str, Any] | None = _parse_album_json(text)
        if not data:
            logger.error(f"[DailyAlbum] JSON 解析全部失败，原始输出：{text[:200]}")
            return None

        album_name: str = data.get("album_name", "").strip()
        artist: list[str] = _to_str_list(data.get("artist", []))
        if not album_name or not artist:
            logger.error(f"[DailyAlbum] 缺少必填字段 album_name/artist，data={data}")
            return None

        return AlbumInfo(
            album_name=album_name,
            artist=artist,
            year=str(data.get("year", "")),
            genre=_to_str_list(data.get("genre", [])),
            cover_url=str(data.get("cover_url", "")),
            description=str(data.get("description", "")),
            listen_tip=str(data.get("listen_tip", "")),
        )
