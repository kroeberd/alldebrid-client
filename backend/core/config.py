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
    #   "postgres"          → Externe PostgreSQL-Instanz (postgres_* Felder)
    #   "postgres_internal" → Interner PG-Container aus docker-compose (auto-config)
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
    """
    Lädt Einstellungen aus config.json.
    Umgebungsvariablen (DB_TYPE, POSTGRES_PASSWORD) überschreiben gespeicherte Werte,
    damit docker-compose-Override ohne config.json-Änderung funktioniert.
    """
    loaded: dict = {}
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r") as f:
                data = json.load(f)
            loaded = {k: v for k, v in data.items() if k in AppSettings.model_fields}
        except Exception:
            pass

    # Umgebungsvariablen haben Vorrang (für Docker-Compose-Integration)
    env_db_type = os.getenv("DB_TYPE", "").strip()
    if env_db_type:
        loaded["db_type"] = env_db_type

    # Interner PG-Container: Verbindungsdaten automatisch aus Umgebung befüllen
    if loaded.get("db_type") == "postgres_internal":
        pg_password = os.getenv("POSTGRES_PASSWORD", "alldebrid_internal")
        loaded.setdefault("postgres_host", "postgres")   # Service-Name im Compose-Netzwerk
        loaded.setdefault("postgres_port", 5432)
        loaded.setdefault("postgres_db", "alldebrid")
        loaded.setdefault("postgres_user", "alldebrid")
        loaded["postgres_password"] = pg_password         # immer aus Env, nie aus config.json
        loaded.setdefault("postgres_ssl", False)
        # Intern wird "postgres_internal" auf "postgres" gemappt — gleiche Logik
        loaded["db_type"] = "postgres"

    return AppSettings(**loaded)


def save_settings(s: AppSettings):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = s.model_dump()
    # Passwort des internen PG nicht in config.json speichern
    # (kommt aus Umgebungsvariable, nicht aus Benutzereingabe)
    if os.getenv("DB_TYPE") == "postgres_internal":
        data.pop("postgres_password", None)
    with open(CONFIG_PATH, "w") as f:
        json.dump(data, f, indent=2)


def apply_settings(s: AppSettings):
    global _settings
    _settings = s


_settings = load_settings()
settings = _settings
