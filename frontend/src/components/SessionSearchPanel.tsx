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

  async function search(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const text = query.trim()
    if (!text) return
    pending.current?.abort()
    const controller = new AbortController()
    pending.current = controller
    setLoading(true)
    setError('')
    setResult(null)
    try {
      const found = await searchProjectSessions(projectId, text, controller.signal)
      if (!controller.signal.aborted && found.project_id === projectId) setResult(found)
    } catch {
      if (!controller.signal.aborted) setError(t('Session search could not finish. Please retry.'))
    } finally {
      if (pending.current === controller) {
        pending.current = null
        setLoading(false)
      }
    }
  }

  return (
    <details className="conversation-session-search">
      <summary>{t('Search past conversations')}</summary>
      <form onSubmit={(event) => void search(event)} role="search">
        <label htmlFor="session-search-input">{t('Search this project')}</label>
        <div>
          <input id="session-search-input" value={query} maxLength={200} onChange={(event) => setQuery(event.target.value)} />
          <button type="submit" disabled={loading || !query.trim()}>{loading ? t('Searching…') : t('Search')}</button>
        </div>
      </form>
      {error && <p role="alert">{error}</p>}
      {result && <div aria-live="polite">
        {!result.matches.length && <p>{t('No matching conversations in the scanned history.')}</p>}
        {result.truncated && <p>{t('Search coverage is incomplete. Narrow the search or review older runs.')}</p>}
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
      </div>}
    </details>
  )
}
