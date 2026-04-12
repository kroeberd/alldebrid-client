import json
import os
from pathlib import Path
from typing import List
from pydantic import BaseModel

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/config/config.json"))

# Single mutable container — all modules reference this dict-like object
# so changes via update_settings() are immediately visible everywhere.
class AppSettings(BaseModel):
    # AllDebrid
    alldebrid_api_key: str = ""
    alldebrid_agent: str = "AllDebrid-Client"

    # Download
    watch_folder: str = "/app/data/watch"
    processed_folder: str = "/app/data/processed"
    download_folder: str = "/app/data/downloads"
    max_concurrent_downloads: int = 3
    max_speed_mbps: int = 0  # 0 = unlimited

    # Integrations
    ariang_url: str = ""
    ariang_enabled: bool = False
    jdownloader_url: str = ""
    jdownloader_user: str = ""
    jdownloader_password: str = ""
    jdownloader_enabled: bool = False

    # Notifications
    discord_webhook_url: str = ""
    discord_notify_added: bool = True
    discord_notify_finished: bool = True
    discord_notify_error: bool = True

    # Filters
    blocked_extensions: List[str] = [
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
        ".svg", ".ico", ".tiff", ".heic"
    ]
    blocked_keywords: List[str] = []
    min_file_size_mb: int = 0

    # Polling
    poll_interval_seconds: int = 30
    watch_interval_seconds: int = 10


# Module-level mutable object — ALL modules must call get_settings() to
# get the current live instance, never cache the result at import time.
_settings: AppSettings = AppSettings()


def get_settings() -> AppSettings:
    return _settings


def load_settings() -> AppSettings:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
            return AppSettings(**data)
        except Exception:
            pass
    return AppSettings()


def save_settings(s: AppSettings):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(s.model_dump(), f, indent=2)


def apply_settings(s: AppSettings):
    """Update the live settings object in-place so all modules see the change."""
    global _settings
    _settings = s


# Load persisted settings on startup
_settings = load_settings()

# Legacy alias so old `from core.config import settings` still works
# for READ-ONLY uses — but write paths must use apply_settings()
settings = _settings  # NOTE: this alias is stale after apply_settings() — use get_settings()
