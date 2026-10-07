"""NFO 產生 + 檔案輸出（電影版，同步執行 — 單片只需幾秒）。

POST /api/nfo/movie  → 回 movie.nfo XML 字串（不寫檔）
GET  /api/export.zip → 打包成 ZIP 直接下載（前端按鈕用這個）
POST /api/export     → 寫出 output/{CODE}/（伺服器端落地）
"""
import io
import zipfile

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, Response
from pydantic import BaseModel

from ..nfo import generate_movie_nfo, export_movie, collect_movie_files
from ..sources import get_source, source_names, is_enabled

router = APIRouter(prefix="/api", tags=["nfo"])


class ExportRequest(BaseModel):
    source: str
    code: str


async def _detail_or_404(source: str, code: str) -> dict:
    mod = get_source(source)
    if mod is None:
        raise HTTPException(400, f"unknown source: {source!r} (expected: {', '.join(source_names())})")
    if not is_enabled(source):
        raise HTTPException(400, f"來源 {source} 已停用（設定頁可重新啟用）")
    d = await mod.full(code)
    if not d.get("id"):
        raise HTTPException(404, f"{source}: {code} not found")
    return d


@router.post("/nfo/movie", operation_id="generate_movie_nfo_api", response_class=PlainTextResponse)
async def gen_movie_nfo(req: ExportRequest):
    """產生 movie.nfo 的 XML 字串（不寫檔）。"""
    d = await _detail_or_404(req.source, req.code)
    return PlainTextResponse(generate_movie_nfo(d), media_type="application/xml")


@router.get("/export.zip", operation_id="export_movie_zip")
async def export_zip(source: str, code: str):
    """把整套輸出（movie.nfo + poster + fanart + extrafanart）打包成 ZIP 下載。

    ZIP 內是一層 {CODE}/ 資料夾，解壓即為 Emby 電影資料夾。
    """
    d = await _detail_or_404(source, code)
    safe, file_pairs = await collect_movie_files(d)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, data in file_pairs:
            z.writestr(f"{safe}/{rel}", data)
    return Response(
        buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe}.zip"'},
    )


@router.post("/export", operation_id="export_movie_files")
async def export(req: ExportRequest):
    """輸出 Emby 電影資料夾：movie.nfo + poster.jpg（裁切）+ fanart.jpg + extrafanart/。

    同番號重複輸出會覆寫（以最後一次選的來源為準）。
    """
    d = await _detail_or_404(req.source, req.code)
    result = await export_movie(d)
    return {"ok": True, "source": req.source, "code": d.get("code"), **result}
