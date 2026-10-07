"""Full metadata: canonical-shape details for the queried code, per source.

Response shape（與 comic / show-metadata-syncer 一致）:
    {
      "query": "SSIS-001",
      "sources": [
        { "source": "javbus", "id": "SSIS-001", "match_score": 100, ... },
        ...
      ]
    }

A source with zero hits still contributes one empty entry so the client
knows the source was tried.
"""
import asyncio

from fastapi import APIRouter, HTTPException

from ..sources import get_source, source_names, enabled_names, is_enabled

router = APIRouter(prefix="/api/metadata", tags=["metadata"])


@router.get("", operation_id="metadata_search")
async def metadata(q: str, source: str = "all"):
    if source == "all":
        mods = [get_source(n) for n in enabled_names()]
    else:
        if get_source(source) and not is_enabled(source):
            raise HTTPException(400, f"來源 {source} 已停用（設定頁可重新啟用）")
        mods = [m for m in [get_source(source)] if m]
        if not mods:
            raise HTTPException(400, f"unknown source: {source!r} (expected: all, {', '.join(source_names())})")

    async def _one(mod):
        try:
            return await mod.full(q)
        except Exception:
            return mod.empty()

    details = await asyncio.gather(*(_one(m) for m in mods))
    details = sorted(details, key=lambda d: d["match_score"], reverse=True)
    return {"query": q, "sources": details}


@router.get("/{source}/{item_id}", operation_id="metadata_by_id", summary="Metadata by code")
async def metadata_by_id(source: str, item_id: str):
    """按 (source, 番號) 直取單筆 canonical detail（無 sources 包裝）。

    例如: GET /api/metadata/javbus/SSIS-001
    """
    mod = get_source(source)
    if mod is None:
        raise HTTPException(400, f"unknown source: {source!r} (expected: {', '.join(source_names())})")
    if not is_enabled(source):
        raise HTTPException(400, f"來源 {source} 已停用（設定頁可重新啟用）")
    detail = await mod.full(item_id)
    if not detail.get("id"):
        raise HTTPException(404, f"{source}: {item_id} not found")
    return detail
