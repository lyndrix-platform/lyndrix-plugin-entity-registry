# Entity Registry — Dokumentation

## Überblick

Die Entity Registry vergibt jedem Configuration Item (CI) der Plattform **eine stabile UUID** und
hält fest, unter welchen Kennungen die verschiedenen Plugins dasselbe Ding kennen. Vorher war
jede Plugin-übergreifende Referenz der IaC-Hostname als String (Monitoring `host:<name>`,
Docker-Manager Host-`name`, Server-Manager `external_id`) — eine Umbenennung riss alle Verbindungen
ab. Mit der Registry speichert jedes Plugin die UUID; die Registry sorgt dafür, dass ein
umbenannter Host (gleiche Proxmox-VMID) dieselbe UUID behält.

Die Registry ist bewusst **klein**: Sie identifiziert, verknüpft und löst Konflikte auf. Sie
enthält keine Prozesslogik — die lebt im ITSM-Plugin.

## Architektur

```
catalog/entity_types.yml   # Entitätstypen, Identifier-Schemata, Beziehungen
app/model/models.py        # Entity / Identifier / Relation / Conflict / MaintenanceEntry
app/model/database.py      # Session-Helfer + Tabellen-Bootstrap (handgeschriebene Migration)
app/logic/catalog.py       # validierter Katalog-Loader (pydantic)
app/logic/service.py       # identify / register / merge / relate / retire / Wartungs-Read-Model
app/logic/maintenance.py   # Payload-Normalisierung + Auswertung über core.api.maintenance (optional)
app/api/__init__.py        # REST-Router — beide UIs nutzen nur diesen
app/ui/react/              # React-UI (Bundle → app/ui/static/ui_bundle.js)
```

### Datenmodell

