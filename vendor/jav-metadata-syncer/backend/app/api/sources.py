"""列出平台目前的 metadata 來源與其狀態（與 comic / show 同 schema）。"""
from fastapi import APIRouter
from pydantic import BaseModel

from ..sources import SOURCES, is_enabled

router = APIRouter(prefix="/api/sources", tags=["sources"])


class SourceStatus(BaseModel):
    name: str
    enabled: bool        # 設定頁的啟用開關（停用的來源不參與搜尋）
    ready: bool          # 現在就能查
    requires_key: bool   # 是否需要 API key
    nfo_crawl: bool      # 是否支援 NFO 輸出（POST /api/export）


@router.get("", response_model=list[SourceStatus], operation_id="list_sources")
def list_sources():
    return [
        SourceStatus(
            name=name,
            enabled=is_enabled(name),
            ready=mod.ready(),
            requires_key=mod.REQUIRES_KEY,
            nfo_crawl=True,
        )
        for name, mod in SOURCES.items()
    ]
