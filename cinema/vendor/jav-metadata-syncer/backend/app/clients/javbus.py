from __future__ import annotations

import re
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from .base import Actress, BaseProvider, Movie, NotFoundError, ProviderError


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8,ja;q=0.7",
}
# `existmag=all` lifts the censored/uncensored age gate so info blocks render fully.
_COOKIES = {"existmag": "all", "age": "verified"}


class JavBusProvider(BaseProvider):
    name = "javbus"
    base_url = "https://www.javbus.com"

    def __init__(self, base_url: str | None = None, timeout: float = 15.0):
        if base_url:
            self.base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def search(self, code: str) -> Movie:
        code = code.strip().upper()
        url = f"{self.base_url}/{code}"
        async with httpx.AsyncClient(
            headers=_HEADERS,
            cookies=_COOKIES,
            timeout=self._timeout,
            follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            if resp.status_code == 404:
                raise NotFoundError(f"javbus: {code} not found")
            if resp.status_code >= 400:
                raise ProviderError(f"javbus: HTTP {resp.status_code}")
            return self._parse(code, url, resp.text)

    def _parse(self, code: str, source_url: str, html: str) -> Movie:
        soup = BeautifulSoup(html, "lxml")
        container = soup.select_one("div.container") or soup
        info = container.select_one("div.info")

        title = self._title(container, code)
        cover = self._cover(container)
        details = self._info_dict(info) if info else {}

        movie = Movie(
            provider=self.name,
            code=details.get("code") or code,
            title=title,
            cover_url=cover,
            thumb_url=cover,
            release_date=details.get("release_date"),
            runtime_minutes=self._parse_runtime(details.get("runtime")),
            director=details.get("director"),
            studio=details.get("studio"),
            label=details.get("label"),
            series=details.get("series"),
            genres=self._genres(info) if info else [],
            actresses=self._actresses(container),
            sample_images=self._samples(soup),
            source_url=source_url,
        )
        if not movie.title:
            raise NotFoundError(f"javbus: empty page for {code}")
        return movie

    # ── parsing helpers ──────────────────────────────────────────────────

    def _title(self, root: Tag, code: str) -> str:
        h = root.select_one("h3")
        if not h:
            return ""
        text = h.get_text(" ", strip=True)
        # JavBus prepends the code; drop it for a cleaner title.
        return re.sub(rf"^{re.escape(code)}\s*", "", text, flags=re.IGNORECASE)

    def _cover(self, root: Tag) -> str | None:
        img = root.select_one("a.bigImage img")
        if not img:
            return None
        src = img.get("src") or img.get("data-src")
        return urljoin(self.base_url + "/", src) if src else None

    _LABEL_MAP = {
        "識別碼": "code",
        "発行日期": "release_date",
        "發行日期": "release_date",
        "発売日": "release_date",
        "長度": "runtime",
        "収録時間": "runtime",
        "導演": "director",
        "監督": "director",
        "製作商": "studio",
        "メーカー": "studio",
        "發行商": "label",
        "レーベル": "label",
        "系列": "series",
        "シリーズ": "series",
    }

    def _info_dict(self, info: Tag) -> dict[str, str]:
        out: dict[str, str] = {}
        for p in info.find_all("p", recursive=False):
            header = p.find("span", class_="header")
            if not header:
                continue
            label = header.get_text(strip=True).rstrip(":：")
            key = self._LABEL_MAP.get(label)
            if not key:
                continue
            # Value = paragraph text minus the header label.
            value = p.get_text(" ", strip=True)
            value = re.sub(rf"^{re.escape(label)}[:：]?\s*", "", value).strip()
            if value:
                out[key] = value
        return out

    @staticmethod
    def _parse_runtime(raw: str | None) -> int | None:
        if not raw:
            return None
        m = re.search(r"(\d+)", raw)
        return int(m.group(1)) if m else None

    def _genres(self, info: Tag) -> list[str]:
        # Genres live in a sibling <p class="genre"> block, OR inline genre links.
        nodes = info.select("p.genre a, span.genre a")
        return [a.get_text(strip=True) for a in nodes if a.get_text(strip=True)]

    def _actresses(self, root: Tag) -> list[Actress]:
        out: list[Actress] = []
        for box in root.select("a.avatar-box"):
            name_tag = box.select_one("span")
            img = box.select_one("img")
            name = name_tag.get_text(strip=True) if name_tag else ""
            if not name:
                continue
            image = img.get("src") if img else None
            out.append(
                Actress(
                    name=name,
                    image_url=urljoin(self.base_url + "/", image) if image else None,
                )
            )
        return out

    def _samples(self, soup: BeautifulSoup) -> list[str]:
        nodes = soup.select("#sample-waterfall a.sample-box")
        urls: list[str] = []
        for a in nodes:
            href = a.get("href")
            if href:
                urls.append(urljoin(self.base_url + "/", href))
        return urls
