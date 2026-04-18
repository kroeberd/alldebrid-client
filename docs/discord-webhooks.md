# Discord Webhook-Konfiguration

## Standard-Webhook

For all notifications:

```json
{
  "discord_webhook_url": "https://discord.com/api/webhooks/...",
  "discord_notify_added": true,
  "discord_notify_finished": true,
  "discord_notify_error": true
}
```

## Discord Identity

```json
{
  "discord_username": "AllDebrid-Client",
  "discord_avatar_url": "https://example.com/avatar.png"
}
```

Hinweis:
- `discord_avatar_url` must be accessible by Discord via a real HTTP/HTTPS URL.
- Local URLs like `http://192.168.x.x/...`, `http://localhost/...` or `.local` usually don't work for Discord.
- For uploaded avatars, optionally set `PUBLIC_BASE_URL` so the app returns a public URL like `https://example.com/api/avatar` instead of a local address.

## Separate Webhook for Torrent Added

```json
{
  "discord_webhook_url": "https://discord.com/api/webhooks/.../main",
  "discord_webhook_added": "https://discord.com/api/webhooks/.../added"
}
```

`discord_webhook_added` falls back to `discord_webhook_url` when empty.

## Event-Typen

| Event | Methode | Farbe | Trigger |
|-------|---------|-------|---------|
| Torrent Added | `send_added()` | Purple 🟣 | After successful upload to AllDebrid |
| Download Complete | `send_complete()` | Green 🟢 | When all files are downloaded |
| Error | `send_error()` | Red 🔴 | On AllDebrid or download error |
| Teildownload | `send_partial()` | Orange 🟠 | Wenn Dateien gefiltert wurden |

## Metadaten in Embeds

### Torrent Added
- Torrent-Name
- Quelle (manual, watch_file, watch_torrent, alldebrid_existing)
- AllDebrid ID
- Zeitstempel

### Download abgeschlossen
- Torrent-Name
- Anzahl Dateien
- Total size
- Download-Client (aria2)
- Zielordner
- Zeitstempel

## Anti-Spam

- **Deduplication**: Same message within 30 seconds is sent only once
- **Rate-Limiting**: Minimum 2 seconds between messages to the same URL
- **Discord 429**: Automatisches Warten bei Rate-Limit-Antwort von Discord
