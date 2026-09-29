import { useCallback, useEffect, useRef, useState } from 'react'
import { errorMessage, listTasks } from '../api/client'
import type { TaskOverview } from '../api/taskOverview'
import { useLanguage, useT } from '../i18n'
import Brand from './Brand'
import WorkspaceNav from './WorkspaceNav'
import './workspace-composer.css'
import './task-overview-page.css'

export default function TaskOverviewPage() {
  const t = useT()
  const language = useLanguage()
  const [overview, setOverview] = useState<TaskOverview | null>(null)
  const [error, setError] = useState('')
  const latestRequest = useRef(0)
  const mounted = useRef(false)
  const inFlight = useRef<AbortController | null>(null)

  const refresh = useCallback(async () => {
    // Polls and repeated clicks share the current read. Starting a newer read
    // every five seconds would discard all responses on a slower connection.
    if (!mounted.current || inFlight.current) return
    const controller = new AbortController()
    inFlight.current = controller
    const { signal } = controller
    const requestId = ++latestRequest.current
    try {
      const latest = await listTasks(signal)
      if (signal.aborted || requestId !== latestRequest.current) return
      setOverview(latest)
      setError('')
    } catch (cause) {
      if (!signal.aborted && requestId === latestRequest.current) {
        if (cause && typeof cause === 'object' && 'status' in cause && [401, 403].includes(Number(cause.status))) setOverview(null)
        setError(errorMessage(cause))
      }
    } finally {
      if (inFlight.current === controller) inFlight.current = null
    }
  }, [])

  useEffect(() => {
    mounted.current = true
    void refresh()
    const interval = window.setInterval(() => void refresh(), 5_000)
    return () => {
      mounted.current = false
      latestRequest.current += 1
      inFlight.current?.abort()
      inFlight.current = null
      window.clearInterval(interval)
    }
  }, [refresh])

  return <div className="task-overview-shell">
    <aside className="conversation-sidebar">
      <a className="conversation-sidebar-brand" href="/" aria-label={t('Back to TradeMind website')}><Brand /></a>
      <a className="conversation-new-research" href="/app"><span aria-hidden="true">＋</span> {t('New task')}</a>
      <WorkspaceNav active="tasks" />
    </aside>
    <main>
      <header>
        <div><small>TRADEMIND</small><h1>{t('Task list')}</h1>
          <p>{t('Independent runs continue when you leave the conversation. Return to any task for approvals and results.')}</p>
        </div>
        <button type="button" onClick={() => void refresh()}>{t('Refresh')}</button>
      </header>
      {error && <p role="alert">{error}</p>}
      {!overview && !error && <p>{t('Loading tasks…')}</p>}
      {overview && <>
        {overview.truncated && <p role="status">{t('Showing recent runs; some observations are unavailable. Open a run for its full history.')}</p>}
        {!overview.rows.length && <p>{t('No tasks yet. Start a new task to see it here.')}</p>}
        <ul>
          {overview.rows.map((row) => {
            const params = new URLSearchParams({ project: row.project_id, run: row.run_id })
            return <li key={row.run_id}>
              <a href={`/app?${params}`}>
                <div><strong>{row.prompt}</strong><span data-status={row.status}>{t(row.status === 'waiting_permission' ? 'Waiting permission' : row.status[0].toUpperCase() + row.status.slice(1))}</span></div>
                <p>{row.result_preview ?? (row.observations_available ? t('Open for the latest result and activity.') : t('Open for full run history.'))}</p>
                <small>{row.project_name} · {new Date(row.created_at).toLocaleString(language === 'zh-CN' ? 'zh-CN' : 'en')}</small>
                <footer>
                  <span>{t('Pending approvals')}: {row.pending_approvals}</span>
                  <span>{t('SDK estimated cost')}: {row.estimated_cost_usd === null ? '—' : `$${row.estimated_cost_usd.toFixed(4)}`}</span>
                  <span>{t('Run budget')}: {row.max_budget_usd === null ? '—' : `$${row.max_budget_usd.toFixed(2)}`}</span>
                  {row.sdk_budget_usd !== null && row.max_budget_usd !== row.sdk_budget_usd && <span>{t('SDK budget threshold')}: ${row.sdk_budget_usd.toFixed(2)}</span>}
                  <span>{t('View run')} →</span>
                </footer>
              </a>
            </li>
          })}
        </ul>
      </>}
    </main>
  </div>
}
