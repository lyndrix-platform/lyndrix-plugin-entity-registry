# Entity Registry — Documentation

## Overview

The Entity Registry assigns every configuration item (CI) of the platform **a stable UUID** and
records under which identifiers the various plugins know the same thing. Previously every
cross-plugin reference was the IaC hostname as a string (Monitoring `host:<name>`,
Docker Manager host `name`, Server Manager `external_id`) — a rename broke all connections.
With the registry, each plugin stores the UUID; the registry ensures that a renamed host
(same Proxmox VMID) keeps the same UUID.

The registry is deliberately **small**: it identifies, links and resolves conflicts. It
contains no process logic — that lives in the ITSM plugin.

## Architecture

```
catalog/entity_types.yml   # entity types, identifier schemas, relations
app/model/models.py        # Entity / Identifier / Relation / Conflict / MaintenanceEntry
app/model/database.py      # session helpers + table bootstrap (hand-written migration)
app/logic/catalog.py       # validated catalog loader (pydantic)
app/logic/service.py       # identify / register / merge / relate / retire / maintenance read model
app/logic/maintenance.py   # payload normalisation + evaluation via core.api.maintenance (optional)
app/api/__init__.py        # REST router — both UIs use only this
app/ui/react/              # React UI (bundle → app/ui/static/ui_bundle.js)
```

### Data model

