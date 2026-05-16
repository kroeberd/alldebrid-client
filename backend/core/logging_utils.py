"""Logging helpers for Docker-friendly, secret-safe output."""
from __future__ import annotations

import logging
import re
from typing import Any

_MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:([0-9a-zA-Z]{32,40})[^\s\"']*", re.IGNORECASE)
_WEBHOOK_URL_RE = re.compile(
    r"https?://(?:canary\.|ptb\.)?(?:discord(?:app)?\.com)/api/webhooks/[^\s\"']+",
    re.IGNORECASE,
)
_LONG_URL_RE = re.compile(r"https?://[^\s\"']{80,}")
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|apikey|token|secret|password|pass|key)=)([^&\s\"']+)"
)
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|token|basic)\s+)[^\s,\"']+")
_PG_DSN_RE = re.compile(r"(postgres(?:ql)?://[^:\s/@]+:)([^@\s]+)(@)", re.IGNORECASE)


def _short_hash(value: str) -> str:
    if not value:
        return ""
    return f"{value[:8]}..."


def sanitize_log_value(value: Any, max_length: int = 300) -> str:
    """Return a short, log-safe string with secrets and long URLs redacted."""
    if value is None:
        return ""
    msg = str(value)
    msg = _PG_DSN_RE.sub(r"\1<redacted>\3", msg)
    msg = _AUTH_HEADER_RE.sub(r"\1<redacted>", msg)
    msg = _WEBHOOK_URL_RE.sub("<webhook-url>", msg)
    msg = _MAGNET_RE.sub(lambda m: f"<magnet:{_short_hash(m.group(1).lower())}>", msg)
    msg = _SENSITIVE_QUERY_RE.sub(r"\1<redacted>", msg)
    msg = _LONG_URL_RE.sub("<url>", msg)
    return (msg[:max_length] + "...") if len(msg) > max_length else msg


def sanitize_exception(exc: BaseException, max_length: int = 300) -> str:
    """Return a safe exception message suitable for logs and API errors."""
    return sanitize_log_value(str(exc).strip() or repr(exc), max_length=max_length)


def configure_logging(level: str = "INFO", pretty: bool = False, log_format: str = "plain") -> None:
    """Configure the app's plaintext log format without forcing ANSI colors."""
    normalized = str(level or "INFO").upper()
    numeric_level = getattr(logging, normalized, logging.INFO)
    fmt_name = str(log_format or "plain").lower()
    if fmt_name == "compact":
        fmt = "%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"
    elif pretty:
        fmt = "%(asctime)s | %(levelname)-5s | %(name)-16s | %(message)s"
    else:
        fmt = "%(asctime)s | %(levelname)-5s | %(name)-20s | %(message)s"
    logging.basicConfig(
        level=numeric_level,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger().setLevel(numeric_level)
    for lib in ("uvicorn.access", "uvicorn.error", "httpx", "aiosqlite", "asyncpg"):
        logging.getLogger(lib).setLevel(logging.WARNING)


def log_startup_banner(
    logger: logging.Logger,
    *,
    version: str,
    mode: str,
    database: str,
    download_client: str,
    web_ui: str,
    auth: str,
) -> None:
    """Emit a compact startup summary through the normal logger."""
    sep = "-" * 56
    lines = [
        sep,
        f" AllDebrid-Client  v{version}",
        sep,
        f" Mode:            {mode}",
        f" Database:        {database}",
        f" Download Client: {download_client}",
        f" Web UI:          {web_ui}",
        f" Auth:            {auth}",
        " GitHub:          https://github.com/kroeberd/alldebrid-client",
        " Discord:         https://discord.gg/8Vb9cj4ksv",
        " Support:         https://buymeacoffee.com/kroeberd",
        sep,
    ]
    for line in lines:
        logger.info(line)
