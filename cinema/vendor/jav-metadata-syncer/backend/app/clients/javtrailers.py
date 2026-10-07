from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession

from .base import Actress, BaseProvider, Movie, NotFoundError, ProviderError

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class JavTrailersProvider(BaseProvider):
    name = "javtrailers"
    base_url = "https://javtrailers.com"

    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        if base_url:
            self.base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def search(self, code: str) -> Movie:
        code = code.strip().upper()
        search_url = f"{self.base_url}/ja/search/{code}"

        try:
            async with AsyncSession(impersonate="chrome124") as s:
                r = await s.get(search_url, timeout=self._timeout,
                                headers={"User-Agent": _UA})
        except Exception as e:
            raise ProviderError(f"javtrailers: fetch failed: {e}") from e

        soup = BeautifulSoup(r.text, "lxml")

        video_url = self._pick_from_results(soup, code)
        if not video_url:
            raise NotFoundError(f"javtrailers: {code} not found")

        try:
            async with AsyncSession(impersonate="chrome124") as s:
                r2 = await s.get(video_url, timeout=self._timeout,
                                 headers={"User-Agent": _UA})
        except Exception as e:
            raise ProviderError(f"javtrailers: video page fetch failed: {e}") from e

        return self._parse(code, video_url, r2.text)

    # ── parsing helpers ────────────────────────────────────────────────────

    def _pick_from_results(self, soup: BeautifulSoup, code: str) -> str | None:
        for title_el in soup.select("p.vid-title"):
            if code in title_el.get_text(strip=True).upper():
                # The <a> wraps the card as an ancestor, not a descendant
                a = title_el.find_parent("a", href=re.compile(r"/video/"))
                if a:
                    return urljoin(self.base_url, str(a["href"]))
        return None

    def _parse(self, code: str, source_url: str, html: str) -> Movie:
        soup = BeautifulSoup(html, "lxml")

        og_image = soup.select_one('meta[property="og:image"]')
        cover_url = og_image.get("content") if og_image else None

        h1 = soup.select_one("h1")
        title = h1.get_text(strip=True) if h1 else ""
        title = re.sub(rf"^{re.escape(code)}\s*", "", title, flags=re.IGNORECASE).strip()
        if not title:
            raise NotFoundError(f"javtrailers: empty page for {code}")

        def field(label: str) -> str | None:
            """Return the text value of the first <p class="mb-1"> whose label matches."""
            for p in soup.select("p.mb-1"):
                span = p.select_one("span.font-weight-bold")
                if span and label in span.get_text(strip=True):
                    raw = p.get_text(" ", strip=True)
                    prefix = span.get_text(strip=True)
                    val = raw[len(prefix):].strip()
                    return val or None
            return None

        release_date = None
        raw_date = field("商品発売日")
        if raw_date:
            try:
                release_date = datetime.strptime(raw_date, "%d %b %Y").strftime("%Y-%m-%d")
            except ValueError:
                release_date = raw_date

        runtime_minutes = None
        raw_dur = field("収録時間")
        if raw_dur:
            m = re.search(r"(\d+)", raw_dur)
            runtime_minutes = int(m.group(1)) if m else None

        director = field("監督")
        studio_raw = field("メーカー")
        # field() returns all text; for linked values that's correct
        studio = studio_raw
        series = field("シリーズ")
        page_code = field("DVD ID") or code

        # ジャンル: links inside the genres <p>
        genres: list[str] = []
        for p in soup.select("p.mb-1"):
            span = p.select_one("span.font-weight-bold")
            if span and "ジャンル" in span.get_text(strip=True):
                genres = [
                    a.get_text(strip=True)
                    for a in p.select("a[href*='/categories/']")
                    if a.get_text(strip=True)
                ]
                break

        # 出演者: links inside the cast <p>
        actresses: list[Actress] = []
        for p in soup.select("p.mb-1"):
            span = p.select_one("span.font-weight-bold")
            if span and "出演者" in span.get_text(strip=True):
                for a in p.select("a[href*='/casts/']"):
                    name = a.get_text(strip=True)
                    if name:
                        actresses.append(Actress(name=name, image_url=None))
                break

        return Movie(
            provider=self.name,
            code=page_code,
            title=title,
            cover_url=cover_url,
            thumb_url=cover_url,
            release_date=release_date,
            runtime_minutes=runtime_minutes,
            director=director,
            studio=studio,
            label=None,
            series=series,
            genres=genres,
            actresses=actresses,
            sample_images=[],
            source_url=source_url,
        )