| Table | Content |
|---|---|
| `registry_entities` | `uuid` (PK), `entity_type`, `display_name`, `status` (`active` / `retired` / `merged`), `owner_source`, `sources` (JSON, "seen by"), `attributes` (JSON), `merged_into`, timestamps |
| `registry_identifiers` | `(scheme, value)` **unique** → `entity_uuid`. Exactly one owner per pair — the database enforces this. |
| `registry_relations` | directed, named edge `(from_uuid, to_uuid, relation)` unique |
| `registry_conflicts` | parked ambiguities with candidates, triggering identifiers, `status` (`open` / `resolved` / `dismissed`) |
| `registry_maintenance` | read model of the maintenance windows (from 0.3.0): `kind` (`window` / `override`), `source`, `override_id` (`""` for windows), `windows` (JSON, normal form), `inherited_from`, `deploy_policy`, `incidents` (JSON), `until` / `started_at` / `ended_at` / `linger_minutes` / `reason` (overrides); **unique** over `(entity_uuid, source, kind, override_id)`. See [Maintenance windows on a CI](#maintenance-windows-on-a-ci). |
| `registry_maintenance_tombstones` | records a `maintenance:override_ended` that arrives before its `override_started` (from 0.4.0); the late start is then stored as already ended. |

**Identifiers vs. attributes.** Identifiers are keys that uniquely designate an entity
(`pve.vm`, `iac.host`, `fqdn`, …). Weak, reusable facts such as IP addresses are **not**
identifiers but attributes — otherwise a newly assigned IP would merge two machines.

### Reconciliation (`identify`)

`identify(entity_type, identifiers, display_name, attributes, source)` finds or creates the
entity behind a set `{schema: value}`:

1. All passed identifiers are looked up (merge tombstones are resolved to the surviving
   entity).
2. **No hit** → new UUID, all identifiers are created.
3. **Exactly one hit** → missing identifiers are learned as aliases (so a rename survives: new
   `iac.host`, same `pve.vm` ⇒ same UUID), `display_name` and `attributes` are updated, the
   source is added to `sources`, and a retired entity becomes active again.
4. **Multiple hits** → **no automatic merge.** The entity matched via the schema with the lowest
   `priority` number is returned; a conflict is recorded (once per candidate set) and
   `entity_registry:conflict` is emitted. Merging is a decision for humans (UI → Conflicts).

`merge(keep, drop)` re-attaches identifiers and relations, unifies attributes/sources, and leaves
the old record as a tombstone (`status=merged`, `merged_into`) — old UUIDs continue to resolve.
Since 0.4.0 the surviving entity also takes over the maintenance windows and overrides of the
dropped one. Concurrent merges in opposite directions cannot produce a tombstone cycle: the
entities are locked in UUID order, and the loser receives HTTP 409.

## Catalog (`catalog/entity_types.yml`)

```yaml
entity_types:        # host, hypervisor, service, container, network, itsm_record, generic
  - {id: host, label: Host, icon: dns, description: ...}
identifier_schemes:  # lower priority wins on conflicts
  - {id: pve.vm, label: Proxmox VM/CT, priority: 10}      # "<node>/<vmid>"
  - {id: iac.host, label: IaC inventory host key, priority: 20}
  - {id: iac.service, label: IaC service instance, priority: 20}  # "<host>/<service.name>" (from 0.3.0)
  - {id: fqdn, priority: 30}
  - {id: docker.container, priority: 40}                   # "<host>/<name>"
  - {id: server_manager.id, priority: 50}
  - {id: monitoring.monitor, priority: 60}
  - {id: itsm.number, priority: 10}
  - {id: bmc.reconid, priority: 10}
  - {id: external, priority: 90}                            # "<system>:<id>"
relations:           # runs_on/hosts, depends_on/required_by, member_of/has_member, affects/affected_by, related_to
```

Unknown types, schemas or relations are rejected (HTTP 400). `POST /catalog/reload`
reloads the file; an invalid catalog leaves the old version active.

## REST API

Base: `/api/plugins/lyndrix.plugin.entity_registry/` (authenticated; permissions
`plugin:lyndrix.plugin.entity_registry:api:read` resp. `api:write`).

`api:write` is granted in the core to every normal user (`INT_USER`). Everything irreversible or
operator-relevant therefore requires its own permission
`plugin:lyndrix.plugin.entity_registry:registry:admin` (role `admin`, group `INT_ADMIN`):
`/entities/{uuid}/merge`, `/entities/{uuid}/retire`, `/register`, `/conflicts/{id}/dismiss`,
`/catalog/reload`, and the free choice of `source` in `/identify` and `/identify/batch`.
Without this permission `source` is optional and is derived from the identity (`user:<name>`);
a deviating value is rejected with 403. The bus interface is unaffected by this.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/identify` | Find-or-create; response `{uuid, created, conflict, entity}` |
| `POST` | `/identify/batch` | `{items: [...]}` → result per entry (`created`/`conflicts`/`failed`) |
| `POST` | `/register` | create/update an entity with its own UUID (e.g. ITSM records) |
| `GET` | `/entities?type=&q=&status=&limit=&offset=&in_maintenance=` | list; `q` searches the name **and** identifier values. Each row carries `maintenance: {active, until, source}`. `in_maintenance=true` keeps only entities that are **now** in maintenance (evaluated per page — `total` then counts the matches on the page) |
| `GET` | `/entities/{uuid}` | detail incl. identifiers, relations and the full `maintenance` field (follows merge tombstones) |
| `GET` | `/entities/{uuid}/host` | host entity via the outgoing `runs_on` relation: `{host, relation}`; 404 without the relation |
| `GET` | `/maintenance?ci_ids=a,b,c` | batch read model for consumers (max. 200 IDs): `{now, maintenance: {ci_id: state \| null}}`; tombstones resolve to the surviving entity (`uuid` in the state), unknown IDs → `null` |
| `PATCH` | `/entities/{uuid}` | change `display_name` / `attributes` |
| `POST` | `/entities/{uuid}/retire` | retire (a new `identify` reactivates) |
| `POST` | `/entities/{uuid}/merge` | merge `{drop_uuid}` into this entity |
| `GET` | `/lookup?scheme=&value=` | entity for an identifier |
| `GET` | `/resolve?ref=&type=` | resolve one reference to **exactly one** entity: UUID (follows merge tombstones), `schema:value` (`pve.vm:hydra/8003`), a bare identifier value (`docker-hydra`) or a display name. Response is always 200: `{entity\|null, match, candidates}` — on ambiguity `entity: null`, `match: "ambiguous"` and the candidates; it never guesses and never creates |
| `POST` / `DELETE` | `/entities/{uuid}/relations[/{relation}/{to_uuid}]` | relations |
| `GET` | `/conflicts?status=open` | conflict list |
| `POST` | `/conflicts/{id}/dismiss` | dismiss a conflict ("these really are two things") |
| `GET` / `POST` | `/catalog` / `/catalog/reload` | read / reload the catalog |
| `GET` | `/stats` | counters per type, identifier, relation, open conflicts |
| `GET` | `/access` | what the caller may do: `{write, admin, source}` — the UI uses this to hide admin actions |

All timestamps of the API are ISO 8601 with `Z` (UTC); since 0.4.0 `created_at`/`updated_at` are written from Python UTC instead of the DB `NOW()`.

## Event bus

### Consumed

| Topic | Payload | Behaviour |
|---|---|---|
| `entity_registry:identify` | `{entity_type, identifiers, display_name?, attributes?, source, request_id?}` or `{items: [...], request_id?}` | reply on `entity_registry:identified` with the same `request_id`: `{ok, uuid, created, conflict}` resp. `{ok, results: [...]}` |
| `entity_registry:register` | `{uuid, entity_type, display_name, identifiers?, attributes?, source}` | bring-your-own-UUID |
| `entity_registry:lookup` | `{ref, entity_type?, request_id}` or `{scheme, value, request_id}` | read-only resolution like `GET /resolve`; reply on `entity_registry:lookup_result`: `{request_id, ok, entity\|null, match, candidates}`. For example, the ITSM plugin uses this to resolve `docker-hydra` to a CI UUID |
| `entity_registry:relate` / `unrelate` | `{from_uuid, to_uuid, relation, source?}` | create / remove a relation |
| `entity_registry:retire` | `{uuid, source?}` | retire |
| `maintenance:windows_published` | `{source, published_at, replaces, items: [{ci_id, kind, name, windows, inherited_from, deploy_policy?, incidents?, host_ci_id?}]}` | read-model feed (core API 1.13): with `replaces: true` all window rows of this `source` are replaced in **one** transaction; unknown `ci_id`s are skipped and counted, merge tombstones are resolved; the item's `kind`/`name`/`host_ci_id` are informational (the type lives on the entity) |
| `maintenance:override_started` | `{override_id, ci_ids, until, reason, source, started_at, linger_minutes}` | upsert per `(entity, source, override_id)`; a redelivery does not create a second row |
| `maintenance:override_ended` | `{override_id, source, ended_at, linger_minutes}` | sets `ended_at`/`linger_minutes` (the first end wins; an end before the start is recorded in `registry_maintenance_tombstones`, a redelivered end does not move `until`); only rows of the same `source`, if it is sent along |

For the request/response case the sending plugin uses `await ctx.request("entity_registry:identify",
payload, reply_topic="entity_registry:identified", timeout=5)` (core API ≥ 1.4). If no reply
comes (registry not loaded), `ctx.request` returns `None` — the caller then continues without a
UUID and fetches it on the next sync.

Invalid payloads (not an object, required fields missing, database not ready) are answered with
`entity_registry:ingest_failed` — also for the `maintenance:*` topics.

### Emitted

`entity_registry:identified` and `entity_registry:lookup_result` (replies), `entity_created`,
`entity_updated` (`{entity, changes}`), `entity_merged` (`{keep, drop_uuid}`), `entity_retired`,
`conflict`, `ingest_failed` (`{topic, error, payload_keys}` — errors on pushes without `request_id`),
`entity_registry:catalog_reloaded` (`{reloaded_at}`, cluster-wide after `POST /catalog/reload`),
`entity_registry:maintenance_changed` (`cluster=True`; `{source, kind: window|override, count, override_id?, …}` —
**one** aggregate event per processed `maintenance:*` event, not one `entity_updated` per entity;
UIs reload upon it).

## Maintenance windows on a CI

From 0.3.0 the registry is a **consumer** of the maintenance window contract (core API 1.13,
`lyndrix-core/docs/plugins.md` → *Maintenance windows*). It owns no windows: the IaC orchestrator
publishes what controller and `service.yml` define, monitoring publishes its manual windows; every
running deploy is an override. The registry stores this per source in `registry_maintenance` and
evaluates **at read time** — without timers, without per-entity events.

**Evaluation.** `GET /entities/{uuid}` returns

```json
"maintenance": {
  "active": true, "until": "2026-11-03T01:00:00Z", "source": "override",
  "next_start": "2026-11-09T01:00:00Z", "note": "deploy aac-grafana",
  "windows": [{"source": "iac-orchestrator", "windows": [{"type": "weekly", "days": ["sun"], "start": "02:00", "end": "04:00", "tz": "Europe/Berlin"}],
               "inherited_from": "stage_default", "deploy_policy": "window",
               "incidents": {"enabled": true, "offline_minutes": 10, "severity": "high", "auto_resolve": true}, "updated_at": "…"}],
  "overrides": [{"id": "job-4711", "source": "iac-orchestrator", "until": "…", "started_at": "…", "ended_at": null, "linger_minutes": 15, "reason": "deploy aac-grafana"}],
  "incidents": {"…": "…"}, "deploy_policy": "window"
}
```

- `active`/`until`/`source`/`next_start` come from `core.api.maintenance.effective_state` over the
  windows of **all** sources plus the stored overrides. An active override wins
  (`source: "override"`), `until` is the later of the two ends. `overrides` lists only those
  currently active (until `until`, and after `override_ended` for `linger_minutes`).
- `incidents` and `deploy_policy` at the top level come from the first source (alphabetically)
  that supplies them — today only the orchestrator.
- **Without the core library** (core < 1.11, `min_core_version` stays 1.4.0) windows are stored and
  returned raw; `active` is then `null` as soon as windows exist ("not evaluable"). Overrides need
  no library and are evaluated anyway.
- Expired overrides are ignored on read and deleted one day after their effective end (ended rows
  after two days, because `ended_at + linger` is not portable SQL); the sweep runs along with the
  next override event.

**Idempotency and cluster.** `maintenance:*` is sent locally by the emitter; the registry runs on
every instance, and the local handler writes the shared DB. Nevertheless all handlers are
idempotent (unique key, `delete`+`insert` per `source` in one transaction, upsert per
`override_id`, retry on 1020/1205/1213), so a later `cluster=True` delivery does no harm.
`replaces` applies only within the same `source` — no emitter can delete another's windows.

**Host of a service.** The orchestrator identifies services as `entity_type: service` with
`iac.service = "<host>/<service.name>"` and sets `runs_on` service → host.
`GET /entities/{uuid}/host` follows this edge (404 if it is missing); consumers that need the host
even without the registry use the `host_ci_id` from the events.

## Cluster operation (from 0.2.0)

If the core runs with several instances (`LYNDRIX_CLUSTER_MODE=cluster`), the registry works on
every instance; the entire state lives in the shared MariaDB.

- **Bootstrap:** tables and migrations run under `db_instance.advisory_lock("lyndrix_bootstrap")`
  in the thread pool. A lost race ("table/column/index already exists", 1050/1060/1061) counts as
  success; if the bootstrap fails, it is retried up to five times.
- **Write access** runs with READ COMMITTED and is briefly retried on MariaDB errors 1020/1205/1213
  (snapshot isolation, lock timeout, deadlock). `last_seen_at` is written at most every 30 s when
  the object is otherwise unchanged.
- **Races on unique keys:** if another instance creates the same identifier at the same time,
  `identify()` re-reads and merges the observation into the winning entity. Open conflicts carry
  the unique key `open_fingerprint` (NULL after resolution) and so are recorded only once.
- **Catalog:** `POST /catalog/reload` reloads locally and sends `entity_registry:catalog_reloaded`
  with `cluster=True`; the other instances then reload as well.
- **`health()`** only checks `SELECT 1` and counts open conflicts.

## Roles

| Role | Permissions | Auto-mapping |
|---|---|---|
| `admin` | `api:read`, `api:write`, `registry:admin` | group `INT_ADMIN` |
| `viewer` | `api:read` | — |

## React UI

Route `/registry` (in the shell under `/apps/lyndrix-plugin-entity_registry/registry`). Deep link to
an entity: `?uuid=<uuid>`. Views: list with type/status filter and search, detail
(identifiers, attributes, relations, "seen by", merge, retire), conflict queue with
"Keep this one, merge the others" resp. "Dismiss".

A CI in maintenance carries the **Maintenance** badge in the list and in the detail header (class
`lx-badge--maintenance`, colour token `--lx-state-maintenance`; older shells fall back to the
paused colour). The list filter "Only in maintenance" uses `?in_maintenance=true`. The detail card
**Maintenance** shows the current window or override (with end and reason), the next window, the
active overrides, the published windows per source including `inherited_from`, the incident rule
and the deploy policy — or the note that the core cannot evaluate windows.

If the ITSM plugin is loaded, the detail view additionally shows the card **Service Management**:
the open records that reference this CI (link into the record), "New incident"
(opens the ITSM form with the CI pre-filled as *affected* — `/apps/lyndrix-plugin-itsm/itsm?new=incident&ci=<uuid>`)
and "All records" (`?ci=<uuid>`). Without the ITSM plugin or permission the card simply does not appear.

## Development & tests

```bash
# from the plugin directory
npm install && npm run build          # React bundle → app/ui/static/ui_bundle.js
ruff check entrypoint.py app tests

# Tests need the core on the PYTHONPATH — easiest in the dev container:
docker cp . lyndrix-core-dev:/tmp/er
docker exec -w /tmp/er -e PYTHONPATH=/app:/tmp/pytest-sm:/tmp/er \
  -e LYNDRIX_CLUSTER_MODE=single -e LYNDRIX_COORDINATOR=local \
  lyndrix-core-dev python -m pytest -q tests
```

`tests/conftest.py` imports `core.services` before `core.api` (works around a circular import in
the core) and provides the fixtures `svc` (service against in-memory SQLite, foreign keys enabled),
`entrypoint` (loads `entrypoint.py` like the core does as a package member) and `bus` (runs
`setup()` against a recording context → bus handlers per topic). `tests/test_maintenance.py`
covers the maintenance read model including the handlers and the path without the core library
(`monkeypatch` on `maintenance.mw = None`). `(scheme, value) IN (...)` was additionally verified
against MariaDB.

## Limits (phase 1)

- No hard deletion via the API — only `retire` and `merge` (audit trail remains).
- Conflicts are only reported, never resolved automatically.
- Relations are simple edges without attributes; graph display is a list.
