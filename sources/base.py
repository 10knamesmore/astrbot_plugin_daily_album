from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class AlbumInfo:
    album_name: str
    artist: list[str]
    year: str = ""
    genre: list[str] = field(default_factory=list)
    cover_url: str = ""
    description: str = ""
    listen_tip: str = ""
    netease_id: str = ""


class AlbumSource(ABC):
    @property
    @abstractmethod
    def source_name(self) -> str: ...

    @abstractmethod
    async def fetch(
        self,
        prompt: str,
        history: list[AlbumInfo],
    ) -> AlbumInfo | None:
        """根据提示词和历史推荐一张专辑，失败返回 None"""
        ...
