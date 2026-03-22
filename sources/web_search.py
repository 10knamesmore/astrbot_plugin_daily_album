from __future__ import annotations

import aiohttp

from astrbot.api import logger

from .base import AlbumInfo, AlbumSource
from .llm import LLMSource

_SEARCH_TIMEOUT = aiohttp.ClientTimeout(total=15)


class WebSearchSource(AlbumSource):
    def __init__(self, context, config: dict) -> None:
        self._context = context
        self._config = config
        self._llm = LLMSource(context, config)

    @property
    def source_name(self) -> str:
        return "web_search"

    async def fetch(
        self,
        prompt: str,
        history: list[AlbumInfo],
    ) -> AlbumInfo | None:
        snippets = await self._search(prompt)
        return await self._llm.fetch(prompt, history, search_snippets=snippets)

    async def _search(self, prompt: str) -> str:
        """搜索并返回拼接好的文本片段，失败时返回空字符串"""
        # 构造搜索关键词：提取 prompt 前 50 字作为 query 补充
        query = f"专辑推荐 {prompt[:50]}"

        results: list[dict] = []

        # 1. 尝试 Tavily
        tavily_key = self._get_tavily_key()
        if tavily_key:
            try:
                results = await self._search_tavily(query, tavily_key)
            except Exception as e:
                logger.warning(f"[DailyAlbum] Tavily 搜索失败：{e}，回退 Bing")

        # 2. 回退 Bing
        if not results:
            try:
                results = await self._search_bing(query)
            except Exception as e:
                logger.warning(f"[DailyAlbum] Bing 搜索失败：{e}")

        if not results:
            return "（未获取联网信息）"

        lines = []
        for r in results[:5]:
            title = r.get("title", "")
            content = r.get("content", r.get("snippet", ""))[:300]
            if title or content:
                lines.append(f"【{title}】\n{content}")
        return "\n\n".join(lines) if lines else "（未获取联网信息）"

    def _get_tavily_key(self) -> str:
        try:
            cfg = self._context.get_config()
            keys = cfg.get("provider_settings", {}).get("websearch_tavily_key", [])
            if isinstance(keys, str):
                return keys.strip()
            if isinstance(keys, list) and keys:
                return str(keys[0]).strip()
        except Exception:
            pass
        return ""

    async def _search_tavily(self, query: str, api_key: str) -> list[dict]:
        async with aiohttp.ClientSession() as session:
            payload = {
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": 5,
            }
            async with session.post(
                "https://api.tavily.com/search",
                json=payload,
                timeout=_SEARCH_TIMEOUT,
            ) as resp:
                data = await resp.json()
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "content": r.get("content", ""),
                    }
                    for r in data.get("results", [])
                ]

    async def _search_bing(self, query: str) -> list[dict]:
        from urllib.parse import quote

        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.warning("[DailyAlbum] BeautifulSoup 未安装，跳过 Bing 搜索")
            return []

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        url = f"https://cn.bing.com/search?q={quote(query)}&mkt=zh-CN"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                html = await resp.text()

        soup = BeautifulSoup(html, "html.parser")
        results = []
        for item in soup.select("li.b_algo")[:5]:
            title_el = item.select_one("h2 a")
            snippet_el = item.select_one(".b_caption p")
            if title_el:
                results.append(
                    {
                        "title": title_el.get_text(strip=True),
                        "url": title_el.get("href", ""),
                        "content": snippet_el.get_text(strip=True) if snippet_el else "",
                    }
                )
        return results
