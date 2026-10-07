from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Actress:
    name: str
    image_url: str | None = None


@dataclass
class Movie:
    provider: str
    code: str
    title: str
    cover_url: str | None = None
    thumb_url: str | None = None
    release_date: str | None = None
    runtime_minutes: int | None = None
    studio: str | None = None
    label: str | None = None
    series: str | None = None
    director: str | None = None
    description: str | None = None
    actresses: list[Actress] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    sample_images: list[str] = field(default_factory=list)
    source_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProviderError(Exception):
    pass


class NotFoundError(ProviderError):
    pass


class BaseProvider(ABC):
    name: str = ""
    base_url: str = ""

    @abstractmethod
    async def search(self, code: str) -> Movie:
        """Fetch metadata for the given code. Raise NotFoundError if missing."""
        raise NotImplementedError
