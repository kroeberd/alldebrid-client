import json
import os
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/app/config/config.json"))


class AppSettings(BaseModel):
    # AllDebrid
    alldebrid_api_key: str = ""
    alldebrid_agent: str = "AllDebrid-Client"

    # ── Datenbank ──────────────────────────────────────────────────────────────
    # db_type = "sqlite" (Standard, abwärtskompatibel) oder "postgres"
    db_type: str = "sqlite"
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "alldebrid"
    postgres_user: str = "alldebrid"
    postgres_password: str = ""
    postgres_schema: str = "public"
    postgres_ssl: bool = False
    postgres_application_name: str = "alldebrid-client"

    # Folders
    watch_folder: str = "/app/data/watch"
    processed_folder: str = "/app/data/processed"
    download_folder: str = "/app/data/downloads"
    max_concurrent_downloads: int = 3
    max_speed_mbps: int = 0

    # Download delivery
    download_client: str = "direct"
    aria2_url: str = "http://127.0.0.1:6800/jsonrpc"
    aria2_secret: str = ""
    aria2_download_path: str = ""
    aria2_operation_timeout_seconds: int = 15
    aria2_start_paused: bool = False
    aria2_poll_interval_seconds: int = 5
    aria2_max_active_downloads: int = 3

    # ── Discord ────────────────────────────────────────────────────────────────
    discord_webhook_url: str = ""
    # Separater Webhook für "Torrent hinzugefügt"; fällt auf discord_webhook_url zurück
    discord_webhook_added: str = ""
    discord_notify_added: bool = True
    discord_notify_finished: bool = True
    discord_notify_error: bool = True

    # Filters
    filters_enabled: bool = True
    blocked_extensions: List[str] = [
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
        ".svg", ".ico", ".tiff", ".heic", ".nfo", ".sfv"
    ]
    blocked_keywords: List[str] = []
    min_file_size_mb: int = 0

    # Polling
    poll_interval_seconds: int = 30
    watch_interval_seconds: int = 10
    paused: bool = False


_settings: AppSettings = AppSettings()


def get_settings() -> AppSettings:
    return _settings


def load_settings() -> AppSettings:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
            valid = {k: v for k, v in data.items() if k in AppSettings.model_fields}
            return AppSettings(**valid)
        except Exception:
            pass
    return AppSettings()


def save_settings(s: AppSettings):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(s.model_dump(), f, indent=2)


def apply_settings(s: AppSettings):
    global _settings
    _settings = s


_settings = load_settings()
settings = _settings
