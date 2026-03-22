from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime
from typing import get_origin, get_type_hints

from astrbot.api import logger

from .base import AlbumInfo, AlbumSource

SYSTEM_PROMPT = (
    "你是专业的音乐编辑。你的输出必须是合法 JSON 对象，不含任何其他内容。\n"
    "字段说明：netease_id 是该专辑在网易云音乐的 ID（纯数字字符串），"
    "如果你不确定请留空字符串，不要猜测或编造。"
)


def _make_output_format() -> str:
    """根据 AlbumInfo 字段定义自动生成 JSON 输出格式示例。"""
    hints = get_type_hints(AlbumInfo)
    example = {
        f.name: ["..."] if get_origin(hints.get(f.name)) is list else "..."
        for f in dataclasses.fields(AlbumInfo)
    }
    return json.dumps(example, ensure_ascii=False)


OUTPUT_FORMAT = _make_output_format()


def _to_str_list(v: object) -> list[str]:
    """将 LLM 返回的 str 或 list 统一转为 list[str]。"""
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str) and v.strip():
        return [v.strip()]
    return []


def _parse_album_json(text: str) -> dict | None:
    """三级回退 JSON 解析"""
    # 1. 直接解析
    try:
        return json.loads(text)
    except Exception:
        pass
    # 2. 提取 ```json ... ``` 块
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
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
    def __init__(self, context, config: dict) -> None:
        self._context = context
        self._config = config

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
        today = datetime.now().strftime("%Y年%m月%d日")
        recent = history[-max_history:] if history else []
        history_lines = "\n".join(
            f"- {a.album_name} / {', '.join(a.artist)}" for a in recent
        ) or "（暂无）"

        parts = [
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

        max_history = int(self._config.get("max_history_in_prompt", 30))
        user_prompt = self._build_prompt(prompt, history, max_history, search_snippets)

        try:
            resp = await self._context.llm_generate(
                chat_provider_id=provider.meta().id,
                prompt=user_prompt,
                system_prompt=SYSTEM_PROMPT,
            )
            text = resp.completion_text.strip()
        except Exception as e:
            logger.error(f"[DailyAlbum] LLM 调用失败：{e}")
            return None

        data = _parse_album_json(text)
        if not data:
            logger.error(f"[DailyAlbum] JSON 解析全部失败，原始输出：{text[:200]}")
            return None

        album_name = data.get("album_name", "").strip()
        artist = _to_str_list(data.get("artist", []))
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
            netease_id=str(data.get("netease_id", "")),
        )
