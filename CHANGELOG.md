# Changelog

All notable changes to AllDebrid-Client will be documented in this file.

This repository uses a project-specific release scheme:

- New features: `vX.Y.0`
- Fixes, debugging, and maintenance: `vX.Y.Z`
- Fundamental or breaking structural changes: `vY.0.0`

Each shipped change must be reflected here and released with a matching Git commit and tag.

---

## [0.5.4] - 2026-04-14

### Fixed
- Reconciled aria2 downloads by stable target path before falling back to expiring AllDebrid URLs, preventing false “not in aria2” resets
- Reused existing aria2 jobs when the unlocked AllDebrid URL changed but the requested aria2 output path stayed the same
- Added regression coverage for path-based aria2 matching in queueing and reconciliation

## [0.5.3] - 2026-04-14

### Fixed
- Serialized aria2 queueing per download URL so duplicate checks now happen before `addUri` even under parallel enqueue races
- Added a regression test covering concurrent `ensure_download` calls for the same unlocked AllDebrid URL

## [0.5.2] - 2026-04-14

### Fixed
- Added startup-time aria2 duplicate cleanup so already queued duplicate jobs are removed before reconciliation and requeue logic runs
- Kept the startup aria2 reconciliation on the already-cleaned in-memory state to avoid a second dependency on immediate RPC refresh

## [0.5.1] - 2026-04-14

### Fixed
- Replaced the aria2 sync `system.multicall` polling with individual RPC calls so authenticated aria2 setups no longer fail with `The parameter at 0 has wrong type`
- Prevented duplicate queueing when AllDebrid or a repeated start path yields the same file entry more than once during aria2 preparation
- Restored correct aria2 state reconciliation so queued/downloading jobs are no longer misclassified as local errors just because sync polling failed

## [0.5.0] - 2026-04-14

### Added
- Added a first-class `aria2` download client with JSON-RPC delivery, duplicate protection by URI/path, start-paused support, and pause/resume endpoints
- Added download-file tracking fields for remote download IDs and client ownership so external delivery status can be merged back into the normal torrent lifecycle
- Added provider-state tracking fields for AllDebrid so provider progress and local transfer progress are no longer conflated

### Changed
- Reworked torrent delivery so direct downloads and aria2 now share the same preparation, filtering, logging, and completion flow
- Refreshed the settings UI and README to document the new direct/aria2 architecture and the removal of JDownloader
- Updated the Unraid template and release metadata to `v0.5.0`

### Removed
- Removed JDownloader from the active backend/API/UI flow and dropped the MyJDownloader dependency from runtime requirements

### Fixed
- Hardened the transition from `ready` on AllDebrid to actual download start by retrying file discovery before failing the torrent
- Preserved nested multi-file torrent paths instead of flattening everything down to a single filename
- Improved sync/finalization so aria2-backed downloads can still end as `completed`, emit `Finished`, and remove the source magnet from AllDebrid
- Added persistent polling-failure escalation so stuck or inconsistent AllDebrid states become visible in events and can be classified as errors when they keep failing

## [0.4.1] - 2026-04-13

### Changed
- Filtered files now trigger a separate partial webhook while the remaining files continue through the normal completion flow
- Discord webhooks now use the AllDebrid-Client name, logo, version footer, and a cleaner embed layout

### Fixed
- Torrents with filtered files now finish as `completed` when all remaining files succeed and are removed from AllDebrid as expected
- Fixed broken sidebar icon rendering and reduced the sidebar version label to the plain version number
- Cleaned up the dashboard overview block so the top section renders correctly again

## [0.4.0] - 2026-04-13

### Added
- Added dedicated sidebar tabs for GitHub, Buy Me a Coffee, detailed statistics, and the full changelog
- Added API endpoints for rich statistics and in-app changelog rendering
- Added a dedicated partial-download Discord webhook summary with total files, downloaded files, skipped files, and byte totals

### Changed
- Removed project/support links from the dashboard and moved them into dedicated navigation areas
- Centered the README header section and refreshed the documentation to match the current UI layout and release version
- Updated release metadata and container references to `v0.4.0`

### Fixed
- Stopped treating excluded-file partial runs like hard errors by separating the partial notification flow from error alerts

## [0.3.1] - 2026-04-13

### Fixed
- Hardened JDownloader handoff so accepted packages are actively moved out of the linkgrabber into the download list
- Added a recovery step during JDownloader status checks to promote lingering linkgrabber entries instead of leaving them stuck

## [0.3.0] - 2026-04-13

### Added
- Added a richer dashboard statistics section with queue health, finished monitor count, and completion insights
- Added a Buy Me a Coffee link in the dashboard and README

### Changed
- Removed legacy secondary-downloader references from the product, documentation, workflow descriptions, templates, and branding assets
- Refreshed the README to match the current feature set, release flow, badges, and support links

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
- JDownloader integration via MyJDownloader cloud device routing
- File filter system: block by extension (images blocked by default), keyword, and minimum size
- Import existing magnets already present on AllDebrid account
- Torrent detail view with file list, block reasons, and event history
- Docker and Docker Compose support
- MIT License

[0.4.0]: https://github.com/kroeberd/alldebrid-client/releases/tag/v0.4.0
[0.4.1]: https://github.com/kroeberd/alldebrid-client/releases/tag/v0.4.1
[0.3.1]: https://github.com/kroeberd/alldebrid-client/releases/tag/v0.3.1
[0.3.0]: https://github.com/kroeberd/alldebrid-client/releases/tag/v0.3.0
[0.2.1]: https://github.com/kroeberd/alldebrid-client/releases/tag/v0.2.1
[0.2.0]: https://github.com/kroeberd/alldebrid-client/releases/tag/v0.2.0
[0.1.0]: https://github.com/kroeberd/alldebrid-client/releases/tag/v0.1.0
[0.0.1]: https://github.com/kroeberd/alldebrid-client/releases/tag/v0.0.1
