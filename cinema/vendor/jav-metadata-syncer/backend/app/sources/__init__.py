"""Source registry.

每個來源是 clients/ 裡的一個 provider（javbus / javtrailers / missav），
由 ProviderSource 包成統一介面：

    NAME / REQUIRES_KEY / ready() / async search(q) / async full(item_id) / empty()

要新增來源：在 clients/ 加一個 provider（見 clients/base.py），
然後在下面的 _PROVIDERS 註冊即可。
"""
import time
from typing import Any, Dict, List

from ..clients import (
    JavBusProvider, JavTrailersProvider, MissAVProvider, NotFoundError,
)
from .base import empty_detail, movie_to_detail

# (source, CODE) → Movie dict 的 TTL 快取；search 與 full 共用同一次抓取
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL = 1800  # 30 分鐘


class ProviderSource:
    REQUIRES_KEY = False

    def __init__(self, provider_cls):
        self.NAME = provider_cls.name
        self._cls = provider_cls

    def ready(self) -> bool:
        return True

    async def _fetch(self, code: str) -> dict:
        """抓一次 provider 頁面（含 TTL 快取）。NotFoundError 也會被快取。"""
        key = f"{self.NAME}/{code.strip().upper()}"
        item = _CACHE.get(key)
        if item and time.time() - item["ts"] < _CACHE_TTL:
            if item["data"] is None:
                raise NotFoundError(f"{self.NAME}: {code} not found (cached)")
            return item["data"]
        try:
            movie = await self._cls().search(code)
        except NotFoundError:
            _CACHE[key] = {"ts": time.time(), "data": None}
            raise
        data = movie.to_dict()
        _CACHE[key] = {"ts": time.time(), "data": data}
        return data

    async def search(self, q: str, limit: int = 5) -> List[dict]:
        """番號精確查詢 → preview 項目（0 或 1 筆）。"""
        try:
            m = await self._fetch(q)
        except NotFoundError:
            return []
        return [{
            "source": self.NAME,
            "id": m.get("code") or q.strip().upper(),
            "title_cn": "",
            "title_native": m.get("title") or "",
            "title_english": "",
            "year": (m.get("release_date") or "")[:4],
            "url": m.get("source_url") or "",
            "cover": m.get("cover_url") or "",
            "score": 100.0,
            "overview": (m.get("description") or "")[:200],
            "aliases": [],
            "hint": "",
        }]

    async def full(self, item_id: str, hint: str = "") -> dict:
        """canonical detail；找不到時回 empty()（id 為空代表 miss）。"""
        try:
            m = await self._fetch(item_id)
        except NotFoundError:
            return self.empty()
        return movie_to_detail(m, self.NAME)

    def empty(self) -> dict:
        return empty_detail(self.NAME)


_PROVIDERS = (JavBusProvider, JavTrailersProvider, MissAVProvider)

SOURCES: Dict[str, ProviderSource] = {
    p.name: ProviderSource(p) for p in _PROVIDERS
}


def get_source(name: str):
    return SOURCES.get((name or "").lower())


def source_names() -> list[str]:
    return list(SOURCES.keys())


def enabled_names() -> list[str]:
    from ..config import settings
    out = [s.strip().lower() for s in settings.enabled_sources.split(",") if s.strip()]
    return [s for s in out if s in SOURCES]


def is_enabled(name: str) -> bool:
    return (name or "").lower() in enabled_names()
