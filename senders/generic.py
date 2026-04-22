"""未知平台兜底 sender：只发主推荐文案的纯文本，保证最低可用。"""

from __future__ import annotations

from astrbot.api import logger

from .base import AlbumSender, SendContext, SendResult


class GenericSender(AlbumSender):
    """所有未注册平台类型的兜底实现。

    只走 ``StarTools.send_message`` 发主文案，不尝试任何平台特化的卡片 / 链接，
    确保至少 LLM 推荐文案能被用户看到。
    """

    @property
    def platform_type(self) -> str:
        # GenericSender 不绑定特定平台类型；工厂用类身份直接选用，不走类型映射。
        return "__generic__"

    async def send(self, sctx: SendContext) -> SendResult:
        try:
            await self.send_plain_text(sctx, sctx.recommend_text)
        except Exception as e:
            logger.error(
                f"[DailyAlbum][generic] 发送到 {sctx.session_str} 失败：{e}",
                exc_info=True,
            )
            return SendResult(
                success=False,
                assistant_text_for_history=sctx.recommend_text,
                error=str(e),
            )
        return SendResult(
            success=True,
            assistant_text_for_history=sctx.recommend_text,
        )
