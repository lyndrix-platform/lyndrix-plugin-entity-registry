import React, { useCallback, useEffect, useMemo, useState } from 'react'
// Provided by the host shell (window.__lyndrix_react_i18next); strings come
// from locales/entity_registry.<locale>.json (namespace "entity_registry").
import { useTranslation } from 'react-i18next'
import { itsmApi, registryApi, spaRedirect } from './lib/api'
import type { Catalog, Conflict, Entity, ItsmRecordSummary, Stats } from './lib/api'

// ─── Small shared pieces ──────────────────────────────────────────────────────

function Button({
  label, onClick, disabled, variant = 'default', icon, title,
}: {
  label: string
  onClick?: () => void
  disabled?: boolean
  variant?: 'default' | 'danger' | 'primary' | 'ghost'
  icon?: string
  title?: string
}) {
  const cls =
    variant === 'primary' ? 'lx-btn lx-btn--primary lx-btn--sm'
    : variant === 'danger' ? 'lx-btn lx-btn--danger lx-btn--sm'
    : variant === 'ghost' ? 'lx-btn lx-btn--ghost lx-btn--sm'
    : 'lx-btn lx-btn--secondary lx-btn--sm'
  return (
    <button type="button" onClick={onClick} disabled={disabled} className={cls} title={title}>
      {icon && <span className="material-icons" style={{ fontSize: 16 }}>{icon}</span>}
      {label}
    </button>
  )
}

function ErrorBox({ msg }: { msg: string }) {
  return (
    <div className="lx-card lx-card-accent lx-card-accent--danger" style={{ padding: '0.75rem 1rem', marginBottom: '1rem' }}>
      <span style={{ color: 'var(--lx-state-down)', fontSize: '0.85rem' }}>{msg}</span>
    </div>
  )
}

function TypeBadge({ type, catalog }: { type: string; catalog: Catalog | null }) {
  const def = catalog?.entity_types.find((t) => t.id === type)
  return (
    <span className="lx-badge lx-badge--accent" title={def?.description}>
      <span className="material-icons" style={{ fontSize: 13 }}>{def?.icon ?? 'category'}</span>
      {def?.label ?? type}
    </span>
  )
}

function StatusBadge({ status }: { status: Entity['status'] }) {
  const { t } = useTranslation('entity_registry')
  const mod = status === 'active' ? 'lx-badge--up' : status === 'retired' ? 'lx-badge--paused' : 'lx-badge--muted'
  return (
    <span className={`lx-badge ${mod}`}>
      <span className="lx-dot" />
      {t(`status.${status}`, { defaultValue: status })}
    </span>
  )
}

function IdentChip({ scheme, value }: { scheme: string; value: string }) {
  return (
    <span className="lx-badge lx-badge--muted lx-mono" style={{ fontSize: '0.68rem' }} title={`${scheme} = ${value}`}>
      <span style={{ opacity: 0.6 }}>{scheme}</span>&nbsp;{value}
    </span>
  )
}

function Mono({ children, copy }: { children: string; copy?: boolean }) {
  const [done, setDone] = useState(false)
  return (
    <span className="lx-mono" style={{ fontSize: '0.75rem', display: 'inline-flex', alignItems: 'center', gap: '0.3rem' }}>
      {children}
      {copy && (
        <button
          className="lx-icon-btn"
          title="copy"
          onClick={() => {
            void navigator.clipboard?.writeText(children)
            setDone(true)
            window.setTimeout(() => setDone(false), 1200)
          }}
        >
          <span className="material-icons" style={{ fontSize: 14 }}>{done ? 'check' : 'content_copy'}</span>
        </button>
      )}
    </span>
  )
}

function fmt(ts: string | null): string {
  if (!ts) return '—'
  const d = new Date(ts)
  return isNaN(d.getTime()) ? ts : d.toLocaleString()
}

