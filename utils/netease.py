"""网易云音乐 API 封装：根据专辑名 + 艺术家搜出专辑首歌的歌曲 ID。

被 senders/aiocqhttp.py 用于发送音乐卡片，被 senders/telegram.py 用于拼接可点链接。
搬迁自原 main.py 的 _search_netease_song_id / _is_target_album，
改为接收 ctx + config 显式注入，去掉对插件实例的依赖。
"""

from __future__ import annotations

from typing import Any

import aiohttp

from astrbot.api import logger
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context

NETEASE_SEARCH_URL: str = "http://music.163.com/api/search/get/web"
NETEASE_ALBUM_URL_TEMPLATE: str = "http://music.163.com/api/album/{album_id}"
# 不带 #、可被 Telegram link preview 识别的歌曲页 URL 模板
NETEASE_SONG_URL_TEMPLATE: str = "https://music.163.com/song?id={song_id}"


async def _is_target_album(
    ctx: Context,
    candidate_name: str,
    candidate_artist: str,
    target_name: str,
    target_artist: list[str],
) -> bool:
    """用 LLM 判断网易云搜索结果是否就是目标专辑（接受 Deluxe/Remastered/Anniversary 等版本）。

    无可用 provider 时直接信任搜索结果（返回 True）。
    """
    provider = ctx.get_using_provider()
    if not provider:
        return True
    try:
        resp: LLMResponse = await ctx.llm_generate(
            chat_provider_id=provider.meta().id,
            prompt=(
                f"目标专辑：《{target_name}》，艺术家：{', '.join(target_artist)}\n"
                f"搜索结果：《{candidate_name}》，艺术家：{candidate_artist}\n\n"
                "判断搜索结果是否是目标专辑（同一张专辑的 Deluxe Edition、"
                "Remastered、Anniversary Edition 等版本均视为匹配）。只回答 yes 或 no。"
            ),
            system_prompt="你是音乐数据核验助手，只输出 yes 或 no，不输出任何其他内容。",
        )
        answer: str = resp.completion_text.strip().lower()
        logger.debug(f"[DailyAlbum] LLM 核验结果：{answer!r}")
        return answer.startswith("y")
    except Exception as e:
        logger.warning(f"[DailyAlbum] LLM 核验失败，信任当前结果：{e}")
        return True


async def search_netease_song_id(
    ctx: Context,
    config: dict[str, Any],
    album_name: str,
    artist: list[str],
) -> str | None:
    """搜索网易云专辑，返回专辑首歌的歌曲 ID；找不到返回 None。

    匹配策略：先用 "专辑名 艺术家" 关键词，再退化为纯 "专辑名"；
    每个候选都用 LLM 核验是否是目标专辑（兼容版本差异）。
    """
    max_attempts: int = int(config.get("netease_search_max_attempts", 3))
    timeout: aiohttp.ClientTimeout = aiohttp.ClientTimeout(total=8)
    keywords: list[str] = [f"{album_name} {' '.join(artist)}", album_name]

    try:
        async with aiohttp.ClientSession(cookies={"appver": "2.0.2"}) as session:
            for keyword in keywords:
                async with session.post(
                    NETEASE_SEARCH_URL,
                    data={
                        "s": keyword,
                        "limit": max_attempts,
                        "type": 10,
                        "offset": 0,
                    },
                    timeout=timeout,
                ) as resp:
                    data: dict[str, Any] = await resp.json(content_type=None)

                albums: list[dict[str, Any]] = data.get("result", {}).get(
                    "albums", []
                )
                if not albums:
                    logger.warning(
                        f"[DailyAlbum] 网易云专辑搜索无结果，keyword={keyword!r}"
                    )
                    continue

                for i, album in enumerate(albums):
                    album_id: int = album["id"]
                    album_title: str = album.get("name", "")
                    album_artist: str = album.get("artist", {}).get("name", "")
                    logger.info(
                        f"[DailyAlbum] 候选专辑 [{i + 1}/{len(albums)}]"
                        f"（keyword={keyword!r}）"
                        f" ID={album_id}，名称={album_title!r}，"
                        f"艺术家={album_artist!r}"
                    )

                    matched: bool = await _is_target_album(
                        ctx, album_title, album_artist, album_name, artist
                    )
                    if not matched:
                        logger.info("[DailyAlbum] LLM 判定不匹配，跳过")
                        continue

                    async with session.get(
                        NETEASE_ALBUM_URL_TEMPLATE.format(album_id=album_id),
                        timeout=timeout,
                    ) as resp:
                        detail: dict[str, Any] = await resp.json(content_type=None)

                    songs: list[dict[str, Any]] = detail.get("album", {}).get(
                        "songs", []
                    )
                    if not songs:
                        logger.warning(
                            f"[DailyAlbum] 专辑 {album_id} 歌曲列表为空，继续尝试"
                        )
                        continue

                    sid: str = str(songs[0]["id"])
                    logger.info(
                        f"[DailyAlbum] 取专辑第一首歌 ID={sid}，"
                        f"歌名={songs[0].get('name', '')!r}"
                    )
                    return sid

                logger.warning(
                    f"[DailyAlbum] keyword={keyword!r} 的 {len(albums)} 条候选"
                    "均未通过核验，尝试下一关键词"
                )

            logger.warning("[DailyAlbum] 所有关键词均未找到匹配专辑，放弃")
    except Exception as e:
        logger.warning(f"[DailyAlbum] 网易云搜索失败：{e}")
    return None
