"""Canonical schema shared by every metadata source（成人影片版）.

與 comic / show-metadata-syncer 同一套設計：每個來源回傳同形狀的
canonical detail，空值規則 str -> ""、num -> None、list -> []。

這個平台的查詢單位是「番號」（如 SSIS-001），id 即番號本身；
來源命中即 match_score=100（番號是精確比對，不做 fuzzy）。
"""

CANONICAL_FIELDS = (
    "source", "id", "url",
    "match_score",
    "media_type",               # movie
    "code",                     # 番號
    "title", "original_title",
    "plot",
    "year", "premiered",
    "runtime",                  # 分鐘（字串）
    "studio", "label", "series",
    "directors",                # [{name}]
    "actors",                   # [{name, role, type, thumb}]
    "genres", "tags",
    "rating",
    "unique_ids",               # {code}
    "images",                   # {poster, fanart, thumb}
    "sample_images",
    "trailers",
)


def empty_detail(source: str) -> dict:
    return {
        "source": source,
        "id": "",
        "url": "",
        "match_score": 0,
        "media_type": "movie",
        "code": "",
        "title": "",
        "original_title": "",
        "plot": "",
        "year": "",
        "premiered": "",
        "runtime": "",
        "studio": "",
        "label": "",
        "series": "",
        "directors": [],
        "actors": [],
        "genres": [],
        "tags": [],
        "rating": {"score": None, "votes": None},
        "unique_ids": {"code": ""},
        "images": {"poster": "", "fanart": "", "thumb": ""},
        "sample_images": [],
        "trailers": [],
    }


def movie_to_detail(m: dict, source: str) -> dict:
    """clients 的 Movie dict → canonical detail。"""
    code = m.get("code") or ""
    release = m.get("release_date") or ""
    d = empty_detail(source)
    d.update({
        "id": code,
        "url": m.get("source_url") or "",
        "match_score": 100,
        "code": code,
        "title": m.get("title") or "",
        "plot": m.get("description") or "",
        "year": release[:4] if len(release) >= 4 and release[:4].isdigit() else "",
        "premiered": release,
        "runtime": str(m.get("runtime_minutes") or ""),
        "studio": m.get("studio") or "",
        "label": m.get("label") or "",
        "series": m.get("series") or "",
        "directors": [{"name": m["director"]}] if m.get("director") else [],
        "actors": [
            {"name": a.get("name", ""), "role": "", "type": "Actress",
             "thumb": a.get("image_url") or ""}
            for a in (m.get("actresses") or [])
        ],
        "genres": m.get("genres") or [],
        "unique_ids": {"code": code},
        "images": {
            "poster": m.get("cover_url") or "",
            "fanart": "",
            "thumb": m.get("thumb_url") or "",
        },
        "sample_images": m.get("sample_images") or [],
    })
    return d