function useCatalog() {
  const [catalog, setCatalog] = useState<Catalog | null>(null)
  useEffect(() => {
    registryApi.getCatalog().then(setCatalog).catch(() => setCatalog(null))
  }, [])
  return catalog
}

// ─── Views ────────────────────────────────────────────────────────────────────

type View = { kind: 'list' } | { kind: 'detail'; uuid: string } | { kind: 'conflicts' }

function StatsStrip({ stats, catalog, onConflicts }: { stats: Stats | null; catalog: Catalog | null; onConflicts: () => void }) {
  const { t } = useTranslation('entity_registry')
  if (!stats) return null
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.5rem', alignItems: 'center', margin: '0.75rem 0 1rem' }}>
      <span className="lx-badge lx-badge--accent">{t('stats.total', { defaultValue: '{{count}} entities', count: stats.total })}</span>
      {Object.entries(stats.by_type).map(([type, n]) => (
        <span key={type} className="lx-badge lx-badge--muted">
          {catalog?.entity_types.find((x) => x.id === type)?.label ?? type}: {n}
        </span>
      ))}
      <span className="lx-badge lx-badge--muted">{t('stats.identifiers', { defaultValue: '{{count}} identifiers', count: stats.identifiers })}</span>
      <span className="lx-badge lx-badge--muted">{t('stats.relations', { defaultValue: '{{count}} relations', count: stats.relations })}</span>
      {stats.open_conflicts > 0 && (
        <button className="lx-badge lx-badge--down" style={{ cursor: 'pointer', border: 'none' }} onClick={onConflicts}>
          <span className="material-icons" style={{ fontSize: 13 }}>warning</span>
          {t('stats.conflicts', { defaultValue: '{{count}} open conflicts', count: stats.open_conflicts })}
        </button>
      )}
    </div>
  )
}

