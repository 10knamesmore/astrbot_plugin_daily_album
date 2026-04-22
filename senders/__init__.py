"""sender 工厂：UMO → (sender 实例, platform 实例, platform_type)。

按 ``platform.meta().name`` 选 sender 类（不是 ``id``）。三级 fallback：

1. UMO 解析失败或 platform_insts 找不到对应实例 → 返回 ``None``，
   orchestrator 决定 warn + skip。
2. 找到 platform 但 ``platform_type`` 不在注册表 → 用 :py:class:`GenericSender`
   保证主文案至少能发到。
3. sender 内部异常 → sender 自己 catch 后返回失败 :py:class:`SendResult`。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.core.platform.message_session import MessageSession

from .aiocqhttp import AioCQHttpSender
from .base import AlbumSender, SendContext, SendResult
from .generic import GenericSender
from .telegram import TelegramSender

__all__ = [
    "AlbumSender",
    "SendContext",
    "SendResult",
    "select_sender",
]


# 平台类型 (platform.meta().name) → AlbumSender 子类
_SENDER_CLASSES: dict[str, type[AlbumSender]] = {
    "aiocqhttp": AioCQHttpSender,
    "telegram": TelegramSender,
}

# Sender 是无状态的，全局复用单例减少分配
_INSTANCES: dict[str, AlbumSender] = {}


def _get_sender_for_type(platform_type: str) -> AlbumSender:
    """按平台类型取（或惰性建）sender 单例；未注册时回退到 GenericSender。"""
    cls: type[AlbumSender] = _SENDER_CLASSES.get(platform_type, GenericSender)
    cache_key: str = cls.__name__
    inst: AlbumSender | None = _INSTANCES.get(cache_key)
    if inst is None:
        inst = cls()
        _INSTANCES[cache_key] = inst
    return inst


def select_sender(
    ctx: Context,
    session_str: str,
) -> tuple[AlbumSender, Any, str] | None:
    """根据 UMO 解析平台并选出对应 sender。

    Returns:
        ``(sender, platform_instance, platform_type)`` 或 ``None``（无法路由）。
    """
    try:
        session: MessageSession = MessageSession.from_str(session_str)
    except Exception as e:
        logger.warning(f"[DailyAlbum] 无法解析 UMO {session_str!r}：{e}")
        return None

    platform: Any = None
    for p in ctx.platform_manager.platform_insts:
        if p.meta().id == session.platform_name:
            platform = p
            break
    if platform is None:
        logger.warning(
            f"[DailyAlbum] platform_insts 中找不到 id={session.platform_name!r}，"
            f"该 session ({session_str}) 将被跳过"
        )
        return None

    platform_type: str = platform.meta().name
    sender: AlbumSender = _get_sender_for_type(platform_type)
    if platform_type not in _SENDER_CLASSES:
        logger.info(
            f"[DailyAlbum] 平台类型 {platform_type!r} 未注册专用 sender，"
            f"使用 GenericSender 兜底（仅纯文本）"
        )
    return sender, platform, platform_type
