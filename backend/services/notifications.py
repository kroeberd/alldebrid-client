"""
Discord-Webhook-Benachrichtigungen mit Rich Embeds.

Features:
- Strukturierte Embeds mit Feldern statt rohem Text
- Klare Farbkodierung pro Event-Typ
- Separater Webhook für torrent-added Events
- Deduplizierung (gleiche Nachricht innerhalb 30s wird unterdrückt)
- Rate-Limiting (mindestens 2s zwischen Nachrichten pro URL)
- Discord 429 Rate-Limit-Handling mit retry_after
- Lazy asyncio.Lock (kompatibel mit IsolatedAsyncioTestCase)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger("alldebrid.notify")

APP_NAME    = "AllDebrid-Client"
_VER_PATH   = Path(__file__).resolve().parents[2] / "VERSION"
APP_VERSION = _VER_PATH.read_text(encoding="utf-8").strip() if _VER_PATH.exists() else "dev"
APP_LOGO    = "https://raw.githubusercontent.com/kroeberd/alldebrid-client/main/docs/logo.svg"

# ── Farben ────────────────────────────────────────────────────────────────────
COLOR_INFO    = 0x3B82F6   # Blau    — allgemein
COLOR_SUCCESS = 0x22C55E   # Grün    — abgeschlossen
COLOR_WARNING = 0xF59E0B   # Gelb    — Warnung / Teildownload
COLOR_ERROR   = 0xEF4444   # Rot     — Fehler
COLOR_ADDED   = 0x8B5CF6   # Lila    — Torrent hinzugefügt
COLOR_PARTIAL = 0xF97316   # Orange  — Gefilterte Dateien

# ── Throttling ────────────────────────────────────────────────────────────────
_RATE_LIMIT_SECONDS  = 2.0
_DEDUP_WINDOW_SECONDS = 30.0


def _fmt_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class NotificationService:
    # Klassenweite Zustandsvariablen — alle Instanzen teilen diese
    _last_sent_at: Dict[str, float] = {}
    _sent_hashes:  Dict[str, float] = {}
    _throttle_lock: Optional[asyncio.Lock] = None  # lazy — pro Event-Loop

    def __init__(self, webhook_url: str = "", added_webhook_url: str = ""):
        self.webhook_url       = (webhook_url or "").strip()
        # Separater Kanal für Added-Events; fällt auf Haupt-URL zurück
        self.added_webhook_url = (added_webhook_url or "").strip() or self.webhook_url

    # ── Lock (lazy, thread-safe für Tests) ───────────────────────────────────

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        if cls._throttle_lock is None:
            cls._throttle_lock = asyncio.Lock()
        return cls._throttle_lock

    # ── Öffentliche Event-Methoden ────────────────────────────────────────────

    async def send_added(
        self,
        name: str,
        source: str = "manual",
        alldebrid_id: str = "",
        extra_fields: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Torrent wurde erfolgreich zu AllDebrid hochgeladen."""
        if not self.added_webhook_url:
            return
        fields: List[Dict[str, Any]] = [
            {"name": "Quelle",  "value": _source_label(source), "inline": True},
            {"name": "Status",  "value": "Hochgeladen zu AllDebrid", "inline": True},
            {"name": "Zeit",    "value": _now_utc(), "inline": True},
        ]
        if alldebrid_id:
            fields.append({"name": "AllDebrid ID", "value": str(alldebrid_id), "inline": True})
        if extra_fields:
            fields.extend(extra_fields)
        await self._send(
            url=self.added_webhook_url,
            title="📥 Torrent hinzugefügt",
            description=f"**{name}**",
            color=COLOR_ADDED,
            fields=fields,
        )

    async def send_complete(
        self,
        name: str,
        file_count: int = 0,
        size_bytes: int = 0,
        destination: str = "",
        download_client: str = "",
    ) -> None:
        """Download vollständig abgeschlossen."""
        if not self.webhook_url:
            return
        fields: List[Dict[str, Any]] = []
        if file_count:
            fields.append({"name": "Dateien",  "value": str(file_count),       "inline": True})
        if size_bytes:
            fields.append({"name": "Größe",    "value": _fmt_bytes(size_bytes), "inline": True})
        if download_client:
            fields.append({"name": "Client",   "value": download_client,        "inline": True})
        if destination:
            fields.append({"name": "Zielordner", "value": f"`{destination}`",   "inline": False})
        fields.append(    {"name": "Zeit",     "value": _now_utc(),              "inline": True})
        await self._send(
            url=self.webhook_url,
            title="✅ Download abgeschlossen",
            description=f"**{name}**",
            color=COLOR_SUCCESS,
            fields=fields,
        )

    async def send_error(
        self,
        name: str,
        reason: str = "",
        context: str = "",
    ) -> None:
        """Fehler beim Verarbeiten oder Herunterladen."""
        if not self.webhook_url:
            return
        fields: List[Dict[str, Any]] = []
        if reason:
            fields.append({"name": "Grund",    "value": reason[:1000],  "inline": False})
        if context:
            fields.append({"name": "Kontext",  "value": context[:500],  "inline": False})
        fields.append(    {"name": "Zeit",     "value": _now_utc(),      "inline": True})
        await self._send(
            url=self.webhook_url,
            title="❌ Fehler",
            description=f"**{name}**",
            color=COLOR_ERROR,
            fields=fields,
        )

    async def send_partial(
        self,
        name: str,
        total_files: int,
        downloaded_files: int,
        blocked_files: int,
        total_size: int = 0,
        downloaded_size: int = 0,
    ) -> None:
        """Teildownload — manche Dateien wurden durch Filter blockiert."""
        if not self.webhook_url:
            return
        fields: List[Dict[str, Any]] = [
            {"name": "Gesamt",            "value": str(total_files),         "inline": True},
            {"name": "Heruntergeladen",   "value": str(downloaded_files),    "inline": True},
            {"name": "Gefiltert",         "value": str(blocked_files),       "inline": True},
        ]
        if total_size:
            fields.append({"name": "Gesamtgröße",          "value": _fmt_bytes(total_size),       "inline": True})
        if downloaded_size:
            fields.append({"name": "Heruntergeladene Größe", "value": _fmt_bytes(downloaded_size), "inline": True})
        fields.append(    {"name": "Zeit",                 "value": _now_utc(),                   "inline": True})
        await self._send(
            url=self.webhook_url,
            title="⚠️ Teildownload",
            description=f"**{name}**\nEinige Dateien wurden gefiltert",
            color=COLOR_PARTIAL,
            fields=fields,
        )

    async def send(self, title: str, description: str, color: int = COLOR_INFO) -> None:
        """Abwärtskompatibel — einfache Nachricht ohne Felder."""
        if not self.webhook_url:
            return
        await self._send(
            url=self.webhook_url,
            title=title,
            description=description,
            color=color,
        )

    async def test(self) -> bool:
        """Sendet eine Test-Nachricht. Gibt True zurück wenn erfolgreich."""
        if not self.webhook_url:
            return False
        try:
            await self._send(
                url=self.webhook_url,
                title="🔔 Test-Benachrichtigung",
                description=f"**{APP_NAME}** ist verbunden und bereit.",
                color=COLOR_INFO,
                fields=[
                    {"name": "Version", "value": APP_VERSION, "inline": True},
                    {"name": "Zeit",    "value": _now_utc(),  "inline": True},
                ],
            )
            return True
        except Exception:
            return False

    # ── Interne Implementierung ───────────────────────────────────────────────

    async def _send(
        self,
        url: str,
        title: str,
        description: str,
        color: int = COLOR_INFO,
        fields: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if not url:
            return

        # Deduplizierung: gleicher Inhalt innerhalb von 30s → überspringen
        dedup_key = hashlib.md5(
            f"{url}|{title}|{description[:100]}".encode()
        ).hexdigest()

        async with self._get_lock():
            now = time.monotonic()

            last_hash = self._sent_hashes.get(dedup_key, 0.0)
            if now - last_hash < _DEDUP_WINDOW_SECONDS:
                logger.debug("Discord: Duplikat unterdrückt (%s)", title)
                return

            # Rate-Limiting
            wait = max(0.0, _RATE_LIMIT_SECONDS - (now - self._last_sent_at.get(url, 0.0)))
            if wait > 0:
                await asyncio.sleep(wait)

            self._last_sent_at[url] = time.monotonic()
            self._sent_hashes[dedup_key] = time.monotonic()

            # Alte Einträge bereinigen (> 5 Minuten)
            cutoff = time.monotonic() - 300
            self._sent_hashes = {k: v for k, v in self._sent_hashes.items() if v > cutoff}

        embed: Dict[str, Any] = {
            "title":       title[:256],
            "description": description[:4096],
            "color":       color,
            "footer": {
                "text":     f"{APP_NAME} v{APP_VERSION}",
                "icon_url": APP_LOGO,
            },
        }
        if fields:
            embed["fields"] = [
                {
                    "name":   f.get("name", "")[:256],
                    "value":  f.get("value", "—")[:1024],
                    "inline": bool(f.get("inline", True)),
                }
                for f in fields[:25]
            ]

        payload = {
            "username":   APP_NAME,
            "avatar_url": APP_LOGO,
            "embeds":     [embed],
        }

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 429:
                        data = await resp.json(content_type=None)
                        retry_after = float(data.get("retry_after", 5))
                        logger.warning("Discord Rate-Limit — warte %.1fs", retry_after)
                        await asyncio.sleep(retry_after)
                        # Einmalig wiederholen
                        async with session.post(url, json=payload) as resp2:
                            if resp2.status not in (200, 204):
                                logger.warning("Discord Retry status %d", resp2.status)
                    elif resp.status not in (200, 204):
                        body = await resp.text()
                        logger.warning("Discord Webhook %d: %s", resp.status, body[:200])
        except Exception as exc:
            logger.error("Discord Benachrichtigung fehlgeschlagen: %s", exc)


def _source_label(source: str) -> str:
    """Wandelt interne Quell-Strings in lesbare Labels um."""
    return {
        "manual":             "Manuell (UI)",
        "watch_file":         "Watch-Ordner (.magnet)",
        "watch_torrent":      "Watch-Ordner (.torrent)",
        "alldebrid_existing": "AllDebrid Import",
        "api":                "API",
    }.get(source, source)
