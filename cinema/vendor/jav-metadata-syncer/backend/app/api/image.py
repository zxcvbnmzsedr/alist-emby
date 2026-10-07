"""Server-side image proxy.

來源站的圖多有防盜連（檢查 Referer），前端無法直接 <img src>。
這個 proxy 按 host 帶上正確 Referer；`crop=cover` 會把 DVD 封面
橫向合圖（背面+正面）裁出右半的正面。
"""
import io
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response
from PIL import Image

router = APIRouter(prefix="/api/image", tags=["image"])

_REFERER_BY_HOST = {
    "www.javbus.com": "https://www.javbus.com/",
    "javbus.com": "https://www.javbus.com/",
    "pics.dmm.co.jp": "https://www.dmm.co.jp/",
    "fourhoi.com": "https://missav.ai/",
    "www.fourhoi.com": "https://missav.ai/",
    "javtrailers.com": "https://javtrailers.com/",
    "cdn.javtrailers.com": "https://javtrailers.com/",
    "images.javtrailers.com": "https://javtrailers.com/",
    "pics.vpdmm.cc": "https://javtrailers.com/",
}
_ALLOWED_IMAGE_HOSTS = set(_REFERER_BY_HOST)


def _crop_front_cover(data: bytes) -> tuple[bytes, str]:
    """If the image looks like a DVD jacket spread (back+front side-by-side),
    return only the right half (front cover). Detected by aspect ratio > 1.4."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if w / h <= 1.4:
                return data, "image/jpeg"
            front = im.crop((w // 2, 0, w, h))
            buf = io.BytesIO()
            front.convert("RGB").save(buf, format="JPEG", quality=92)
            return buf.getvalue(), "image/jpeg"
    except Exception:
        return data, "image/jpeg"


@router.get("", operation_id="image_proxy")
async def image_proxy(
    url: str = Query(..., description="absolute image URL to proxy"),
    crop: str | None = Query(None, description="'cover' to auto-crop DVD jacket front"),
):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or parsed.netloc not in _ALLOWED_IMAGE_HOSTS:
        raise HTTPException(status_code=400, detail="host not allowed")
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": _REFERER_BY_HOST.get(parsed.netloc, f"{parsed.scheme}://{parsed.netloc}/"),
    }
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        r = await client.get(url, headers=headers)
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail="upstream error")

    content = r.content
    media_type = r.headers.get("content-type", "image/jpeg")
    if crop == "cover":
        content, media_type = _crop_front_cover(content)

    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
