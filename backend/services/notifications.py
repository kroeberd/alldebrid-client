"""
Discord-Webhook-Benachrichtigungsdienst mit Rich Embeds.

Funktionen:
- Strukturierte Embeds mit Feldern statt einfachem Text
- Klare Farbkodierung: blau=info, grün=erfolg, orange=warnung, rot=fehler
- Deduplizierung: gleiche Nachricht innerhalb von 30s wird nicht erneut gesendet
- Rate-Limiting: mindestens 2s zwischen Nachrichten an die gleiche URL
- Separater Webhook für "Torrent hinzugefügt"-Events
- Torrent-Name immer prominent im Titel
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

logger = logging.getLogger("alldebrid.notify")

APP_NAME = "AllDebrid-Client"
VERSION_PATH = Path(__file__).resolve().parents[2] / "VERSION"
APP_VERSION = VERSION_PATH.read_text(encoding="utf-8").strip() if VERSION_PATH.exists() else "dev"
APP_LOGO_URL = "https://raw.githubusercontent.com/kroeberd/alldebrid-client/main/docs/logo.svg"

# Farben
COLOR_INFO    = 0x3B82F6   # Blau
COLOR_SUCCESS = 0x22C55E   # Grün
COLOR_WARNING = 0xF59E0B   # Orange
COLOR_ERROR   = 0xEF4444   # Rot
COLOR_ADDED   = 0x8B5CF6   # Lila
COLOR_PARTIAL = 0xF97316   # Orange-Rot

# Rate-Limiting
_RATE_LIMIT_SECONDS = 2.0
# Deduplizierungsfenster: gleiche Nachricht innerhalb dieses Zeitfensters wird unterdrückt
_DEDUP_WINDOW_SECONDS = 30.0


def _fmt_bytes(size: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size or 0)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.1f} {units[idx]}"


def _fmt_time_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class NotificationService:
    # Klassenweite Rate-Limiting-Daten (alle Instanzen teilen diese)
    _last_sent_at: Dict[str, float] = {}
    _throttle_lock: Optional[asyncio.Lock] = None   # lazy — wird beim ersten Aufruf erstellt
    # Deduplizierung: hash(url+title+content) → timestamp
    _sent_hashes: Dict[str, float] = {}

    @classmethod
    def _get_lock(cls) -> asyncio.Lock:
        """Gibt den Throttle-Lock zurück, erstellt ihn lazy im aktuellen event loop."""
        if cls._throttle_lock is None:
            cls._throttle_lock = asyncio.Lock()
        return cls._throttle_lock

    def __init__(self, webhook_url: str = "", added_webhook_url: str = ""):
        self.webhook_url = webhook_url.strip() if webhook_url else ""
        # Separater Webhook für "Added"-Events; fällt auf webhook_url zurück
        self.added_webhook_url = (added_webhook_url or "").strip() or self.webhook_url

    # ─────────────────────────────────────────────────────────────────────────
    # Öffentliche Methoden
    # ─────────────────────────────────────────────────────────────────────────

    async def send_added(
        self,
        name: str,
        source: str = "manual",
        alldebrid_id: str = "",
        extra_fields: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """
        Sendet eine "Torrent hinzugefügt"-Nachricht an den konfigurierten Webhook.

        Diese Methode verwendet `added_webhook_url`, falls konfiguriert,
        andernfalls `webhook_url`.
        """
        if not self.added_webhook_url:
            return
        fields: List[Dict[str, str]] = [
            {"name": "Quelle", "value": source, "inline": True},
            {"name": "Status", "value": "Hochgeladen zu AllDebrid", "inline": True},
            {"name": "Zeit", "value": _fmt_time_utc(), "inline": True},
        ]
        if alldebrid_id:
            fields.append({"name": "AllDebrid ID", "value": alldebrid_id, "inline": True})
        if extra_fields:
            fields.extend(extra_fields)

        await self._send_embed(
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
        """Sendet eine Abschluss-Nachricht."""
        if not self.webhook_url:
            return
        fields: List[Dict[str, str]] = []
        if file_count:
            fields.append({"name": "Dateien", "value": str(file_count), "inline": True})
        if size_bytes:
            fields.append({"name": "Größe", "value": _fmt_bytes(size_bytes), "inline": True})
        if download_client:
            fields.append({"name": "Client", "value": download_client, "inline": True})
        if destination:
            fields.append({"name": "Zielordner", "value": f"`{destination}`", "inline": False})
        fields.append({"name": "Zeit", "value": _fmt_time_utc(), "inline": True})

        await self._send_embed(
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
        """Sendet eine Fehler-Nachricht."""
        if not self.webhook_url:
            return
        fields: List[Dict[str, str]] = []
        if reason:
            fields.append({"name": "Grund", "value": reason[:1000], "inline": False})
        if context:
            fields.append({"name": "Kontext", "value": context, "inline": False})
        fields.append({"name": "Zeit", "value": _fmt_time_utc(), "inline": True})

        await self._send_embed(
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
        """Sendet eine Teildownload-Zusammenfassung (manche Dateien gefiltert)."""
        if not self.webhook_url:
            return
        fields: List[Dict[str, str]] = [
            {"name": "Gesamt", "value": str(total_files), "inline": True},
            {"name": "Heruntergeladen", "value": str(downloaded_files), "inline": True},
            {"name": "Gefiltert", "value": str(blocked_files), "inline": True},
        ]
        if total_size:
            fields.append({"name": "Gesamtgröße", "value": _fmt_bytes(total_size), "inline": True})
        if downloaded_size:
            fields.append({"name": "Heruntergeladene Größe", "value": _fmt_bytes(downloaded_size), "inline": True})
        fields.append({"name": "Zeit", "value": _fmt_time_utc(), "inline": True})

        await self._send_embed(
            url=self.webhook_url,
            title="⚠️ Teildownload",
            description=f"**{name}**\nEinige Dateien wurden gefiltert",
            color=COLOR_PARTIAL,
            fields=fields,
        )

    async def send(self, title: str, description: str, color: int = COLOR_INFO) -> None:
        """
        Abwärtskompatible Methode für einfache Nachrichten ohne Felder.
        Bestehender Code kann diese Methode weiter verwenden.
        """
        if not self.webhook_url:
            return
        await self._send_embed(
            url=self.webhook_url,
            title=title,
            description=description,
            color=color,
        )

    async def test(self) -> bool:
        try:
            await self._send_embed(
                url=self.webhook_url,
                title="🔔 Test-Benachrichtigung",
                description=f"**{APP_NAME}** ist verbunden und bereit.",
                color=COLOR_INFO,
                fields=[
                    {"name": "Version", "value": APP_VERSION, "inline": True},
                    {"name": "Zeit", "value": _fmt_time_utc(), "inline": True},
                ],
            )
            return True
        except Exception:
            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Interne Implementierung
    # ─────────────────────────────────────────────────────────────────────────

    async def _send_embed(
        self,
        url: str,
        title: str,
        description: str,
        color: int = COLOR_INFO,
        fields: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        if not url:
            return

        # Deduplizierung
        dedup_key = hashlib.md5(
            f"{url}|{title}|{description[:100]}".encode()
        ).hexdigest()

        async with self._get_lock():
            now = time.monotonic()

            # Prüfe ob diese Nachricht kürzlich gesendet wurde
            last_hash_time = self._sent_hashes.get(dedup_key, 0.0)
            if now - last_hash_time < _DEDUP_WINDOW_SECONDS:
                logger.debug(
                    "Nachricht unterdrückt (Duplikat innerhalb %ss): %s",
                    _DEDUP_WINDOW_SECONDS,
                    title,
                )
                return

            # Rate-Limiting
            last_sent = self._last_sent_at.get(url, 0.0)
            wait_for = max(0.0, _RATE_LIMIT_SECONDS - (now - last_sent))
            if wait_for > 0:
                await asyncio.sleep(wait_for)

            self._last_sent_at[url] = time.monotonic()
            self._sent_hashes[dedup_key] = time.monotonic()

            # Alte Einträge bereinigen (> 5 Minuten)
            cutoff = time.monotonic() - 300
            self._sent_hashes = {
                k: v for k, v in self._sent_hashes.items() if v > cutoff
            }

        embed: Dict[str, Any] = {
            "title": title[:256],
            "description": description[:4096],
            "color": color,
            "footer": {
                "text": f"{APP_NAME} v{APP_VERSION}",
                "icon_url": APP_LOGO_URL,
            },
        }

        if fields:
            embed["fields"] = [
                {
                    "name": f.get("name", "")[:256],
                    "value": f.get("value", "—")[:1024],
                    "inline": f.get("inline", True),
                }
                for f in fields[:25]  # Discord-Limit: 25 Felder
            ]

        payload = {
            "username": APP_NAME,
            "avatar_url": APP_LOGO_URL,
            "embeds": [embed],
        }

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as session:
                async with session.post(url, json=payload) as resp:
                    if resp.status == 429:
                        # Rate-limited von Discord
                        retry_after = float(
                            (await resp.json(content_type=None)).get("retry_after", 5)
                        )
                        logger.warning(
                            "Discord Rate-Limit erreicht, warte %.1fs", retry_after
                        )
                        await asyncio.sleep(retry_after)
                    elif resp.status not in (200, 204):
                        body = await resp.text()
                        logger.warning(
                            "Discord-Webhook antwortete mit %s: %s",
                            resp.status,
                            body[:200],
                        )
        except Exception as exc:
            logger.error("Discord-Benachrichtigung fehlgeschlagen: %s", exc)
