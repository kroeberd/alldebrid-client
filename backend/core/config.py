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
    download_folder: str = "/download"
    max_concurrent_downloads: int = 3
    max_speed_mbps: int = 0
    aria2_max_download_limit: int = 0  # bytes/s, 0=unlimited — persisted across restarts
    aria2_max_upload_limit: int = 0    # bytes/s, 0=unlimited

    # Download delivery
    download_client: str = "aria2"
    aria2_mode: str = "builtin"  # built-in is the default; no extra setup required
    aria2_url: str = "http://127.0.0.1:6800/jsonrpc"
    aria2_secret: str = ""
    aria2_download_path: str = ""
    aria2_builtin_auto_start: bool = True
    aria2_builtin_port: int = 6800
    aria2_builtin_log_file: str = "/app/data/aria2/aria2.log"
    aria2_builtin_session_file: str = "/app/data/aria2/aria2.session"
    aria2_operation_timeout_seconds: int = 15
    aria2_start_paused: bool = False
    aria2_poll_interval_seconds: int = 1  # fast polling for responsive dispatch
    aria2_max_active_downloads: int = 3
    aria2_purge_interval_minutes: int = 5  # purge completed results more often to free RAM
    aria2_max_download_result: int = 20  # lower = less RAM for completed download metadata
    aria2_keep_unfinished_download_result: bool = False
    aria2_waiting_window: int = 100
    aria2_stopped_window: int = 100
    aria2_split: int = 4  # fewer segments = fewer recv-buffers in aria2 heap
    aria2_min_split_size: str = "20M"  # aria2 default; splits only files >80MB with split=4
    aria2_max_connection_per_server: int = 4  # fewer connections = less buffer RAM
    aria2_disk_cache: str = "0"    # 0 = disabled; per aria2 docs: 4 MiB total for HTTP/FTP downloads
    aria2_file_allocation: str = "none"   # no prealloc: instant start, no blocking; prealloc/falloc block aria2
    aria2_continue_downloads: bool = True
    aria2_lowest_speed_limit: str = "0"

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
    discord_avatar_url: str = ""  # Discord only accepts PNG/JPG/WEBP — SVG rejected
    discord_notify_added: bool = True
    discord_notify_finished: bool = True
    discord_notify_error: bool = True
    discord_notify_update: bool = True

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
    # Periodic built-in aria2 restart to reclaim glibc malloc arena memory.
    # aria2 uses glibc malloc which retains freed pages in arenas even with
    # MALLOC_ARENA_MAX=1. A periodic restart fully resets the process heap.
    # Set to 0 to disable. Downloads are recovered from DB within 1 poll cycle.
    aria2_restart_interval_hours: float = 0  # 0 = disabled; recommended: 4-8h
    # disk-cache for built-in aria2. Set to 0 for native filesystems (ext4/XFS).
    # Set to 16M or higher for FUSE-based mounts (mergerfs, NFS, SMB) to reduce
    # FUSE round-trips and actually lower aria2 heap usage.

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

    # Database maintenance
    db_backup_enabled: bool = True
    db_backup_folder: str = "/app/data/db-backups"
    db_backup_keep_days: int = 7
    db_wipe_enabled: bool = False
    db_backup_before_wipe: bool = True

    # AllDebrid upload retry (statusCode 5 = upload failed)
    upload_fail_retry_count: int = 3   # max retries for statusCode 5
    upload_fail_retry_delay_minutes: int = 5  # minutes between retries

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
    # Max seconds to wait for a single FlexGet task (0 = use default of 3600s = 1h)
    flexget_task_timeout_seconds: int = 0

    # Legacy schedule fields (kept for migration compatibility)
    flexget_schedule_minutes: int = 0
    flexget_jitter_seconds: int = 0

    # ── Jackett ───────────────────────────────────────────────────────────────
    jackett_enabled:    bool = False
    jackett_url:        str  = "http://localhost:9117"
    jackett_api_key:    str  = ""
    jackett_webhook_url: str = ""  # falls leer → discord_webhook_url

    # ── Statistics & Reporting ────────────────────────────────────────────────
    # How often to take a stats snapshot (minutes, 0 = disabled)
    stats_snapshot_interval_minutes: int = 60
    # How many days to keep snapshots
    stats_snapshot_keep_days: int = 30
    # Auto-report: interval in hours (0 = disabled)
    stats_report_interval_hours: int = 0
    update_check_interval_hours: int = 12
    # Report window in hours used for manual default display and scheduled reports
    stats_report_window_hours: int = 24
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
        except Exception as exc:
            import logging
            logging.getLogger("alldebrid.config").warning(
                "Config file could not be read (%s) — using defaults", exc
            )
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
