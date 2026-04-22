"""aiocqhttp（QQ / OneBot v11 / NapCat / LLOneBot）平台 sender。

发送策略：先发主文案，再发网易云音乐卡片（type=music, type=163）；
搜不到网易云时改发 LLM 人格化的"去其他平台手搜"提示。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .base import AlbumSender, SendContext, SendResult

DEFAULT_NOT_FOUND_HINT: str = (
    "未能在网易云找到这张专辑，可以去 Spotify / Apple Music / 网易云 / BandCamp 手动搜索哦～"
)


class AioCQHttpSender(AlbumSender):
    """OneBot v11 / aiocqhttp 平台发送实现。"""

    @property
    def platform_type(self) -> str:
        return "aiocqhttp"

    async def send(self, sctx: SendContext) -> SendResult:
        # 1. 主文案：通过统一的 StarTools 发送
        try:
            await self.send_plain_text(sctx, sctx.recommend_text)
        except Exception as e:
            logger.error(
                f"[DailyAlbum][aiocqhttp] 主文案发送到 {sctx.session_str} 失败：{e}",
                exc_info=True,
            )
            return SendResult(
                success=False,
                assistant_text_for_history=sctx.recommend_text,
                error=str(e),
            )

        # 2. 平台特化：音乐卡片或 fallback 提示
        appended_text: str = ""
        if sctx.netease_song_id:
            try:
                await self._send_music_card(sctx, sctx.netease_song_id)
                logger.info(
                    f"[DailyAlbum][aiocqhttp] 音乐卡片已发送到 {sctx.session_str}："
                    f"song_id={sctx.netease_song_id}"
                )
            except Exception as e:
                # 卡片失败不视为整体失败，但记入历史以便追溯
                logger.warning(
                    f"[DailyAlbum][aiocqhttp] 音乐卡片发送失败 ({sctx.session_str})：{e}"
                )
                appended_text = "\n（音乐卡片发送失败，请到平台手动搜索）"
                try:
                    await self.send_plain_text(sctx, appended_text.lstrip())
                except Exception:
                    pass
        else:
            hint: str = await self.generate_not_found_hint(
                sctx, default_hint=DEFAULT_NOT_FOUND_HINT
            )
            appended_text = "\n" + hint
            try:
                await self.send_plain_text(sctx, hint)
            except Exception as e:
                logger.warning(
                    f"[DailyAlbum][aiocqhttp] fallback 提示发送失败 ({sctx.session_str})：{e}"
                )

        return SendResult(
            success=True,
            assistant_text_for_history=sctx.recommend_text + appended_text,
        )

    async def _send_music_card(self, sctx: SendContext, song_id: str) -> None:
        """通过 OneBot v11 的 send_group_msg / send_private_msg 发送 type=music 卡片。"""
        bot = getattr(sctx.platform, "bot", None)
        if bot is None:
            raise RuntimeError("aiocqhttp platform 实例没有 bot 属性")

        session: MessageSession = MessageSession.from_str(sctx.session_str)
        payload: dict[str, Any] = {
            "message": [{"type": "music", "data": {"type": "163", "id": song_id}}]
        }
        if session.message_type == MessageType.GROUP_MESSAGE:
            await bot.api.call_action(
                "send_group_msg",
                group_id=int(session.session_id),
                **payload,
            )
        else:
            await bot.api.call_action(
                "send_private_msg",
                user_id=int(session.session_id),
                **payload,
            )
