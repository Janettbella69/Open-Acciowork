import { useMemo, useState } from 'react'
import { errorMessage, setEvidenceStatus } from '../api/client'
import type { Evidence, EvidenceStatus, OpportunityProject } from '../api/types'
import { useLanguage, useT } from '../i18n'
import FeedbackForm from './FeedbackForm'

const kindLabels: Record<Evidence['kind'], string> = {
  observed_fact: 'Observed fact',
  user_statement: 'User statement',
  community_signal: 'Community signal',
  inference: 'Inference',
}

const statusLabels: Record<EvidenceStatus, string> = {
  active: 'Active',
  stale: 'Stale',
  removed: 'Removed',
}

type EvidenceDrawerProps = {
  project: OpportunityProject
  runId?: string
  onChanged: () => void
}

export default function EvidenceDrawer({ project, runId, onChanged }: EvidenceDrawerProps) {
  const t = useT()
  const language = useLanguage()
  const [kindFilter, setKindFilter] = useState<'all' | Evidence['kind']>('all')
  const [statusFilter, setStatusFilter] = useState<'all' | EvidenceStatus>('all')
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState('')
  const [error, setError] = useState('')

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase()
    return [...project.evidence].reverse().filter((item) => {
      if (kindFilter !== 'all' && item.kind !== kindFilter) return false
      if (statusFilter !== 'all' && item.status !== statusFilter) return false
      if (needle && !`${item.claim} ${item.source_title} ${item.tags.join(' ')}`
        .toLowerCase().includes(needle)) return false
      return true
    })
  }, [project.evidence, kindFilter, statusFilter, query])

  const changeStatus = async (item: Evidence, status: EvidenceStatus) => {
    setBusy(item.id)
    setError('')
    setNotice('')
    try {
      const change = await setEvidenceStatus(project.id, item.id, status)
      const affected = change.affected_decision_ids.length
      const statusText = language === 'zh-CN'
        ? t(statusLabels[status])
        : statusLabels[status].toLowerCase()
      setNotice(
        status === 'active'
          ? affected
            ? t('Evidence restored; {n} linked decisions were updated', { n: affected })
            : t('Evidence restored')
          : affected
            ? t('Evidence marked {status}; {n} linked decisions need review', { status: statusText, n: affected })
            : t('Evidence marked {status}', { status: statusText }),
      )
      onChanged()
    } catch (statusError) {
      setError(errorMessage(statusError))
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="evidence-drawer" aria-label={t('Evidence drawer')}>
      <div className="evidence-drawer-controls">
        <label>
          <span className="conversation-visually-hidden">{t('Search evidence')}</span>
          <input
            type="search"
            placeholder={t('Search evidence…')}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <label>
          <span className="conversation-visually-hidden">{t('Filter by kind')}</span>
          <select
            aria-label={t('Filter by kind')}
            value={kindFilter}
            onChange={(event) => setKindFilter(event.target.value as typeof kindFilter)}
          >
            <option value="all">{t('All kinds')}</option>
            {Object.entries(kindLabels).map(([value, text]) => (
              <option key={value} value={value}>{t(text)}</option>
            ))}
          </select>
        </label>
        <label>
          <span className="conversation-visually-hidden">{t('Filter by status')}</span>
          <select
            aria-label={t('Filter by status')}
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value as typeof statusFilter)}
          >
            <option value="all">{t('All statuses')}</option>
            {Object.entries(statusLabels).map(([value, text]) => (
              <option key={value} value={value}>{t(text)}</option>
            ))}
          </select>
        </label>
      </div>

      {notice && <p className="evidence-drawer-notice" role="status">{notice}</p>}
      {error && <p className="conversation-error" role="alert">{t(error)}</p>}

      {visible.length ? visible.map((item) => (
        <article key={item.id} className="evidence-card" data-status={item.status}>
          <header>
            <span data-kind={item.kind}>{t(kindLabels[item.kind])}</span>
            {item.dimension && <span data-kind="dimension">{t(item.dimension[0].toUpperCase() + item.dimension.slice(1))}</span>}
            <span data-status={item.status}>{t(statusLabels[item.status])}</span>
            <b>{t('Confidence {n}%', { n: (item.confidence * 100).toFixed(0) })}</b>
          </header>
          <p>{item.claim}</p>
          <footer>
            <span>
              {item.source_url ? (
                <a href={item.source_url} target="_blank" rel="noreferrer">
                  {item.source_title}
                </a>
              ) : item.source_path ? (
                <code>{item.source_path}</code>
              ) : (
                item.source_title
              )}
            </span>
            <small>
              {t('Retrieved {time}', {
                time: new Date(item.retrieved_at).toLocaleString(language === 'zh-CN' ? 'zh-CN' : 'en'),
              })}
              {item.observed_at && ` · ${t('Observed {date}', {
                date: new Date(item.observed_at).toLocaleDateString(language === 'zh-CN' ? 'zh-CN' : 'en'),
              })}`}
            </small>
            {item.freshness_note && <em>{item.freshness_note}</em>}
          </footer>
          {item.excerpt && <blockquote>{item.excerpt}</blockquote>}
          <div className="evidence-card-actions">
            {item.status === 'active' ? (
              <>
                <button
                  type="button"
                  disabled={busy === item.id}
                  onClick={() => void changeStatus(item, 'stale')}
                >{t('Mark stale')}</button>
                <button
                  type="button"
                  data-tone="danger"
                  disabled={busy === item.id}
                  onClick={() => void changeStatus(item, 'removed')}
                >{t('Remove')}</button>
              </>
            ) : (
              <button
                type="button"
                disabled={busy === item.id}
                onClick={() => void changeStatus(item, 'active')}
              >{t('Restore')}</button>
            )}
          </div>
          <FeedbackForm
            key={`${project.id}:${item.id}:${item.source_run_id ?? runId ?? ''}`}
            target={{ project_id: project.id, evidence_id: item.id, run_id: item.source_run_id ?? runId, kind: 'evidence_issue' }}
            label="Report evidence issue"
          />
        </article>
      )) : (
        <p className="evidence-drawer-empty">
          {project.evidence.length
            ? t('No evidence matches these filters.')
            : t('No evidence has been recorded yet. Evidence appears here after the Agent uses the ledger tools.')}
        </p>
      )}
    </div>
  )
}
