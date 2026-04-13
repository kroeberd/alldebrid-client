# Changelog

All notable changes to AllDebrid-Client will be documented in this file.

This repository uses a project-specific release scheme:

- New features: `vX.Y.0`
- Fixes, debugging, and maintenance: `vX.Y.Z`
- Fundamental or breaking structural changes: `vY.0.0`

Each shipped change must be reflected here and released with a matching Git commit and tag.

---

## [0.2.1] - 2026-04-13

### Changed
- Added a dedicated `Finished` monitor/event entry when a torrent has fully completed
- Automatically removes completed downloads from AllDebrid after successful completion handling
- Throttles Discord webhook delivery to one message every 5 seconds per webhook URL to reduce timeout pressure

## [0.2.0] - 2026-04-13

### Added
- Added GitHub Actions workflow to update the Docker Hub description from the repository README
- Added smart multi-architecture build and publish workflow for GHCR and Docker Hub
- Added GitHub issue templates for bug reports and feature requests plus issue template configuration
- Added a tracked `VERSION` file for release-aware automation triggers

### Changed
- Updated repository release references to use `v0.2.0` for the current feature release

## [0.1.0] - 2026-04-13

### Added
- Added a documented Docker image build workflow for local and tagged releases
- Added repository release rules covering changelog updates, commit discipline, and tag naming
- Added branded logo usage to the README and frontend static assets

### Changed
- Updated the frontend branding to display the project logo instead of a placeholder icon
- Updated the Unraid template to reference the tracked SVG logo asset and the new release version
- Aligned repository documentation around the `vX.Y.Z` tagging scheme requested for future releases

### Fixed
- Fixed the Docker/branding documentation mismatch where the Unraid template referenced a non-existent icon path

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

[0.2.1]: https://github.com/kroeberd/alldebrid-client/releases/tag/v0.2.1
[0.2.0]: https://github.com/kroeberd/alldebrid-client/releases/tag/v0.2.0
[0.1.0]: https://github.com/kroeberd/alldebrid-client/releases/tag/v0.1.0
[0.0.1]: https://github.com/kroeberd/alldebrid-client/releases/tag/v0.0.1
