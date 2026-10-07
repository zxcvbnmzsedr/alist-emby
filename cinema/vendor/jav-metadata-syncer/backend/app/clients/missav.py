from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag
from curl_cffi.requests import AsyncSession

from .base import Actress, BaseProvider, Movie, NotFoundError, ProviderError


class MissAVProvider(BaseProvider):
    """MissAV is behind Cloudflare; requires browser TLS impersonation
    via curl_cffi. Code URLs auto-redirect from `/<code>` to `/dmXX/<code>`.
    """

    name = "missav"
    base_url = "https://missav.ai"

    def __init__(self, base_url: str | None = None, timeout: float = 20.0,
                 impersonate: str = "chrome124"):
        if base_url:
            self.base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._impersonate = impersonate

    async def search(self, code: str) -> Movie:
        code = code.strip().lower()
        url = f"{self.base_url}/{code}"
        async with AsyncSession() as session:
            try:
                r = await session.get(
                    url,
                    impersonate=self._impersonate,
                    timeout=self._timeout,
                    allow_redirects=True,
                )
            except Exception as e:
                raise ProviderError(f"missav: request failed: {e}") from e
        if r.status_code == 404:
            raise NotFoundError(f"missav: {code} not found")
        if r.status_code >= 400:
            raise ProviderError(f"missav: HTTP {r.status_code}")
        return self._parse(code, str(r.url), r.text)

    # ── parsing ─────────────────────────────────────────────────────────

    def _parse(self, code: str, source_url: str, html: str) -> Movie:
        soup = BeautifulSoup(html, "lxml")
        og = self._og_tags(soup)

        # If we landed on the homepage (no og:type=video.*), the code is missing.
        og_type = og.get("og:type", "")
        if not og_type.startswith("video"):
            raise NotFoundError(f"missav: {code} not found (no video page)")

        title = self._strip_code(og.get("og:title", ""), code)
        actresses = [Actress(name=a) for a in og.get_all("og:video:actor")]
        secondary = self._secondary_dict(soup)

        movie = Movie(
            provider=self.name,
            code=secondary.get("code") or code.upper(),
            title=title,
            cover_url=og.get("og:image"),
            thumb_url=og.get("og:image"),
            release_date=og.get("og:video:release_date") or secondary.get("release_date"),
            runtime_minutes=self._duration_to_minutes(og.get("og:video:duration")),
            director=og.get("og:video:director") or secondary.get("director"),
            studio=secondary.get("studio"),
            label=secondary.get("label"),
            series=secondary.get("series"),
            description=og.get("og:description"),
            actresses=actresses,
            genres=self._genres(soup),
            sample_images=[],  # MissAV's gallery is JS-rendered; skip
            source_url=source_url,
        )
        if not movie.title:
            raise NotFoundError(f"missav: empty page for {code}")
        return movie

    # ── helpers ─────────────────────────────────────────────────────────

    class _OG:
        def __init__(self, items: list[tuple[str, str]]):
            self._items = items

        def get(self, key: str, default: str | None = None) -> str | None:
            for k, v in self._items:
                if k == key:
                    return v
            return default

        def get_all(self, key: str) -> list[str]:
            return [v for k, v in self._items if k == key]

    def _og_tags(self, soup: BeautifulSoup) -> "MissAVProvider._OG":
        items: list[tuple[str, str]] = []
        for m in soup.find_all("meta", property=True):
            prop = m.get("property") or ""
            content = m.get("content") or ""
            if prop.startswith("og:") and content:
                items.append((prop, content))
        return self._OG(items)

    @staticmethod
    def _strip_code(title: str, code: str) -> str:
        # og:title is "CODE Japanese title …" — drop the leading code.
        return re.sub(rf"^{re.escape(code)}\s*", "", title.strip(), flags=re.IGNORECASE)

    @staticmethod
    def _duration_to_minutes(value: str | None) -> int | None:
        if not value:
            return None
        try:
            return max(1, int(int(value) / 60))
        except ValueError:
            return None

    _SECONDARY_LABELS = {
        "發行日期": "release_date",
        "発売日": "release_date",
        "番號": "code",
        "番号": "code",
        "片長": "runtime",
        "時長": "runtime",
        "導演": "director",
        "監督": "director",
        "製作商": "studio",
        "メーカー": "studio",
        "發行商": "label",
        "レーベル": "label",
        "系列": "series",
        "シリーズ": "series",
    }

    def _secondary_dict(self, soup: BeautifulSoup) -> dict[str, str]:
        out: dict[str, str] = {}
        for div in soup.select("div.text-secondary"):
            text = div.get_text(" ", strip=True)
            for label, key in self._SECONDARY_LABELS.items():
                if text.startswith(label):
                    value = text[len(label):].lstrip(":：").strip()
                    # Drop label-name leftovers from <a> texts
                    if value:
                        out.setdefault(key, value)
                    break
        return out

    def _genres(self, soup: BeautifulSoup) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            path = urlparse(href).path
            # /genres/<slug> on MissAV are tag pages
            if "/genres/" in path:
                name = (a.get_text() or "").strip()
                if name and name not in seen and len(name) <= 20:
                    seen.add(name)
                    out.append(name)
        return out
