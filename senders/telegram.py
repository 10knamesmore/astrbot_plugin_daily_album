"""Telegram 平台 sender。

MVP 策略：单条消息 = 推荐文案 + 网易云直链（或 fallback 提示）。
不调 ``platform.client.send_message`` 直发，而是走 ``StarTools.send_message`` →
``TelegramPlatformEvent.send_with_client``，复用其 4096 切分与 MarkdownV2 降级。
"""

from __future__ import annotations

from astrbot.api import logger

from ..utils.netease import NETEASE_SONG_URL_TEMPLATE
from .base import AlbumSender, SendContext, SendResult

DEFAULT_NOT_FOUND_HINT: str = "没找到网易云链接，可以去 Spotify / Apple Music 手搜~"


class TelegramSender(AlbumSender):
    """Telegram 平台发送实现（python-telegram-bot v20+）。"""

    @property
    def platform_type(self) -> str:
        return "telegram"

    async def send(self, sctx: SendContext) -> SendResult:
        # 拼装尾部内容：网易云链接（找到时） / fallback 提示（找不到时）
        suffix: str
        if sctx.netease_song_id:
            suffix = "\n\n" + NETEASE_SONG_URL_TEMPLATE.format(
                song_id=sctx.netease_song_id
            )
        else:
            hint: str = await self.generate_not_found_hint(
                sctx, default_hint=DEFAULT_NOT_FOUND_HINT
            )
            suffix = "\n\n" + hint

        full_text: str = sctx.recommend_text + suffix
        try:
            await self.send_plain_text(sctx, full_text)
        except Exception as e:
            logger.error(
                f"[DailyAlbum][telegram] 发送到 {sctx.session_str} 失败：{e}",
                exc_info=True,
            )
            return SendResult(
                success=False,
                assistant_text_for_history=full_text,
                error=str(e),
            )

        return SendResult(
            success=True,
            assistant_text_for_history=full_text,
        )