function EntityList({ catalog, onOpen }: { catalog: Catalog | null; onOpen: (uuid: string) => void }) {
  const { t } = useTranslation('entity_registry')
  const [entities, setEntities] = useState<Entity[]>([])
  const [total, setTotal] = useState(0)
  const [q, setQ] = useState('')
  const [type, setType] = useState('')
  const [status, setStatus] = useState('active')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const res = await registryApi.listEntities({ q: q.trim() || undefined, type: type || undefined, status, limit: 500 })
      setEntities(res.entities)
      setTotal(res.total)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }, [q, type, status])

  useEffect(() => {
    const h = window.setTimeout(() => void load(), q ? 250 : 0)
    return () => window.clearTimeout(h)
  }, [load, q])

  return (
    <>
      <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', marginBottom: '0.75rem' }}>
        <input
          className="lx-input"
          style={{ flex: '1 1 220px' }}
          placeholder={t('list.searchPlaceholder', { defaultValue: 'Search name or identifier…' })}
          value={q}
          onChange={(e) => setQ(e.target.value)}
        />
        <select className="lx-select" value={type} onChange={(e) => setType(e.target.value)}>
          <option value="">{t('list.allTypes', { defaultValue: 'All types' })}</option>
          {catalog?.entity_types.map((x) => <option key={x.id} value={x.id}>{x.label}</option>)}
        </select>
        <select className="lx-select" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="active">{t('status.active', { defaultValue: 'active' })}</option>
          <option value="retired">{t('status.retired', { defaultValue: 'retired' })}</option>
          <option value="all">{t('list.allStatuses', { defaultValue: 'all' })}</option>
        </select>
        <Button label={t('common.refresh', { defaultValue: 'Refresh' })} icon="refresh" onClick={() => void load()} />
      </div>
      {error && <ErrorBox msg={error} />}
      {loading ? (
        <div className="lx-empty"><span className="lx-spinner" /></div>
      ) : entities.length === 0 ? (
        <div className="lx-empty">{t('list.empty', { defaultValue: 'No entities yet. Other plugins register them via identify().' })}</div>
      ) : (
        <div className="lx-card" style={{ padding: 0, overflow: 'hidden' }}>
          <table className="lx-table">
            <thead>
              <tr>
                <th>{t('list.colName', { defaultValue: 'Name' })}</th>
                <th>{t('list.colType', { defaultValue: 'Type' })}</th>
                <th>{t('list.colIdentifiers', { defaultValue: 'Identifiers' })}</th>
                <th>{t('list.colSources', { defaultValue: 'Seen by' })}</th>
                <th>{t('list.colLastSeen', { defaultValue: 'Last seen' })}</th>
              </tr>
            </thead>
            <tbody>
              {entities.map((e) => (
                <tr key={e.uuid} className="lx-row-hover" style={{ cursor: 'pointer' }} onClick={() => onOpen(e.uuid)}>
                  <td>
                    <div style={{ fontWeight: 600, color: 'var(--lx-text)' }}>{e.display_name}</div>
                    <Mono>{e.uuid}</Mono>
                  </td>
                  <td><TypeBadge type={e.entity_type} catalog={catalog} /> {e.status !== 'active' && <StatusBadge status={e.status} />}</td>
                  <td style={{ display: 'flex', flexWrap: 'wrap', gap: '0.25rem', maxWidth: 420 }}>
                    {e.identifiers.slice(0, 4).map((i) => <IdentChip key={`${i.scheme}:${i.value}`} scheme={i.scheme} value={i.value} />)}
                    {e.identifiers.length > 4 && <span className="lx-badge lx-badge--muted">+{e.identifiers.length - 4}</span>}
                  </td>
                  <td style={{ fontSize: '0.75rem', color: 'var(--lx-text-muted)' }}>{e.sources.join(', ') || '—'}</td>
                  <td style={{ fontSize: '0.75rem', color: 'var(--lx-text-muted)' }}>{fmt(e.last_seen_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ padding: '0.5rem 0.75rem', fontSize: '0.72rem', color: 'var(--lx-text-muted)' }}>
            {t('list.shown', { defaultValue: '{{shown}} of {{total}}', shown: entities.length, total })}
          </div>
        </div>
      )}
    </>
  )
}

function EntityDetail({ uuid, catalog, onBack, onOpen, onChanged }: {
  uuid: string
  catalog: Catalog | null
  onBack: () => void
  onOpen: (uuid: string) => void
  onChanged: () => void
}) {
  const { t } = useTranslation('entity_registry')
  const [entity, setEntity] = useState<Entity | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [mergeDrop, setMergeDrop] = useState('')
  const [relTo, setRelTo] = useState('')
  const [relKind, setRelKind] = useState('')
  const [confirmRetire, setConfirmRetire] = useState(false)
  // ITSM records referencing this CI: undefined = loading, null = ITSM not available.
  const [itsm, setItsm] = useState<ItsmRecordSummary[] | null | undefined>(undefined)

  const load = useCallback(async () => {
    setError(null)
    try {
      const res = await registryApi.getEntity(uuid)
      setEntity(res.entity)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [uuid])

  useEffect(() => { void load() }, [load])
  useEffect(() => { setItsm(undefined); void itsmApi.recordsForCi(uuid).then(setItsm) }, [uuid])

  async function run(fn: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await fn()
      await load()
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  const relationLabel = (id: string) => catalog?.relations.find((r) => r.id === id)?.label ?? id
  const attrs = useMemo(() => Object.entries(entity?.attributes ?? {}), [entity])

  if (!entity && !error) return <div className="lx-empty"><span className="lx-spinner" /></div>

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '0.75rem' }}>
        <Button label={t('common.back', { defaultValue: 'Back' })} icon="arrow_back" variant="ghost" onClick={onBack} />
        {entity && (
          <>
            <h2 style={{ margin: 0, fontSize: '1.05rem', fontWeight: 700, color: 'var(--lx-text)' }}>{entity.display_name}</h2>
            <TypeBadge type={entity.entity_type} catalog={catalog} />
            <StatusBadge status={entity.status} />
          </>
        )}
      </div>
      {error && <ErrorBox msg={error} />}
      {entity && (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: '1rem' }}>
          <div className="lx-card" style={{ padding: '1rem' }}>
            <div className="lx-section-title">{t('detail.identity', { defaultValue: 'Identity' })}</div>
            <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '0.35rem 0.75rem', fontSize: '0.8rem' }}>
              <span className="lx-label">UUID</span><Mono copy>{entity.uuid}</Mono>
              <span className="lx-label">{t('detail.owner', { defaultValue: 'Owner' })}</span><span>{entity.owner_source ?? '—'}</span>
              <span className="lx-label">{t('list.colSources', { defaultValue: 'Seen by' })}</span><span>{entity.sources.join(', ') || '—'}</span>
              <span className="lx-label">{t('detail.created', { defaultValue: 'Created' })}</span><span>{fmt(entity.created_at)}</span>
              <span className="lx-label">{t('list.colLastSeen', { defaultValue: 'Last seen' })}</span><span>{fmt(entity.last_seen_at)}</span>
              {entity.merged_into && (<><span className="lx-label">{t('detail.mergedInto', { defaultValue: 'Merged into' })}</span><a onClick={() => onOpen(entity.merged_into!)} style={{ cursor: 'pointer' }}><Mono>{entity.merged_into}</Mono></a></>)}
            </div>
            <div className="lx-section-title" style={{ marginTop: '1rem' }}>{t('detail.identifiers', { defaultValue: 'Identifiers' })}</div>
            <table className="lx-table">
              <thead><tr><th>{t('detail.scheme', { defaultValue: 'Scheme' })}</th><th>{t('detail.value', { defaultValue: 'Value' })}</th><th>{t('detail.source', { defaultValue: 'Source' })}</th></tr></thead>
              <tbody>
                {entity.identifiers.map((i) => (
                  <tr key={`${i.scheme}:${i.value}`}>
                    <td className="lx-mono" style={{ fontSize: '0.75rem' }}>{i.scheme}</td>
                    <td className="lx-mono" style={{ fontSize: '0.75rem' }}>{i.value}</td>
                    <td style={{ fontSize: '0.75rem', color: 'var(--lx-text-muted)' }}>{i.source ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="lx-card" style={{ padding: '1rem' }}>
            <div className="lx-section-title">{t('detail.attributes', { defaultValue: 'Attributes' })}</div>
            {attrs.length === 0 ? (
              <div style={{ fontSize: '0.8rem', color: 'var(--lx-text-muted)' }}>—</div>
            ) : (
              <table className="lx-table">
                <tbody>
                  {attrs.map(([k, v]) => (
                    <tr key={k}>
                      <td className="lx-mono" style={{ fontSize: '0.75rem', whiteSpace: 'nowrap' }}>{k}</td>
                      <td className="lx-mono" style={{ fontSize: '0.75rem', wordBreak: 'break-all' }}>{typeof v === 'string' ? v : JSON.stringify(v)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <div className="lx-section-title" style={{ marginTop: '1rem' }}>{t('detail.relations', { defaultValue: 'Relations' })}</div>
            {(entity.relations ?? []).length === 0 && <div style={{ fontSize: '0.8rem', color: 'var(--lx-text-muted)' }}>—</div>}
            {(entity.relations ?? []).map((r) => (
              <div key={r.id} style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '0.8rem', padding: '0.25rem 0' }}>
                <span className="material-icons" style={{ fontSize: 16, color: 'var(--lx-text-muted)' }}>{r.direction === 'out' ? 'arrow_forward' : 'arrow_back'}</span>
                <span style={{ color: 'var(--lx-text-muted)' }}>{relationLabel(r.relation)}</span>
                <a onClick={() => onOpen(r.other_uuid)} style={{ cursor: 'pointer', fontWeight: 600, color: 'var(--lx-accent)' }}>
                  {r.other_display_name ?? r.other_uuid}
                </a>
                {r.other_entity_type && <TypeBadge type={r.other_entity_type} catalog={catalog} />}
                <button
                  className="lx-icon-btn lx-icon-btn--danger"
                  title={t('common.remove', { defaultValue: 'Remove' })}
                  disabled={busy}
                  onClick={() => void run(() => r.direction === 'out'
                    ? registryApi.removeRelation(entity.uuid, r.relation, r.other_uuid)
                    : registryApi.removeRelation(r.other_uuid, r.relation, entity.uuid))}
                >
                  <span className="material-icons" style={{ fontSize: 16 }}>link_off</span>
                </button>
              </div>
            ))}
            <div style={{ display: 'flex', gap: '0.4rem', marginTop: '0.5rem', flexWrap: 'wrap' }}>
              <select className="lx-select" value={relKind} onChange={(e) => setRelKind(e.target.value)}>
                <option value="">{t('detail.pickRelation', { defaultValue: 'relation…' })}</option>
                {catalog?.relations.map((r) => <option key={r.id} value={r.id}>{r.label}</option>)}
              </select>
              <input className="lx-input lx-mono" style={{ flex: '1 1 260px', fontSize: '0.75rem' }} placeholder={t('detail.targetUuid', { defaultValue: 'target entity UUID' })} value={relTo} onChange={(e) => setRelTo(e.target.value)} />
              <Button label={t('detail.addRelation', { defaultValue: 'Link' })} icon="link" disabled={busy || !relKind || relTo.trim().length !== 36}
                onClick={() => void run(async () => { await registryApi.addRelation(entity.uuid, relTo.trim(), relKind); setRelTo('') })} />
            </div>
          </div>

          {itsm !== null && (
            <div className="lx-card" style={{ padding: '1rem' }}>
              <div className="lx-section-title">{t('detail.itsm', { defaultValue: 'Service management' })}</div>
              <p style={{ fontSize: '0.78rem', color: 'var(--lx-text-muted)', margin: '0 0 0.5rem' }}>
                {t('detail.itsmHint', { defaultValue: 'Records in the ITSM plugin that reference this configuration item.' })}
              </p>
              {itsm === undefined ? (
                <span className="lx-spinner" />
              ) : itsm.length === 0 ? (
                <div style={{ fontSize: '0.8rem', color: 'var(--lx-text-muted)' }}>{t('detail.itsmNone', { defaultValue: 'No open records for this configuration item.' })}</div>
              ) : (
                itsm.map((r) => (
                  <div key={r.uuid} style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', fontSize: '0.8rem', padding: '0.2rem 0' }}>
                    <a href={itsmApi.recordLink(r.uuid)} onClick={(e) => { e.preventDefault(); spaRedirect(itsmApi.recordLink(r.uuid)) }} className="lx-mono" style={{ color: 'var(--lx-accent)', fontWeight: 600, whiteSpace: 'nowrap' }}>{r.number}</a>
                    <span style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.title}</span>
                    <span className="lx-badge lx-badge--muted">{r.state}</span>
                    {r.priority && <span className="lx-badge lx-badge--muted">{r.priority}</span>}
                  </div>
                ))
              )}
              <div style={{ display: 'flex', gap: '0.4rem', marginTop: '0.6rem', flexWrap: 'wrap' }}>
                <Button label={t('detail.itsmNewIncident', { defaultValue: 'New incident' })} icon="support_agent" variant="primary" disabled={entity.status !== 'active'}
                  onClick={() => spaRedirect(itsmApi.newIncidentLink(entity.uuid))} />
                <Button label={t('detail.itsmAll', { defaultValue: 'All records' })} icon="list" onClick={() => spaRedirect(itsmApi.allForCiLink(entity.uuid))} />
              </div>
            </div>
          )}

          <div className="lx-card" style={{ padding: '1rem' }}>
            <div className="lx-section-title">{t('detail.actions', { defaultValue: 'Actions' })}</div>
            <p style={{ fontSize: '0.78rem', color: 'var(--lx-text-muted)', margin: '0 0 0.5rem' }}>
              {t('detail.mergeHint', { defaultValue: 'Merge another entity into this one: its identifiers and relations move here and its UUID keeps resolving to this entity.' })}
            </p>
            <div style={{ display: 'flex', gap: '0.4rem', flexWrap: 'wrap' }}>
              <input className="lx-input lx-mono" style={{ flex: '1 1 260px', fontSize: '0.75rem' }} placeholder={t('detail.dropUuid', { defaultValue: 'UUID of the entity to merge into this one' })} value={mergeDrop} onChange={(e) => setMergeDrop(e.target.value)} />
              <Button label={t('detail.merge', { defaultValue: 'Merge into this' })} icon="call_merge" variant="primary" disabled={busy || mergeDrop.trim().length !== 36}
                onClick={() => void run(async () => { await registryApi.merge(entity.uuid, mergeDrop.trim()); setMergeDrop('') })} />
            </div>
            <div style={{ marginTop: '1rem', display: 'flex', gap: '0.4rem' }}>
              {entity.status === 'active' && !confirmRetire && (
                <Button label={t('detail.retire', { defaultValue: 'Retire' })} icon="archive" variant="danger" disabled={busy} onClick={() => setConfirmRetire(true)} />
              )}
              {confirmRetire && (
                <>
                  <Button label={t('detail.retireConfirm', { defaultValue: 'Confirm retire' })} variant="danger" disabled={busy}
                    onClick={() => { setConfirmRetire(false); void run(() => registryApi.retire(entity.uuid)) }} />
                  <Button label={t('common.cancel', { defaultValue: 'Cancel' })} onClick={() => setConfirmRetire(false)} />
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  )
}

function ConflictList({ catalog, onOpen, onBack, onChanged }: {
  catalog: Catalog | null
  onOpen: (uuid: string) => void
  onBack: () => void
  onChanged: () => void
}) {
  const { t } = useTranslation('entity_registry')
  const [conflicts, setConflicts] = useState<Conflict[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      setConflicts((await registryApi.listConflicts('open')).conflicts)
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }, [])
  useEffect(() => { void load() }, [load])

  async function keep(c: Conflict, keepUuid: string) {
    setBusy(true)
    setError(null)
    try {
      for (const cand of c.candidates) {
        if (cand.uuid !== keepUuid) await registryApi.merge(keepUuid, cand.uuid)
      }
      await load()
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  async function dismiss(c: Conflict) {
    setBusy(true)
    try {
      await registryApi.dismissConflict(c.id)
      await load()
      onChanged()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', marginBottom: '0.75rem' }}>
        <Button label={t('common.back', { defaultValue: 'Back' })} icon="arrow_back" variant="ghost" onClick={onBack} />
        <h2 style={{ margin: 0, fontSize: '1.05rem', fontWeight: 700, color: 'var(--lx-text)' }}>{t('conflicts.title', { defaultValue: 'Open conflicts' })}</h2>
      </div>
      <p style={{ fontSize: '0.8rem', color: 'var(--lx-text-muted)', marginTop: 0 }}>
        {t('conflicts.hint', { defaultValue: 'One observation matched several entities. The registry never merges automatically — pick the entity to keep, or dismiss if they really are different things.' })}
      </p>
      {error && <ErrorBox msg={error} />}
      {conflicts.length === 0 && <div className="lx-empty">{t('conflicts.none', { defaultValue: 'No open conflicts.' })}</div>}
      {conflicts.map((c) => (
        <div key={c.id} className="lx-card lx-card-accent lx-card-accent--warning" style={{ padding: '1rem', marginBottom: '0.75rem' }}>
          <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap', marginBottom: '0.5rem' }}>
            <TypeBadge type={c.entity_type} catalog={catalog} />
            <span style={{ fontSize: '0.75rem', color: 'var(--lx-text-muted)' }}>{t('conflicts.reportedBy', { defaultValue: 'reported by {{source}} · {{when}}', source: c.source ?? '?', when: fmt(c.created_at) })}</span>
            <span style={{ flex: 1 }} />
            <Button label={t('conflicts.dismiss', { defaultValue: 'Dismiss' })} icon="close" disabled={busy} onClick={() => void dismiss(c)} />
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.25rem', marginBottom: '0.6rem' }}>
            {Object.entries(c.identifiers).map(([s, v]) => <IdentChip key={s} scheme={s} value={v} />)}
          </div>
          <table className="lx-table">
            <tbody>
              {c.candidates.map((cand) => (
                <tr key={cand.uuid}>
                  <td>
                    <a onClick={() => onOpen(cand.uuid)} style={{ cursor: 'pointer', fontWeight: 600, color: 'var(--lx-accent)' }}>{cand.display_name ?? cand.uuid}</a>
                    <div><Mono>{cand.uuid}</Mono></div>
                  </td>
                  <td style={{ fontSize: '0.75rem', color: 'var(--lx-text-muted)' }}>{t('conflicts.matched', { defaultValue: 'matched via' })} {cand.matched.join(', ')}</td>
                  <td style={{ textAlign: 'right' }}>
                    <Button label={t('conflicts.keep', { defaultValue: 'Keep this, merge others' })} icon="call_merge" variant="primary" disabled={busy} onClick={() => void keep(c, cand.uuid)} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </>
  )
}

// ─── Root ─────────────────────────────────────────────────────────────────────

export default function PluginApp() {
  const { t } = useTranslation('entity_registry')
  const catalog = useCatalog()
  const [stats, setStats] = useState<Stats | null>(null)
  // Deep link: /registry?uuid=<id> opens the detail view directly.
  const initial = useMemo<View>(() => {
    const uuid = new URLSearchParams(window.location.search).get('uuid')
    return uuid && uuid.length === 36 ? { kind: 'detail', uuid } : { kind: 'list' }
  }, [])
  const [view, setView] = useState<View>(initial)

  const refreshStats = useCallback(() => {
    registryApi.getStats().then(setStats).catch(() => setStats(null))
  }, [])
  useEffect(() => { refreshStats() }, [refreshStats, view])

  const open = (uuid: string) => setView({ kind: 'detail', uuid })

  return (
    <div style={{ maxWidth: 1100, margin: '0 auto', padding: '1.5rem 1.5rem 3rem' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '1rem' }}>
        <div>
          <h1 style={{ margin: 0, fontSize: '1.25rem', fontWeight: 700, letterSpacing: '-0.01em', color: 'var(--lx-text)' }}>
            {t('title', { defaultValue: 'Entity Registry' })}
          </h1>
          <p style={{ margin: '0.25rem 0 0', fontSize: '0.8rem', color: 'var(--lx-text-muted)' }}>
            {t('subtitle', { defaultValue: 'One stable UUID per configuration item, shared by every plugin.' })}
          </p>
        </div>
        {view.kind === 'list' && (
          <Button label={t('conflicts.title', { defaultValue: 'Open conflicts' })} icon="warning" onClick={() => setView({ kind: 'conflicts' })} />
        )}
      </div>
      <StatsStrip stats={stats} catalog={catalog} onConflicts={() => setView({ kind: 'conflicts' })} />
      {view.kind === 'list' && <EntityList catalog={catalog} onOpen={open} />}
      {view.kind === 'detail' && (
        <EntityDetail uuid={view.uuid} catalog={catalog} onBack={() => setView({ kind: 'list' })} onOpen={open} onChanged={refreshStats} />
      )}
      {view.kind === 'conflicts' && (
        <ConflictList catalog={catalog} onOpen={open} onBack={() => setView({ kind: 'list' })} onChanged={refreshStats} />
      )}
    </div>
  )
}
