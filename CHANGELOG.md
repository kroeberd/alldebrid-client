# Changelog

All notable changes to AllDebrid-Client will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.0.1] - 2026-04-12

### Added
- Initial release of AllDebrid-Client
- FastAPI backend with async architecture
- SQLite database to track torrents, files, and events — prevents re-downloading known hashes
- AllDebrid API integration: upload magnets, poll status, unlock links, delete after completion
- Watch folder support: auto-process `.torrent` and `.magnet`/`.txt` files, move to `processed/` after import
- Background scheduler: configurable watch interval and AllDebrid poll interval
- Web UI: Dashboard with stats, Torrent queue, Event log, Settings editor
- Discord webhook notifications: notify on added, finished, and error events
- aria2 / AriaNg integration via JSON-RPC
- JDownloader integration via FlashGot endpoint with optional Basic Auth
- File filter system: block by extension (images blocked by default), keyword, and minimum size
- Import existing magnets already present on AllDebrid account
- Torrent detail view with file list, block reasons, and event history
- Docker and Docker Compose support
- MIT License

[0.0.1]: https://github.com/your-username/alldebrid-client/releases/tag/v0.0.1
