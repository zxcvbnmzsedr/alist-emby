from __future__ import annotations

from .base import BaseProvider, Movie, Actress, ProviderError, NotFoundError
from .javbus import JavBusProvider
from .javtrailers import JavTrailersProvider
from .missav import MissAVProvider

PROVIDERS: dict[str, type[BaseProvider]] = {
    JavBusProvider.name: JavBusProvider,
    JavTrailersProvider.name: JavTrailersProvider,
    MissAVProvider.name: MissAVProvider,
}


def get_provider(name: str) -> BaseProvider:
    cls = PROVIDERS.get(name.lower())
    if cls is None:
        raise KeyError(f"unknown provider: {name}")
    return cls()


def list_providers() -> list[str]:
    return sorted(PROVIDERS.keys())


__all__ = [
    "BaseProvider",
    "Movie",
    "Actress",
    "ProviderError",
    "NotFoundError",
    "PROVIDERS",
    "get_provider",
    "list_providers",
]
