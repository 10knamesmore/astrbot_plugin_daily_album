"""平台无关的发送策略基础设施。

每个平台一个 AlbumSender 子类，负责把 SendContext 里的"album + 推荐文案 +
预查好的网易云 song id"按平台能力组装成消息发送出去。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.api.provider import LLMResponse
from astrbot.api.star import Context, StarTools

from ..sources.base import AlbumInfo


@dataclass(frozen=True)
class SendContext:
    """单次发送所需的所有只读上下文。Orchestrator 在 per-session 循环里实例化并传入。"""

    album: AlbumInfo
    session_str: str
    """目标 UMO（unified_msg_origin），形如 ``platform_id:message_type:session_id``。"""
    platform: Any
    """平台适配器实例，从 ``ctx.platform_manager.platform_insts`` 解析得到。"""
    platform_type: str
    """平台**类型**字符串（``platform.meta().name``），如 ``aiocqhttp`` / ``telegram``。"""
    recommend_text: str
    """Orchestrator 已生成好的主推荐文案（per-session 人格化）。"""
    netease_song_id: str | None
    """Orchestrator 共享查询出的网易云首歌 ID；找不到为 None。"""
    config: dict[str, Any]
    """插件原始配置字典，sender 可读取自己关心的可选项。"""
    ctx: Context
    """AstrBot Context，sender 需要调 LLM / provider 时用。"""


@dataclass(frozen=True)
class SendResult:
    """发送结果。orchestrator 据此决定是否记日志、写历史。"""

    success: bool
    assistant_text_for_history: str
    """实际落到对话历史的完整 assistant 文本（主文案 + 平台特化追加内容）。"""
    error: str | None = None


class AlbumSender(ABC):
    """平台相关的专辑消息发送策略。一个 sender 对应一种 ``platform.meta().name``。

    子类只需实现 :pyattr:`platform_type` 与 :py:meth:`send`；
    :py:meth:`send_plain_text` 与 :py:meth:`generate_not_found_hint`
    提供基类默认实现，子类按需覆盖。
    """

    @property
    @abstractmethod
    def platform_type(self) -> str:
        """该 sender 负责的平台类型字符串，必须等于 ``platform.meta().name``。"""

    @abstractmethod
    async def send(self, sctx: SendContext) -> SendResult:
        """执行完整的单 session 发送：主文案 + 平台特化附加 + fallback 提示。

        实现方负责所有错误兜底，自身不抛异常；用 :py:class:`SendResult` 报告结果。
        不要在这里写历史，由 orchestrator 统一处理。
        """

    async def send_plain_text(self, sctx: SendContext, text: str) -> None:
        """走 ``StarTools.send_message`` 发一条 Plain MessageChain。

        所有平台都能用：MessageChain 是平台无关的标准组件，由各平台
        ``send_by_session`` 自行翻译。子类一般不用覆盖。
        """
        chain: MessageChain = MessageChain().message(text)
        await StarTools.send_message(sctx.session_str, chain)

    async def generate_not_found_hint(
        self,
        sctx: SendContext,
        *,
        default_hint: str,
    ) -> str:
        """LLM 人格化生成"没找到，可以去别的平台手搜"提示。

        失败 / 无 provider 时回退到 ``default_hint``。Prompt 模板里嵌入了
        当前会话的人格描述，让提示与 bot 的口吻一致。
        """
        provider = sctx.ctx.get_using_provider()
        if not provider:
            return default_hint

        umo: str = sctx.session_str
        try:
            _, persona, _, _ = (
                await sctx.ctx.persona_manager.resolve_selected_persona(
                    umo=umo,
                    conversation_persona_id=None,
                    platform_name=umo.split(":", 1)[0],
                )
            )
        except Exception as e:
            logger.warning(f"[DailyAlbum] resolve_selected_persona 失败：{e}")
            persona = None
        persona_prompt: str = (persona or {}).get("prompt", "")

        artist: list[str] = sctx.album.artist
        try:
            resp: LLMResponse = await sctx.ctx.llm_generate(
                chat_provider_id=provider.meta().id,
                prompt=(
                    f"今日推荐的专辑是《{sctx.album.album_name}》，"
                    f"艺术家：{', '.join(artist)}。"
                    f"在当前平台没有找到这张专辑的可点链接。"
                    "请用你自己的风格，告知用户没有找到，可以去其他平台自行搜索。"
                    "直接输出这句话，不要加任何前缀或解释。"
                ),
                system_prompt=persona_prompt or "你是一个热爱音乐的推荐者。",
            )
            return resp.completion_text.strip() or default_hint
        except Exception as e:
            logger.warning(f"[DailyAlbum] not_found_hint 生成失败，回退默认：{e}")
            return default_hint
