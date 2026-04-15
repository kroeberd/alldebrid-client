# Discord Webhook-Konfiguration

## Standard-Webhook

Für alle Benachrichtigungen:

```json
{
  "discord_webhook_url": "https://discord.com/api/webhooks/...",
  "discord_notify_added": true,
  "discord_notify_finished": true,
  "discord_notify_error": true
}
```

## Discord-Identität

```json
{
  "discord_username": "AllDebrid-Client",
  "discord_avatar_url": "https://example.com/avatar.png"
}
```

Hinweis:
- `discord_avatar_url` muss für Discord über eine echte HTTP- oder HTTPS-URL erreichbar sein.
- Lokale URLs wie `http://192.168.x.x/...`, `http://localhost/...` oder `.local` funktionieren für Discord in der Regel nicht.
- Für hochgeladene Avatare kann optional `PUBLIC_BASE_URL` gesetzt werden, damit die App statt einer lokalen Adresse eine öffentliche URL wie `https://example.com/api/avatar` zurückgibt.

## Separater Webhook für "Torrent hinzugefügt"

```json
{
  "discord_webhook_url": "https://discord.com/api/webhooks/.../main",
  "discord_webhook_added": "https://discord.com/api/webhooks/.../added"
}
```

`discord_webhook_added` fällt auf `discord_webhook_url` zurück wenn leer.

## Event-Typen

| Event | Methode | Farbe | Trigger |
|-------|---------|-------|---------|
| Torrent hinzugefügt | `send_added()` | Lila 🟣 | Nach erfolgreichem Upload zu AllDebrid |
| Download abgeschlossen | `send_complete()` | Grün 🟢 | Wenn alle Dateien heruntergeladen |
| Fehler | `send_error()` | Rot 🔴 | Bei AllDebrid- oder Download-Fehler |
| Teildownload | `send_partial()` | Orange 🟠 | Wenn Dateien gefiltert wurden |

## Metadaten in Embeds

### Torrent hinzugefügt
- Torrent-Name
- Quelle (manual, watch_file, watch_torrent, alldebrid_existing)
- AllDebrid ID
- Zeitstempel

### Download abgeschlossen
- Torrent-Name
- Anzahl Dateien
- Gesamtgröße
- Download-Client (aria2)
- Zielordner
- Zeitstempel

## Anti-Spam

- **Deduplizierung**: Gleiche Nachricht innerhalb von 30 Sekunden wird nur einmal gesendet
- **Rate-Limiting**: Mindestens 2 Sekunden zwischen Nachrichten an die gleiche URL
- **Discord 429**: Automatisches Warten bei Rate-Limit-Antwort von Discord