| Tabelle | Inhalt |
|---|---|
| `registry_entities` | `uuid` (PK), `entity_type`, `display_name`, `status` (`active` / `retired` / `merged`), `owner_source`, `sources` (JSON, „gesehen von“), `attributes` (JSON), `merged_into`, Zeitstempel |
| `registry_identifiers` | `(scheme, value)` **eindeutig** → `entity_uuid`. Genau ein Besitzer pro Paar — die Datenbank erzwingt das. |
| `registry_relations` | gerichtete, benannte Kante `(from_uuid, to_uuid, relation)` eindeutig |
| `registry_conflicts` | geparkte Mehrdeutigkeiten mit Kandidaten, auslösenden Identifiern, `status` (`open` / `resolved` / `dismissed`) |
| `registry_maintenance` | Read-Model der Wartungsfenster (ab 0.3.0): `kind` (`window` / `override`), `source`, `override_id` (`""` bei Fenstern), `windows` (JSON, Normalform), `inherited_from`, `deploy_policy`, `incidents` (JSON), `until` / `started_at` / `ended_at` / `linger_minutes` / `reason` (Overrides); **eindeutig** über `(entity_uuid, source, kind, override_id)`. Siehe [Wartungsfenster am CI](#wartungsfenster-am-ci). |

**Identifier vs. Attribute.** Identifier sind Schlüssel, die eine Entität eindeutig bezeichnen
(`pve.vm`, `iac.host`, `fqdn`, …). Schwache, wiederverwendbare Fakten wie IP-Adressen sind **keine**
Identifier, sondern Attribute — sonst würde eine neu vergebene IP zwei Maschinen zusammenführen.

### Reconciliation (`identify`)

`identify(entity_type, identifiers, display_name, attributes, source)` findet oder erzeugt die
Entität hinter einer Menge `{schema: wert}`:

1. Alle übergebenen Identifier werden nachgeschlagen (Merge-Tombstones werden zur überlebenden
   Entität aufgelöst).
2. **Kein Treffer** → neue UUID, alle Identifier werden angelegt.
3. **Genau ein Treffer** → fehlende Identifier werden als Aliase gelernt (daher überlebt eine
   Umbenennung: neuer `iac.host`, gleiche `pve.vm` ⇒ gleiche UUID), `display_name` und `attributes`
   werden aktualisiert, die Quelle wird zu `sources` ergänzt, eine stillgelegte Entität wird wieder
   aktiv.
4. **Mehrere Treffer** → **kein automatischer Merge.** Zurückgegeben wird die Entität, die über
   das Schema mit der niedrigsten `priority`-Zahl getroffen wurde; ein Konflikt wird (einmal pro
   Kandidatenmenge) abgelegt und `entity_registry:conflict` emittiert. Zusammenführen ist eine
   Entscheidung für Menschen (UI → Konflikte).

`merge(keep, drop)` hängt Identifier und Beziehungen um, vereinigt Attribute/Quellen, lässt den
alten Datensatz als Tombstone (`status=merged`, `merged_into`) stehen — alte UUIDs lösen weiter auf.

## Katalog (`catalog/entity_types.yml`)

```yaml
entity_types:        # host, hypervisor, service, container, network, itsm_record, generic
  - {id: host, label: Host, icon: dns, description: ...}
identifier_schemes:  # niedrigere priority gewinnt bei Konflikten
  - {id: pve.vm, label: Proxmox VM/CT, priority: 10}      # "<node>/<vmid>"
  - {id: iac.host, label: IaC inventory host key, priority: 20}
  - {id: iac.service, label: IaC service instance, priority: 20}  # "<host>/<service.name>" (ab 0.3.0)
  - {id: fqdn, priority: 30}
  - {id: docker.container, priority: 40}                   # "<host>/<name>"
  - {id: server_manager.id, priority: 50}
  - {id: monitoring.monitor, priority: 60}
  - {id: itsm.number, priority: 10}
  - {id: bmc.reconid, priority: 10}
  - {id: external, priority: 90}                            # "<system>:<id>"
relations:           # runs_on/hosts, depends_on/required_by, member_of/has_member, affects/affected_by, related_to
```

Unbekannte Typen, Schemata oder Beziehungen werden abgelehnt (HTTP 400). `POST /catalog/reload`
lädt die Datei neu; ein ungültiger Katalog lässt die alte Version aktiv.

## REST-API

Basis: `/api/plugins/lyndrix.plugin.entity_registry/` (authentifiziert; Rechte
`plugin:lyndrix.plugin.entity_registry:api:read` bzw. `api:write`).

| Methode | Pfad | Zweck |
|---|---|---|
| `POST` | `/identify` | Find-or-create; Antwort `{uuid, created, conflict, entity}` |
| `POST` | `/identify/batch` | `{items: [...]}` → Ergebnis pro Eintrag (`created`/`conflicts`/`failed`) |
| `POST` | `/register` | Entität mit eigener UUID anlegen/aktualisieren (z. B. ITSM-Datensätze) |
| `GET` | `/entities?type=&q=&status=&limit=&offset=&in_maintenance=` | Liste; `q` sucht in Name **und** Identifier-Werten. Jede Zeile trägt `maintenance: {active, until, source}`. `in_maintenance=true` behält nur Entitäten, die **jetzt** in Wartung sind (seitenweise ausgewertet — `total` zählt dann die Treffer der Seite) |
| `GET` | `/entities/{uuid}` | Detail inkl. Identifier, Beziehungen und vollem `maintenance`-Feld (folgt Merge-Tombstones) |
| `GET` | `/entities/{uuid}/host` | Host-Entität über die ausgehende `runs_on`-Beziehung: `{host, relation}`; 404 ohne Beziehung |
| `GET` | `/maintenance?ci_ids=a,b,c` | Batch-Read-Model für Konsumenten (max. 200 IDs): `{now, maintenance: {ci_id: state \| null}}`; Tombstones lösen zur überlebenden Entität auf (`uuid` im State), unbekannte IDs → `null` |
| `PATCH` | `/entities/{uuid}` | `display_name` / `attributes` ändern |
| `POST` | `/entities/{uuid}/retire` | stilllegen (ein erneutes `identify` reaktiviert) |
| `POST` | `/entities/{uuid}/merge` | `{drop_uuid}` in diese Entität zusammenführen |
| `GET` | `/lookup?scheme=&value=` | Entität zu einem Identifier |
| `GET` | `/resolve?ref=&type=` | Eine Referenz zu **genau einer** Entität auflösen: UUID (folgt Merge-Tombstones), `schema:wert` (`pve.vm:hydra/8003`), reiner Identifier-Wert (`docker-hydra`) oder Anzeigename. Antwort immer 200: `{entity\|null, match, candidates}` — bei Mehrdeutigkeit `entity: null`, `match: "ambiguous"` und die Kandidaten; es wird nie geraten und nie angelegt |
| `POST` / `DELETE` | `/entities/{uuid}/relations[/{relation}/{to_uuid}]` | Beziehungen |
| `GET` | `/conflicts?status=open` | Konfliktliste |
| `POST` | `/conflicts/{id}/dismiss` | Konflikt verwerfen („das sind wirklich zwei Dinge“) |
| `GET` / `POST` | `/catalog` / `/catalog/reload` | Katalog lesen / neu laden |
| `GET` | `/stats` | Zähler je Typ, Identifier, Beziehungen, offene Konflikte |

## Event-Bus

### Konsumiert

| Topic | Payload | Verhalten |
|---|---|---|
| `entity_registry:identify` | `{entity_type, identifiers, display_name?, attributes?, source, request_id?}` oder `{items: [...], request_id?}` | Antwort auf `entity_registry:identified` mit demselben `request_id`: `{ok, uuid, created, conflict}` bzw. `{ok, results: [...]}` |
| `entity_registry:register` | `{uuid, entity_type, display_name, identifiers?, attributes?, source}` | Bring-your-own-UUID |
| `entity_registry:lookup` | `{ref, entity_type?, request_id}` oder `{scheme, value, request_id}` | Nur-Lese-Auflösung wie `GET /resolve`; Antwort auf `entity_registry:lookup_result`: `{request_id, ok, entity\|null, match, candidates}`. So löst z. B. das ITSM-Plugin `docker-hydra` zur CI-UUID auf |
| `entity_registry:relate` / `unrelate` | `{from_uuid, to_uuid, relation, source?}` | Beziehung anlegen / entfernen |
| `entity_registry:retire` | `{uuid, source?}` | stilllegen |
| `maintenance:windows_published` | `{source, published_at, replaces, items: [{ci_id, kind, name, windows, inherited_from, deploy_policy?, incidents?, host_ci_id?}]}` | Read-Model-Feed (Core-API 1.13): bei `replaces: true` werden alle Fenster-Zeilen dieser `source` in **einer** Transaktion ersetzt; unbekannte `ci_id`s werden übersprungen und gezählt, Merge-Tombstones aufgelöst; `kind`/`name`/`host_ci_id` des Items sind informativ (der Typ steht an der Entität) |
| `maintenance:override_started` | `{override_id, ci_ids, until, reason, source, started_at, linger_minutes}` | Upsert je `(Entität, source, override_id)`; eine erneute Zustellung legt keine zweite Zeile an |
| `maintenance:override_ended` | `{override_id, source, ended_at, linger_minutes}` | setzt `ended_at`/`linger_minutes` (das erste Ende gewinnt); nur Zeilen derselben `source`, wenn sie mitgeschickt wird |

Für den Request/Response-Fall nutzt das sendende Plugin `await ctx.request("entity_registry:identify",
payload, reply_topic="entity_registry:identified", timeout=5)` (Core-API ≥ 1.4). Kommt keine
Antwort (Registry nicht geladen), liefert `ctx.request` `None` — der Aufrufer arbeitet dann ohne
UUID weiter und holt sie beim nächsten Sync nach.

Ungültige Payloads (kein Objekt, Pflichtfelder fehlen, Datenbank nicht bereit) werden mit
`entity_registry:ingest_failed` beantwortet — auch für die `maintenance:*`-Topics.

### Emittiert

`entity_registry:identified` und `entity_registry:lookup_result` (Antworten), `entity_created`,
`entity_updated` (`{entity, changes}`), `entity_merged` (`{keep, drop_uuid}`), `entity_retired`,
`conflict`, `ingest_failed` (`{topic, error, payload_keys}` — Fehler bei Pushes ohne `request_id`),
`entity_registry:catalog_reloaded` (`{reloaded_at}`, clusterweit nach `POST /catalog/reload`),
`entity_registry:maintenance_changed` (`{source, kind: window|override, count, override_id?, …}` —
**ein** Sammel-Event je verarbeitetem `maintenance:*`-Event, kein `entity_updated` pro Entität;
UIs laden daraufhin neu).

## Wartungsfenster am CI

Ab 0.3.0 ist die Registry **Konsument** des Wartungsfenster-Vertrags (Core-API 1.13,
`lyndrix-core/docs/plugins.md` → *Maintenance windows*). Sie besitzt keine Fenster: Der
IaC-Orchestrator publiziert, was Controller und `service.yml` definieren, das Monitoring seine
manuellen Fenster; jeder laufende Deploy ist ein Override. Die Registry speichert das je Quelle in
`registry_maintenance` und wertet **beim Lesen** aus — ohne Timer, ohne Pro-Entity-Events.

**Auswertung.** `GET /entities/{uuid}` liefert

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

- `active`/`until`/`source`/`next_start` kommen aus `core.api.maintenance.effective_state` über
  die Fenster **aller** Quellen plus die gespeicherten Overrides. Ein aktives Override gewinnt
  (`source: "override"`), `until` ist das spätere der beiden Enden. `overrides` listet nur die
  gerade aktiven (bis `until`, nach `override_ended` noch `linger_minutes`).
- `incidents` und `deploy_policy` auf oberster Ebene stammen von der ersten Quelle (alphabetisch),
  die sie liefert — heute nur der Orchestrator.
- **Ohne Core-Bibliothek** (Core < 1.11, `min_core_version` bleibt 1.4.0) werden Fenster roh
  gespeichert und geliefert; `active` ist dann `null`, sobald Fenster existieren („nicht
  auswertbar“). Overrides brauchen keine Bibliothek und werden trotzdem ausgewertet.
- Abgelaufene Overrides werden beim Lesen ignoriert und einen Tag nach ihrem wirksamen Ende
  gelöscht (beendete Zeilen nach zwei Tagen, weil `ended_at + linger` nicht portabel in SQL
  steht); der Sweep läuft beim nächsten Override-Event mit.

**Idempotenz und Cluster.** `maintenance:*` wird vom Emitter lokal gesendet; die Registry läuft
auf jeder Instanz, der lokale Handler schreibt die gemeinsame DB. Trotzdem sind alle Handler
idempotent (Unique-Key, `delete`+`insert` je `source` in einer Transaktion, Upsert je
`override_id`, Retry bei 1020/1205/1213), sodass eine spätere `cluster=True`-Zustellung keinen
Schaden anrichtet. `replaces` wirkt nur innerhalb derselben `source` — kein Emitter kann die
Fenster eines anderen löschen.

**Host eines Services.** Der Orchestrator identifiziert Services als `entity_type: service` mit
`iac.service = "<host>/<service.name>"` und setzt `runs_on` Service → Host.
`GET /entities/{uuid}/host` folgt dieser Kante (404, wenn sie fehlt); Konsumenten, die den Host
auch ohne Registry brauchen, nutzen das `host_ci_id` aus den Events.

## Cluster-Betrieb (ab 0.2.0)

Läuft der Core mit mehreren Instanzen (`LYNDRIX_CLUSTER_MODE=cluster`), arbeitet die Registry auf
jeder Instanz; der gesamte Zustand liegt in der gemeinsamen MariaDB.

- **Bootstrap:** Tabellen und Migrationen laufen unter `db_instance.advisory_lock("lyndrix_bootstrap")`
  im Thread-Pool. Ein verlorenes Rennen („Tabelle/Spalte/Index existiert bereits“, 1050/1060/1061)
  gilt als Erfolg; schlägt der Bootstrap fehl, wird er bis zu fünfmal wiederholt.
- **Schreibzugriffe** laufen mit READ COMMITTED und werden bei MariaDB-Fehler 1020/1205/1213
  (Snapshot-Isolation, Lock-Timeout, Deadlock) kurz wiederholt. `last_seen_at` wird bei sonst
  unverändertem Objekt höchstens alle 30 s geschrieben.
- **Wettläufe um eindeutige Schlüssel:** Legt eine andere Instanz denselben Identifier gleichzeitig
  an, liest `identify()` neu und übernimmt die Beobachtung in die Gewinner-Entität. Offene Konflikte
  tragen den eindeutigen Schlüssel `open_fingerprint` (NULL nach Auflösung) und werden so nur einmal
  abgelegt.
- **Katalog:** `POST /catalog/reload` lädt lokal neu und sendet `entity_registry:catalog_reloaded`
  mit `cluster=True`; die anderen Instanzen laden daraufhin ebenfalls neu.
- **`health()`** prüft nur `SELECT 1` und zählt offene Konflikte.

## Rollen

| Rolle | Rechte | Auto-Mapping |
|---|---|---|
| `admin` | `api:read`, `api:write` | Gruppe `INT_ADMIN` |
| `viewer` | `api:read` | — |

## React-UI

Route `/registry` (im Shell unter `/apps/lyndrix-plugin-entity_registry/registry`). Deep-Link auf
eine Entität: `?uuid=<uuid>`. Ansichten: Liste mit Typ-/Status-Filter und Suche, Detail
(Identifier, Attribute, Beziehungen, „gesehen von“, Merge, Stilllegen), Konfliktwarteschlange mit
„Diese behalten, andere zusammenführen“ bzw. „Verwerfen“.

Ein CI in Wartung trägt in Liste und Detail-Kopfzeile das Badge **Wartung** (Klasse
`lx-badge--maintenance`, Farbtoken `--lx-state-maintenance`; ältere Shells fallen auf die
Paused-Farbe zurück). Der Listenfilter „Nur in Wartung“ nutzt `?in_maintenance=true`. Die
Detailkarte **Wartung** zeigt aktuelles Fenster bzw. Override (mit Ende und Grund), das nächste
Fenster, die aktiven Overrides, die veröffentlichten Fenster je Quelle samt `inherited_from`,
die Incident-Regel und die Deploy-Policy — oder den Hinweis, dass der Core Fenster nicht
auswerten kann.

Ist das ITSM-Plugin geladen, zeigt die Detailansicht zusätzlich die Karte **Service Management**:
die offenen Datensätze, die dieses CI referenzieren (Link in den Datensatz), „Neuer Incident“
(öffnet das ITSM-Formular mit dem CI als *betroffen* vorbelegt — `/apps/lyndrix-plugin-itsm/itsm?new=incident&ci=<uuid>`)
und „Alle Datensätze“ (`?ci=<uuid>`). Ohne ITSM-Plugin oder -Berechtigung bleibt die Karte
einfach weg.

## Entwicklung & Tests

```bash
# aus dem Plugin-Verzeichnis
npm install && npm run build          # React-Bundle → app/ui/static/ui_bundle.js
ruff check entrypoint.py app tests

# Tests brauchen den Core auf dem PYTHONPATH — am einfachsten im Dev-Container:
docker cp . lyndrix-core-dev:/tmp/er
docker exec -w /tmp/er -e PYTHONPATH=/app:/tmp/pytest-sm:/tmp/er \
  -e LYNDRIX_CLUSTER_MODE=single -e LYNDRIX_COORDINATOR=local \
  lyndrix-core-dev python -m pytest -q tests
```

`tests/conftest.py` importiert `core.services` vor `core.api` (umgeht einen zirkulären Import im
Core) und stellt die Fixtures `svc` (Service gegen SQLite im Speicher, Fremdschlüssel aktiv),
`entrypoint` (lädt `entrypoint.py` wie der Core als Paketmitglied) und `bus` (führt `setup()`
gegen einen aufzeichnenden Kontext aus → Bus-Handler je Topic). `tests/test_maintenance.py`
deckt das Wartungs-Read-Model samt Handlern und dem Pfad ohne Core-Bibliothek
(`monkeypatch` auf `maintenance.mw = None`) ab. `(scheme, value) IN (...)` wurde zusätzlich
gegen MariaDB geprüft.

## Grenzen (Phase 1)

- Kein hartes Löschen über die API — nur `retire` und `merge` (Audit-Spur bleibt).
- Konflikte werden nur gemeldet, nie automatisch aufgelöst.
- Beziehungen sind einfache Kanten ohne Attribute; Graph-Darstellung ist eine Liste.
