from __future__ import annotations

from astrbot.api import logger
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context


async def extract_search_query(context: Context, prompt: str) -> str:
    """用 LLM 从推荐提示词中提炼出适合搜索引擎的关键词短语。"""
    provider = context.get_using_provider()
    if not provider:
        logger.debug("[DailyAlbum] 无可用 Provider，跳过关键词提取")
        return prompt

    try:
        resp: LLMResponse = await context.llm_generate(
            chat_provider_id=provider.meta().id,
            prompt=(
                f"以下是一段专辑推荐需求描述：\n{prompt}\n\n"
                "请从中提炼出 3-6 个用于搜索引擎的关键词，"
                "以空格分隔输出，不要有任何其他内容。"
            ),
            system_prompt="你是搜索关键词提取助手，只输出关键词，不输出任何解释。",
        )
        keywords: str = resp.completion_text.strip()
        logger.debug(f"[DailyAlbum] 关键词提取结果：{keywords!r}")
        return keywords
    except Exception as e:
        logger.warning(f"[DailyAlbum] 关键词提取失败，回退原始提示词：{e}")
        return prompt
