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

    # Database
    db_type: str = "sqlite"
    postgres_host: str = "alldebrid-postgres"
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
    download_client: str = "aria2"
    aria2_url: str = "http://127.0.0.1:6800/jsonrpc"
    aria2_secret: str = ""
    aria2_download_path: str = ""
    aria2_operation_timeout_seconds: int = 15
    aria2_start_paused: bool = False
    aria2_poll_interval_seconds: int = 5
    aria2_max_active_downloads: int = 3

    # Sonarr integration
    sonarr_enabled: bool = False
    sonarr_url: str = ""
    sonarr_api_key: str = ""

    # Radarr integration
    radarr_enabled: bool = False
    radarr_url: str = ""
    radarr_api_key: str = ""

    # Discord
    discord_webhook_url: str = ""
    discord_webhook_added: str = ""
    discord_username: str = "AllDebrid-Client"
    discord_avatar_url: str = "https://raw.githubusercontent.com/kroeberd/alldebrid-client/main/docs/logo.svg"
    discord_notify_added: bool = True
    discord_notify_finished: bool = True
    discord_notify_error: bool = True

    # Filters
    filters_enabled: bool = False
    blocked_extensions: List[str] = [
        ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
        ".svg", ".ico", ".tiff", ".heic", ".nfo", ".sfv"
    ]
    blocked_keywords: List[str] = []
    min_file_size_mb: int = 0

    # Deep aria2 filesystem sync
    # Interval in minutes (0 = disabled). Checks actual file presence on disk
    # independently of aria2 GID/status, resolving same-filename-different-folder issues.
    aria2_deep_sync_interval_minutes: int = 10

    # Polling
    poll_interval_seconds: int = 30
    watch_interval_seconds: int = 10
    paused: bool = False

    # Rate limiting — AllDebrid API calls per minute (0 = unlimited)
    alldebrid_rate_limit_per_minute: int = 60

    # Auto-restart stuck downloads
    # Torrents stuck in queued/downloading for longer than this are reset (0 = disabled)
    stuck_download_timeout_hours: int = 6
    # Full AllDebrid reconciliation interval (minutes) — syncs ALL torrents incl. error/queued
    full_sync_interval_minutes: int = 5

    # Backups
    backup_enabled: bool = True
    backup_folder: str = "/app/data/backups"
    backup_keep_days: int = 7
    backup_interval_hours: int = 24

    # aria2 download retry on error
    # How many times to retry a failed aria2 download before giving up (0 = no retry)
    aria2_error_retry_count: int = 3
    # Seconds to wait between retries
    aria2_error_retry_delay_seconds: int = 60

    # Labels / categories (comma-separated, empty = disabled)
    torrent_labels: List[str] = []

    # ── FlexGet integration ───────────────────────────────────────────────────
    flexget_enabled: bool = False
    flexget_url: str = "http://localhost:5050"
    flexget_api_key: str = ""
    # Comma-separated task names to run (empty = all tasks)
    flexget_tasks_raw: str = ""
    # Webhook URL for FlexGet events (separate from Discord)
    flexget_webhook_url: str = ""
    # JSON array of task schedule objects: [{task, interval_minutes, jitter_seconds, enabled}]
    flexget_task_schedules_json: str = "[]"
    # Minutes to wait before retrying when FlexGet is unreachable (0 = disabled)
    flexget_retry_delay_minutes: int = 5
    # Schedule: interval in minutes (0 = disabled, max 720 = 12h)
    flexget_schedule_minutes: int = 0
    # Minutes to wait before retrying when FlexGet is unreachable (default 5)
    flexget_retry_delay_minutes: int = 5
    # Jitter: random offset added to schedule interval (seconds, 0 = disabled)
    flexget_jitter_seconds: int = 0

    # ── Statistics & Reporting ────────────────────────────────────────────────
    # How often to take a stats snapshot (minutes, 0 = disabled)
    stats_snapshot_interval_minutes: int = 60
    # How many days to keep snapshots
    stats_snapshot_keep_days: int = 30
    # Auto-report: interval in hours (0 = disabled)
    stats_report_interval_hours: int = 0
    # Webhook URL that receives automated reporting payloads
    stats_report_webhook_url: str = ""


_settings: AppSettings = AppSettings()


def _build_effective_settings(loaded: dict) -> AppSettings:
    env_db_type = os.getenv("DB_TYPE", "").strip()
    if env_db_type:
        loaded["db_type"] = env_db_type
    return AppSettings(**{k: v for k, v in loaded.items() if k in AppSettings.model_fields})


def get_settings() -> AppSettings:
    return _settings


def load_settings() -> AppSettings:
    loaded: dict = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
            loaded = {k: v for k, v in data.items() if k in AppSettings.model_fields}
        except Exception:
            pass
    return _build_effective_settings(loaded)


def save_settings(s: AppSettings):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = s.model_dump()
    if os.getenv("DB_TYPE") == "postgres_internal":
        data.pop("postgres_password", None)
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


def apply_settings(s: AppSettings):
    global _settings
    _settings = s


_settings = load_settings()
settings = _settings
