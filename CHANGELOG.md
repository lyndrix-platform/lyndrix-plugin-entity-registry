# Changelog

All notable changes to this plugin are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow the
manifest `version` and the matching git tag.

## [0.3.0] - 2026-10-18

Maintenance-window read-model (feature "maintenance windows", part P5a) and the
`iac.service` identifier scheme the IaC orchestrator needs to identify service
instances. Roll out **before** orchestrator 1.6.0.

### Added
- Catalog scheme `iac.service` (`<host>/<service.name>`, priority 20) for
  `entity_type: service`; the orchestrator relates them to their host via `runs_on`.
- Table `registry_maintenance` (created idempotently under the bootstrap lock):
  one row per `(entity, source)` with the published windows, one per
  `(entity, source, override_id)` for an override. Unique key over all four.
- Bus consumers `maintenance:windows_published` (`replaces: true` drops the
  source's earlier rows in the same transaction; unknown `ci_id`s are skipped and
  counted; merge tombstones resolve to the survivor; the item's `kind` is
  informational — the entity's type stays authoritative),
  `maintenance:override_started` (upsert per `(entity, source, override_id)`),
  `maintenance:override_ended` (`ended_at` + `linger_minutes`; the first end wins).
  One `entity_registry:maintenance_changed {source, kind, count, …}` (cluster-wide) per event
  instead of an `entity_updated` per entity.
- Field `maintenance` on `GET /entities/{uuid}` (full: `active`, `until`,
  `source`, `next_start`, `note`, `windows[]` per source with `inherited_from`,
  active `overrides[]`, `incidents`, `deploy_policy`) and compact
  `{active, until, source}` on list rows and `resolve()` candidates.
  Evaluated at read time through `core.api.maintenance` (API 1.13); on an
  older core (`min_core_version` stays 1.4.0) `active` is `null` and the raw
  data is served — overrides are evaluated either way.
- `GET /entities?in_maintenance=true` (page-wise filter), batch
  `GET /maintenance?ci_ids=a,b,…` (max 200, tombstones resolved, unknown → `null`),
  `GET /entities/{uuid}/host` (target of the outgoing `runs_on`; 404 without one).
- Expired overrides are purged one day after their effective end, piggybacking on
  the next override event (no timer in the plugin).
- React UI: `MaintenanceBadge` (`lx-badge--maintenance`, falls back to the paused
  colour on older shells) in the list and the detail header, list filter
  "in maintenance only", detail card **Maintenance** (current/next window,
  overrides, published windows per source with inheritance, incident rule,
  deploy policy). Locales de/en.

### Changed
- `tests/conftest.py` now hosts the shared `svc` fixture plus `entrypoint`/`bus`
  fixtures that load `entrypoint.py` the way the core does — the bus handlers are
  covered for the first time.

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
