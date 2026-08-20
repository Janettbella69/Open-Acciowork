import { useMemo, useState } from 'react'
import { errorMessage, setEvidenceStatus } from '../api/client'
import type { Evidence, EvidenceStatus, OpportunityProject } from '../api/types'

const kindLabels: Record<Evidence['kind'], string> = {
  observed_fact: '观测事实',
  user_statement: '用户陈述',
  community_signal: '社区信号',
  inference: '推断',
}

const statusLabels: Record<EvidenceStatus, string> = {
  active: '有效',
  stale: '已过期',
  removed: '已移除',
}

type EvidenceDrawerProps = {
  project: OpportunityProject
  onChanged: () => void
}

export default function EvidenceDrawer({ project, onChanged }: EvidenceDrawerProps) {
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
      setNotice(
        status === 'active'
          ? `证据已恢复${affected ? `；${affected} 条结论已联动更新` : ''}`
          : `证据已标记为${statusLabels[status]}${affected ? `；${affected} 条结论转入待复核` : ''}`,
      )
      onChanged()
    } catch (statusError) {
      setError(errorMessage(statusError))
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="evidence-drawer" aria-label="Evidence drawer">
      <div className="evidence-drawer-controls">
        <label>
          <span className="conversation-visually-hidden">Search evidence</span>
          <input
            type="search"
            placeholder="搜索证据…"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <label>
          <span className="conversation-visually-hidden">Filter by kind</span>
          <select
            aria-label="Filter by kind"
            value={kindFilter}
            onChange={(event) => setKindFilter(event.target.value as typeof kindFilter)}
          >
            <option value="all">全部类型</option>
            {Object.entries(kindLabels).map(([value, text]) => (
              <option key={value} value={value}>{text}</option>
            ))}
          </select>
        </label>
        <label>
          <span className="conversation-visually-hidden">Filter by status</span>
          <select
            aria-label="Filter by status"
            value={statusFilter}
            onChange={(event) => setStatusFilter(event.target.value as typeof statusFilter)}
          >
            <option value="all">全部状态</option>
            {Object.entries(statusLabels).map(([value, text]) => (
              <option key={value} value={value}>{text}</option>
            ))}
          </select>
        </label>
      </div>

      {notice && <p className="evidence-drawer-notice" role="status">{notice}</p>}
      {error && <p className="conversation-error" role="alert">{error}</p>}

      {visible.length ? visible.map((item) => (
        <article key={item.id} className="evidence-card" data-status={item.status}>
          <header>
            <span data-kind={item.kind}>{kindLabels[item.kind]}</span>
            {item.dimension && <span data-kind="dimension">{item.dimension}</span>}
            <span data-status={item.status}>{statusLabels[item.status]}</span>
            <b>置信度 {(item.confidence * 100).toFixed(0)}%</b>
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
              抓取 {new Date(item.retrieved_at).toLocaleString()}
              {item.observed_at && ` · 观察 ${new Date(item.observed_at).toLocaleDateString()}`}
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
                >标记过期</button>
                <button
                  type="button"
                  data-tone="danger"
                  disabled={busy === item.id}
                  onClick={() => void changeStatus(item, 'removed')}
                >移除</button>
              </>
            ) : (
              <button
                type="button"
                disabled={busy === item.id}
                onClick={() => void changeStatus(item, 'active')}
              >恢复</button>
            )}
          </div>
        </article>
      )) : (
        <p className="evidence-drawer-empty">
          {project.evidence.length
            ? '没有符合筛选条件的证据。'
            : '还没有证据记录。Agent 使用账本工具后会出现在这里。'}
        </p>
      )}
    </div>
  )
}
