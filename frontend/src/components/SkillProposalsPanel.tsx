import { useCallback, useEffect, useRef, useState } from 'react'
import { errorMessage, listSkillProposals } from '../api/client'
import type { SkillProposalDetail, SkillProposalState, SkillProposalSummary } from '../api/skillProposals'
import { proposalStateLabels } from '../api/skillProposals'
import { useT } from '../i18n'
import SkillProposalReview from './SkillProposalReview'
import './skill-proposals.css'

export default function SkillProposalsPanel({ onPublished }: { onPublished: () => Promise<void> }) {
  const t = useT()
  const [filter, setFilter] = useState<SkillProposalState>('pending')
  const filterRef = useRef(filter)
  const [items, setItems] = useState<SkillProposalSummary[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [nextCursor, setNextCursor] = useState<string | null>(null)
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set())
  const inFlight = useRef(new Set<string>())
  const active = useRef(false)
  const requestRevision = useRef(0)
  const controller = useRef<AbortController | null>(null)
  const cursorRef = useRef<string | null>(null)
  const loadingRef = useRef(false)
  const latest = useRef(new Map<string, SkillProposalSummary>())

  useEffect(() => {
    active.current = true
    return () => { active.current = false; requestRevision.current += 1; controller.current?.abort() }
  }, [])

  const remember = useCallback((incoming: SkillProposalSummary) => {
    const previous = latest.current.get(incoming.id)
    if (previous && previous.revision > incoming.revision) return previous
    latest.current.set(incoming.id, incoming)
    return incoming
  }, [])

  const load = useCallback(async (append = false) => {
    if (append && (loadingRef.current || !cursorRef.current)) return
    controller.current?.abort()
    const abort = new AbortController()
    controller.current = abort
    const revision = ++requestRevision.current
    const state = filterRef.current
    const cursor = append ? cursorRef.current : null
    loadingRef.current = true
    setLoading(true)
    setError('')
    if (!append) { setItems([]); setSelected(null); cursorRef.current = null; setNextCursor(null) }
    const current = () => active.current && revision === requestRevision.current && filterRef.current === state
    try {
      const page = await listSkillProposals(state, cursor, abort.signal)
      if (!current()) return
      cursorRef.current = page.next_cursor
      setNextCursor(page.next_cursor)
      const incoming = page.items.map(remember)
      setItems((previous) => {
        const merged = new Map((append ? previous : []).map((item) => [item.id, latest.current.get(item.id) ?? item]))
        incoming.forEach((item) => merged.set(item.id, item))
        return [...merged.values()].filter((item) => item.state === state)
      })
    } catch (cause) {
      if (current()) setError(errorMessage(cause))
    } finally {
      if (current()) { loadingRef.current = false; setLoading(false) }
    }
  }, [remember])

  useEffect(() => { filterRef.current = filter; void load() }, [filter, load])

  const changed = useCallback((detail: SkillProposalDetail) => {
    if (!active.current) return
    const newest = remember({ ...detail, scan_ok: detail.scan?.ok === true })
    setItems((previous) => previous.map((item) => item.id === newest.id ? newest : item)
      .filter((item) => item.state === filterRef.current))
  }, [remember])

  const beginReview = useCallback((id: string) => {
    if (inFlight.current.has(id)) return false
    inFlight.current.add(id)
    setBusyIds(new Set(inFlight.current))
    return true
  }, [])
  const endReview = useCallback((id: string) => {
    inFlight.current.delete(id)
    if (active.current) setBusyIds(new Set(inFlight.current))
  }, [])

  return <section className="skill-proposals" aria-labelledby="skill-proposals-heading">
    <header><div><h2 id="skill-proposals-heading">{t('Skill proposals')}</h2>
      <p>{t('Agent proposals stay outside the installed registry until a person reviews and publishes the frozen package.')}</p></div>
      <div className="skill-proposals-controls"><label>{t('Proposal state')}<select value={filter} onChange={(event) => {
        filterRef.current = event.target.value as SkillProposalState
        requestRevision.current += 1
        controller.current?.abort()
        setSelected(null)
        setFilter(filterRef.current)
      }}>{Object.entries(proposalStateLabels).map(([value, label]) => <option key={value} value={value}>{t(label)}</option>)}</select></label>
        <button type="button" disabled={loading} onClick={() => void load()}>{t('Refresh proposals')}</button></div>
    </header>
    {error && <p role="alert">{t(error)}</p>}
    <div className="skill-proposals-layout">
      <section className="skill-proposals-list" aria-label={t('Proposal list')} aria-busy={loading}>
        {items.map((item) => <button type="button" className="skill-proposal-row" key={item.id}
          aria-pressed={selected === item.id} onClick={() => setSelected(item.id)}>
          <strong>{item.manifest.name} <span>v{item.manifest.version}</span></strong>
          <span>{item.project_name}</span><small>{t(proposalStateLabels[item.state])} · {t(item.scan_ok ? 'Scan passed' : 'Scan blocked')}</small>
          <code>{item.content_hash.slice(0, 16)}</code>
          {busyIds.has(item.id) && <small>{t('Saving review…')}</small>}
        </button>)}
        {loading && <p role="status">{t('Loading proposals…')}</p>}
        {!loading && !error && !items.length && <p>{t('No proposals in this state.')}</p>}
        {nextCursor && <button type="button" disabled={loading} onClick={() => void load(true)}>{t('Load more proposals')}</button>}
      </section>
      {selected ? <SkillProposalReview key={selected} proposalId={selected} busy={busyIds.has(selected)}
        minimumRevision={latest.current.get(selected)?.revision ?? 0}
        beginReview={beginReview} endReview={endReview} onChanged={changed} onPublished={onPublished} />
        : <div className="skill-proposal-placeholder"><p>{t('Select a proposal to inspect its sources, scan and frozen files.')}</p></div>}
    </div>
  </section>
}
