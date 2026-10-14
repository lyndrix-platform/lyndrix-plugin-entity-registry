const PLUGIN_ID = 'lyndrix.plugin.entity_registry'
const TOKEN_KEY = 'lyndrix_token'
const REQUEST_TIMEOUT_MS = 15000

// ─── Types (mirror app/model/models.py to_dict + API payloads) ────────────────

export interface Identifier {
  scheme: string
  value: string
  source: string | null
  created_at: string | null
}

export interface RelationView {
  id: number
  from_uuid: string
  to_uuid: string
  relation: string
  source: string | null
  created_at: string | null
  direction: 'in' | 'out'
  other_uuid: string
  other_display_name: string | null
  other_entity_type: string | null
}

export interface Entity {
  uuid: string
  entity_type: string
  display_name: string
  status: 'active' | 'retired' | 'merged'
  owner_source: string | null
  sources: string[]
  attributes: Record<string, unknown>
  merged_into: string | null
  created_at: string | null
  updated_at: string | null
  last_seen_at: string | null
  identifiers: Identifier[]
  relations?: RelationView[]
}

export interface ConflictCandidate {
  uuid: string
  display_name: string | null
  matched: string[]
}

export interface Conflict {
  id: number
  entity_type: string
  candidates: ConflictCandidate[]
  identifiers: Record<string, string>
  source: string | null
  status: 'open' | 'resolved' | 'dismissed'
  resolution: string | null
  created_at: string | null
  resolved_at: string | null
}

export interface Catalog {
  entity_types: { id: string; label: string; icon: string; description: string }[]
  identifier_schemes: { id: string; label: string; description: string; priority: number }[]
  relations: { id: string; label: string; inverse: string | null }[]
}

export interface Stats {
  total: number
  by_type: Record<string, number>
  identifiers: number
  relations: number
  open_conflicts: number
}

// ─── Fetch wrapper ────────────────────────────────────────────────────────────

// In-SPA redirect the shell's router picks up — not a full reload (a hard
// reload cold-loads the SPA before plugin routes are registered).
function spaRedirect(path: string) {
  window.history.pushState({}, '', path)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = localStorage.getItem(TOKEN_KEY)
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(init.headers as Record<string, string> | undefined),
  }
  const controller = new AbortController()
  const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  let res: Response
  try {
    res = await fetch(path, { ...init, headers, signal: controller.signal })
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw new Error('Request timed out')
    throw err
  } finally {
    window.clearTimeout(timer)
  }
  if (res.status === 401) {
    localStorage.removeItem(TOKEN_KEY)
    spaRedirect('/login')
    throw new Error('Unauthorized')
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const body = (await res.json()) as { detail?: string }
      msg = body.detail ?? msg
    } catch { /* ignore */ }
    throw new Error(msg)
  }
  return res.json() as Promise<T>
}

// Path params are user-controlled strings (UUIDs typed into inputs); always
// encode them so a stray '/' or '..' cannot change the request path.
const enc = (v: string | number) => encodeURIComponent(String(v))
const base = (sub: string) => `/api/plugins/${PLUGIN_ID}/${sub}`

export const registryApi = {
  listEntities: (p: { type?: string; q?: string; status?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams()
    if (p.type) qs.set('type', p.type)
    if (p.q) qs.set('q', p.q)
    if (p.status) qs.set('status', p.status)
    if (p.limit) qs.set('limit', String(p.limit))
    if (p.offset) qs.set('offset', String(p.offset))
    return apiFetch<{ total: number; entities: Entity[] }>(base(`entities?${qs}`))
  },
  getEntity: (uuid: string) => apiFetch<{ entity: Entity }>(base(`entities/${enc(uuid)}`)),
  updateEntity: (uuid: string, body: { display_name?: string; attributes?: Record<string, unknown> }) =>
    apiFetch<{ entity: Entity }>(base(`entities/${enc(uuid)}`), { method: 'PATCH', body: JSON.stringify(body) }),
  retire: (uuid: string) => apiFetch<{ entity: Entity }>(base(`entities/${enc(uuid)}/retire`), { method: 'POST' }),
  merge: (keep: string, drop: string) =>
    apiFetch<{ entity: Entity }>(base(`entities/${enc(keep)}/merge`), {
      method: 'POST', body: JSON.stringify({ drop_uuid: drop }),
    }),
  addRelation: (uuid: string, to_uuid: string, relation: string) =>
    apiFetch<{ relation: RelationView }>(base(`entities/${enc(uuid)}/relations`), {
      method: 'POST', body: JSON.stringify({ to_uuid, relation }),
    }),
  removeRelation: (uuid: string, relation: string, to_uuid: string) =>
    apiFetch<{ ok: boolean }>(base(`entities/${enc(uuid)}/relations/${enc(relation)}/${enc(to_uuid)}`), { method: 'DELETE' }),
  lookup: (scheme: string, value: string) =>
    apiFetch<{ entity: Entity }>(base(`lookup?scheme=${encodeURIComponent(scheme)}&value=${encodeURIComponent(value)}`)),
  listConflicts: (status = 'open') => apiFetch<{ conflicts: Conflict[] }>(base(`conflicts?status=${status}`)),
  dismissConflict: (id: number, reason?: string) =>
    apiFetch<{ conflict: Conflict }>(base(`conflicts/${enc(id)}/dismiss`), {
      method: 'POST', body: JSON.stringify({ reason }),
    }),
  getCatalog: () => apiFetch<Catalog>(base('catalog')),
  getStats: () => apiFetch<Stats>(base('stats')),
}
