import { useEffect, useState } from 'react'
import { decideMemory, errorMessage, listMemory } from '../api/client'
import type { MemoryItem } from '../api/types'

const kindLabels: Record<MemoryItem['kind'], string> = {
  preference: 'Preference',
  fact: 'Fact',
  decision: 'Decision',
  lesson: 'Lesson',
}

export default function MemoryReview() {
  const [items, setItems] = useState<MemoryItem[]>([])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    void listMemory()
      .then((memories) => { if (active) setItems(memories) })
      .catch((loadError) => { if (active) setError(errorMessage(loadError)) })
    return () => { active = false }
  }, [])

  const decide = async (memoryId: string, decision: 'approve' | 'reject') => {
    setBusy(memoryId)
    setError('')
    try {
      const updated = await decideMemory(memoryId, decision)
      setItems((current) => current.map((item) => (item.id === updated.id ? updated : item)))
    } catch (decisionError) {
      setError(errorMessage(decisionError))
    } finally {
      setBusy('')
    }
  }

  const proposed = items.filter((item) => item.status === 'proposed')
  const decided = items.filter((item) => item.status !== 'proposed')

  return (
    <div className="settings-card memory-review">
      {error && <p className="conversation-error" role="alert">{error}</p>}

      {proposed.length ? proposed.map((item) => (
        <article key={item.id} className="memory-review-item">
          <div>
            <p>{item.text}</p>
            <small>
              {kindLabels[item.kind]} · scope {item.scope}
              {' · proposed '}
              {new Date(item.created_at).toLocaleDateString()}
            </small>
          </div>
          <div className="memory-review-actions">
            <button type="button" disabled={busy === item.id} onClick={() => void decide(item.id, 'approve')}>
              Approve
            </button>
            <button type="button" data-tone="danger" disabled={busy === item.id} onClick={() => void decide(item.id, 'reject')}>
              Reject
            </button>
          </div>
        </article>
      )) : (
        <p className="settings-integration-note">
          No memory proposals are waiting for review. Agent proposals appear here and only
          take effect after you approve them.
        </p>
      )}

      {decided.length > 0 && (
        <details className="memory-review-history">
          <summary>Reviewed memories ({decided.length})</summary>
          {decided.map((item) => (
            <p key={item.id}>
              <span data-status={item.status}>{item.status}</span> {item.text}
            </p>
          ))}
        </details>
      )}
    </div>
  )
}
