"""Read / write runtime settings via the UI."""
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import runtime_settings
from ..sources import source_names

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsOut(BaseModel):
    enabled_sources: List[str]


class SettingsIn(BaseModel):
    enabled_sources: Optional[List[str]] = None


@router.get("", response_model=SettingsOut, operation_id="get_settings")
def get_settings():
    return SettingsOut(**runtime_settings.current())


@router.put("", response_model=SettingsOut, operation_id="update_settings")
def update_settings(body: SettingsIn):
    updates = body.model_dump(exclude_none=True)
    if "enabled_sources" in updates:
        bad = [s for s in updates["enabled_sources"] if s not in source_names()]
        if bad:
            raise HTTPException(400, f"未知來源: {bad}（可用: {source_names()}）")
        updates["enabled_sources"] = ",".join(updates["enabled_sources"])
    runtime_settings.update(updates)
    return SettingsOut(**runtime_settings.current())
