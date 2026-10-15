# Changelog

All notable changes to this plugin are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow the
manifest `version` and the matching git tag.

## [0.2.0] - 2026-10-15

Cluster readiness (core cluster mode, N instances on one MariaDB 12).

### Fixed
- Table bootstrap runs under `db_instance.advisory_lock("lyndrix_bootstrap")`
  (when the core offers it), off the event loop, and treats "already exists"
  (1050/1060/1061) as a peer's success. A failed bootstrap is retried instead of
  leaving the plugin not-ready until the next restart.
- `identify()` no longer fails when another instance inserts the same
  identifier first: the lookup is re-run once and the observation is absorbed
  into the winning entity.
- Writes (`identify`, `register`, `merge`, `update_entity`) use READ COMMITTED
  sessions and retry on MariaDB 1020/1205/1213.
- The same open conflict can no longer be filed twice: new unique
  `registry_conflicts.open_fingerprint` column (NULL once resolved/dismissed),
  added by an idempotent migration that also backfills existing open conflicts.

### Changed
- `identify()` refreshes `last_seen_at` of an otherwise unchanged entity at most
  every 30 s.
- `POST /catalog/reload` announces `entity_registry:catalog_reloaded` cluster-wide;
  peers reload their catalog.
- `health()` runs `SELECT 1` plus the open-conflict count instead of four counts.

## [0.1.0] - 2026-10-14

### Added
- Initial release: entities, identifiers, relations, conflicts, REST API, bus API,
  React UI.
