"""Emby / Jellyfin 電影 NFO 產生 + 檔案輸出。

電影的 Emby 慣例（與影集不同）：每部片一個資料夾
    output/{CODE}/
    ├── movie.nfo
    ├── poster.jpg        # 裁切後的正面封面
    ├── fanart.jpg        # 完整封面（背+正合圖或原圖）
    └── extrafanart/      # 劇照
        ├── fanart1.jpg ...
"""
from datetime import datetime
from pathlib import Path

import httpx
from lxml import etree

from .config import settings
from .api.image import _REFERER_BY_HOST, _crop_front_cover


def output_base() -> Path:
    return Path(settings.output_dir)


def _sub(parent, tag, text=None, attrib=None, cdata=False):
    el = etree.SubElement(parent, tag, attrib or {})
    if cdata and text:
        el.text = etree.CDATA(text)
    elif text is not None:
        el.text = str(text)
    return el


def generate_movie_nfo(d: dict) -> str:
    """canonical detail → movie.nfo XML 字串。"""
    root = etree.Element("movie")
    code = d.get("code") or d.get("id") or ""
    title = d.get("title") or ""

    _sub(root, "plot", d.get("plot") or "", cdata=True)
    _sub(root, "outline", d.get("plot") or "", cdata=True)
    _sub(root, "lockdata", "false")
    _sub(root, "dateadded", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    # JAV 慣例：title 帶番號前綴方便媒體庫辨識
    _sub(root, "title", f"{code} {title}".strip())
    _sub(root, "originaltitle", title)
    _sub(root, "sorttitle", code)

    for p in d.get("directors") or []:
        _sub(root, "director", p.get("name", ""))

    year = d.get("year") or ""
    if year:
        _sub(root, "year", year)
    premiered = d.get("premiered") or ""
    if premiered:
        _sub(root, "premiered", premiered)
        _sub(root, "releasedate", premiered)
    if d.get("runtime"):
        _sub(root, "runtime", d["runtime"])
    if d.get("studio"):
        _sub(root, "studio", d["studio"])

    # 系列 → Emby 的 boxset
    if d.get("series"):
        set_el = etree.SubElement(root, "set")
        _sub(set_el, "name", d["series"])
    if d.get("label"):
        _sub(root, "tag", d["label"])

    for g in d.get("genres") or []:
        _sub(root, "genre", g)

    if code:
        _sub(root, "num", code)
        _sub(root, "uniqueid", code, {"type": "num", "default": "true"})

    for a in d.get("actors") or []:
        ael = etree.SubElement(root, "actor")
        _sub(ael, "name", a.get("name", ""))
        _sub(ael, "type", "Actor")
        if a.get("thumb"):
            _sub(ael, "thumb", a["thumb"])

    art = etree.SubElement(root, "art")
    _sub(art, "poster", "poster.jpg")
    _sub(art, "fanart", "fanart.jpg")

    xml = etree.tostring(root, encoding="utf-8", xml_declaration=True,
                         pretty_print=True, standalone=True)
    return xml.decode("utf-8")


async def _download(client: httpx.AsyncClient, url: str) -> bytes | None:
    from urllib.parse import urlparse
    host = urlparse(url).netloc
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": _REFERER_BY_HOST.get(host, f"https://{host}/"),
    }
    try:
        r = await client.get(url, headers=headers)
        if r.status_code < 400 and r.content:
            return r.content
    except Exception:
        pass
    return None


async def collect_movie_files(d: dict) -> tuple[str, list[tuple[str, bytes]]]:
    """組出一部片的完整檔案組（不落地）。

    回傳 (safe_code, [(相對路徑, bytes), ...])：
    movie.nfo + poster（裁切）+ fanart + extrafanart 劇照。
    """
    code = (d.get("code") or d.get("id") or "").strip().upper()
    safe = "".join(c for c in code if c not in r'\/:*?"<>|') or "unknown"
    files: list[tuple[str, bytes]] = [
        ("movie.nfo", generate_movie_nfo(d).encode("utf-8")),
    ]

    cover = (d.get("images") or {}).get("poster") or ""
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        if cover:
            data = await _download(client, cover)
            if data:
                files.append(("fanart.jpg", data))
                front, _ = _crop_front_cover(data)
                files.append(("poster.jpg", front))
        for i, u in enumerate(d.get("sample_images") or [], 1):
            data = await _download(client, u)
            if data:
                files.append((f"extrafanart/fanart{i}.jpg", data))

    return safe, files


async def export_movie(d: dict) -> dict:
    """輸出一部片到 output/{CODE}/。

    回傳 {output, files}。同番號重複輸出會覆寫（以最後一次的來源為準）。
    """
    safe, file_pairs = await collect_movie_files(d)
    out_dir = output_base() / safe
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for rel, data in file_pairs:
        target = out_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(rel)
    return {"output": str(out_dir.resolve()), "files": written}
