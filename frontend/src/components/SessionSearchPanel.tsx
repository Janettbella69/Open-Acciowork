import { useEffect, useRef, useState, type FormEvent } from 'react'
import { searchProjectSessions } from '../api/client'
import type { SessionSearch } from '../api/sessionSearch'
import { useT } from '../i18n'

export default function SessionSearchPanel({ projectId, onSelectRun }: {
  projectId: string
  onSelectRun: (runId: string) => void
}) {
  const t = useT()
  const [query, setQuery] = useState('')
  const [result, setResult] = useState<SessionSearch | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const pending = useRef<AbortController | null>(null)

  useEffect(() => {
    setQuery('')
    setResult(null)
    setError('')
    setLoading(false)
    return () => {
      pending.current?.abort()
      pending.current = null
    }
  }, [projectId])

  async function search(text: string, cursor?: string) {
    if (!text || loading) return
    pending.current?.abort()
    const controller = new AbortController()
    pending.current = controller
    setLoading(true)
    setError('')
    if (!cursor) setResult(null)
    try {
      const found = await searchProjectSessions(projectId, text, controller.signal, cursor)
      if (!controller.signal.aborted && found.project_id === projectId) {
        setResult((current) => {
          if (!cursor || !current) return found
          const skipped = current.skipped_event_logs + found.skipped_event_logs
          return {
            ...found, matches: [...current.matches, ...found.matches],
            scanned_runs: current.scanned_runs + found.scanned_runs,
            skipped_event_logs: skipped, truncated: found.next_cursor !== null || skipped > 0,
          }
        })
      }
    } catch {
      if (!controller.signal.aborted) setError(t('Session search could not finish. Please retry.'))
    } finally {
      if (pending.current === controller) {
        pending.current = null
        setLoading(false)
      }
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    void search(query.trim())
  }

  function changeQuery(value: string) {
    pending.current?.abort()
    pending.current = null
    setLoading(false)
    setQuery(value)
    setResult(null)
    setError('')
  }

  return (
    <details className="conversation-session-search">
      <summary>{t('Search past conversations')}</summary>
      <form onSubmit={submit} role="search">
        <label htmlFor="session-search-input">{t('Search this project')}</label>
        <div>
          <input id="session-search-input" value={query} maxLength={200} onChange={(event) => changeQuery(event.target.value)} />
          <button type="submit" disabled={loading || !query.trim()}>{loading ? t('Searching…') : t('Search')}</button>
        </div>
      </form>
      {error && <p role="alert">{error}</p>}
      {result && <div aria-live="polite">
        {!result.matches.length && <p>{t('No matching conversations in the scanned history.')}</p>}
        {result.skipped_event_logs > 0 && <p>{t('Some event logs could not be searched. Results may be incomplete.')}</p>}
        {result.next_cursor && <p>{t('More history remains. Continue searching older runs.')}</p>}
        <ul>
          {result.matches.map((match) => <li key={match.run_id}>
            <button type="button" onClick={() => onSelectRun(match.run_id)}>
              <strong>{match.excerpt}</strong>
              <small>{t('Run')} {match.run_id} · {t({
                prompt: 'Prompt', user_message: 'User message',
                assistant_message: 'Assistant message', result: 'Result',
              }[match.source])} · {t(match.status)}</small>
            </button>
          </li>)}
        </ul>
        {result.next_cursor && <button type="button" disabled={loading}
          onClick={() => void search(result.query.trim(), result.next_cursor ?? undefined)}>{t('Search older runs')}</button>}
      </div>}
    </details>
  )
}
