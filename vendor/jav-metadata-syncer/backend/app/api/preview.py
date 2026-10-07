"""Lightweight preview: title + cover + code, per source.

番號是精確查詢，所以 preview 每來源最多 1 筆（命中 score=100）。
結果有 30 分鐘快取，接著呼叫 /api/metadata 不會重抓。
"""
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..sources import get_source, enabled_names, is_enabled

router = APIRouter(prefix="/api/preview", tags=["preview"])


class PreviewItem(BaseModel):
    source: str
    id: str
    title_cn: str | None = None
    title_native: str | None = None
    title_english: str | None = None
    year: str | None = None
    url: str | None = None
    cover: str | None = None
    score: float = 0
    overview: str = ""
    aliases: list[str] = []
    hint: str = ""


@router.get("", response_model=list[PreviewItem], operation_id="preview_search")
async def preview(q: str, source: str = "all"):
    """以番號查詢候選。source 可為 all 或單一來源代號。"""
    if source == "all":
        mods = [get_source(n) for n in enabled_names()]
    else:
        if get_source(source) and not is_enabled(source):
            raise HTTPException(400, f"來源 {source} 已停用（設定頁可重新啟用）")
        mods = [m for m in [get_source(source)] if m]
    results = await asyncio.gather(
        *(m.search(q) for m in mods), return_exceptions=True
    )
    hits: list[PreviewItem] = []
    for items in results:
        if isinstance(items, BaseException):
            continue
        hits.extend(PreviewItem(**it) for it in items)
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits
