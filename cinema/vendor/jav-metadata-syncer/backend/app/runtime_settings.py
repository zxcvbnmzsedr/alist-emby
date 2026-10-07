"""Runtime settings overlay.

Layer cake (lowest → highest precedence):
  1. defaults baked into pydantic Settings
  2. env vars / .env file (loaded by pydantic at startup)
  3. JSON file at {data_dir}/settings.json (written by the /api/settings PUT)
"""
import json
from pathlib import Path

from .config import settings

# Anything outside this set is rejected by update().
WRITABLE_KEYS = {"enabled_sources"}


def _settings_file() -> Path:
    return Path(settings.data_dir) / "settings.json"


def load_from_disk() -> None:
    """Apply any saved overrides to the live settings singleton."""
    f = _settings_file()
    if not f.exists():
        return
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return
    for k, v in (data or {}).items():
        if k in WRITABLE_KEYS:
            setattr(settings, k, v)


def update(updates: dict) -> None:
    """Persist a partial update and live-apply it."""
    clean = {k: v for k, v in (updates or {}).items() if k in WRITABLE_KEYS}
    f = _settings_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if f.exists():
        try:
            existing = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
    existing.update(clean)
    f.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    for k, v in clean.items():
        setattr(settings, k, v)


def current() -> dict:
    """Effective values for the UI."""
    from .sources import enabled_names
    return {
        "enabled_sources": enabled_names(),
    }
