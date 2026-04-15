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
    # db_type:
    #   "sqlite"            → SQLite (Standard, abwärtskompatibel)
    #   "postgres"          → Externe PostgreSQL-Instanz
    #   "postgres_internal" → Interner Docker-Container (auto-config via Env)
    db_type: str = "sqlite"

    # PostgreSQL-Verbindung — Defaults entsprechen dem internen Container
    postgres_host: str = "postgres"       # "postgres" = interner Container-Name
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


def _build_effective_settings(loaded: dict) -> AppSettings:
    """
    Wendet Umgebungsvariablen-Overrides an und normalisiert postgres_internal.
    Wird sowohl von load_settings() als auch apply_settings() genutzt.
    """
    # DB_TYPE Env hat Vorrang vor config.json
    env_db_type = os.getenv("DB_TYPE", "").strip()
    if env_db_type:
        loaded["db_type"] = env_db_type

    db_type = loaded.get("db_type", "sqlite")

    # Interner PG-Container: Verbindungsdaten aus Env — immer, egal was in config.json steht
    if db_type == "postgres_internal":
        pg_password = os.getenv("POSTGRES_PASSWORD", "alldebrid_internal")
        # Diese Werte immer setzen (nicht setdefault) — interne Verbindung ist fest definiert
        loaded["postgres_host"]     = "postgres"   # Docker-Netzwerk-Name des PG-Containers
        loaded["postgres_port"]     = 5432
        loaded["postgres_db"]       = "alldebrid"
        loaded["postgres_user"]     = "alldebrid"
        loaded["postgres_password"] = pg_password
        loaded["postgres_ssl"]      = False
        # Intern → nach außen als "postgres" behandeln (gleiche Logik)
        loaded["db_type"] = "postgres"

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
    # Passwort des internen PG nicht in config.json speichern — kommt aus Env
    if os.getenv("DB_TYPE") == "postgres_internal":
        data.pop("postgres_password", None)
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


def apply_settings(s: AppSettings):
    global _settings
    _settings = s


_settings = load_settings()
settings = _settings
