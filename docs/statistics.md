# Statistiken

## Dashboard-Metriken (`GET /api/stats`)

| Feld | Beschreibung |
|------|-------------|
| `by_status` | Torrent-Zählung pro Status |
| `completed_count` | Anzahl abgeschlossener Torrents |
| `error_count` | Anzahl fehlerhafter Torrents |
| `total_count` | Gesamtanzahl Torrents |
| `success_rate_pct` | Erfolgsrate in % (null wenn keine Terminal-Torrents) |
| `total_completed_bytes` | Gesamte heruntergeladene Datenmenge |
| `active_downloads` | Aktive Downloads (downloading + processing + uploading + paused) |
| `queued_downloads` | Downloads in der Warteschlange |
| `total_blocked_files` | Anzahl gefilterter Dateien |
| `completed_last_24h` | Abgeschlossene Torrents in den letzten 24 Stunden |
| `completed_last_7d` | Abgeschlossene Torrents in den letzten 7 Tagen |
| `avg_download_duration_seconds` | Ø Download-Dauer in Sekunden |
| `avg_torrent_size_bytes` | Ø Torrent-Größe in Bytes |
| `finished_events` | Anzahl "Finished"-Events in der Event-Log |

## Detail-Statistiken (`GET /api/stats/detail`)

Zusätzlich:

| Feld | Beschreibung |
|------|-------------|
| `daily_completions` | Tägliche Abschlüsse der letzten 14 Tage |
| `sources` | Top-Quellen (manual, watch_file, etc.) |
| `totals.success_rate_pct` | Erfolgsrate in den Totals |

## Dashboard-Bug (behoben)

**Problem**: Die "Completed"-Karte zeigte immer 0.

**Ursache**: `_delete_magnet_after_completion()` setzte `status='deleted'` nach dem Löschen des Magneten bei AllDebrid. Dadurch zählte `by_status.completed` immer 0, weil alle abgeschlossenen Torrents im Status `deleted` landeten.

**Fix**: Der Status bleibt dauerhaft `'completed'`. Das erneute Polling wird durch `sync_alldebrid_status` verhindert, das bereits `status NOT IN ('completed', 'deleted', 'error')` filtert.
