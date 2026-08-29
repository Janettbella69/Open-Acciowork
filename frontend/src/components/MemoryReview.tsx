import { useEffect, useState } from 'react'
import { decideMemory, errorMessage, listMemory } from '../api/client'
import type { MemoryItem } from '../api/types'
import { useLanguage, useT } from '../i18n'

const kindLabels: Record<MemoryItem['kind'], string> = {
  preference: 'Preference',
  fact: 'Fact',
  decision: 'Decision',
  lesson: 'Lesson',
}

const scopeLabels: Record<MemoryItem['scope'], string> = {
  user: 'User scope',
  project: 'Project scope',
  agent: 'Agent scope',
}

const statusLabels: Record<MemoryItem['status'], string> = {
  proposed: 'Proposed',
  approved: 'Approved',
  rejected: 'Rejected',
  disabled: 'Disabled',
}

export default function MemoryReview() {
  const t = useT()
  const language = useLanguage()
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
      {error && <p className="conversation-error" role="alert">{t(error)}</p>}

      {proposed.length ? proposed.map((item) => (
        <article key={item.id} className="memory-review-item">
          <div>
            <p>{item.text}</p>
            <small>
              {t(kindLabels[item.kind])} · {t(scopeLabels[item.scope])} · {t('Proposed {date}', {
                date: new Date(item.created_at).toLocaleDateString(language === 'zh-CN' ? 'zh-CN' : 'en'),
              })}
            </small>
          </div>
          <div className="memory-review-actions">
            <button type="button" disabled={busy === item.id} onClick={() => void decide(item.id, 'approve')}>
              {t('Approve')}
            </button>
            <button type="button" data-tone="danger" disabled={busy === item.id} onClick={() => void decide(item.id, 'reject')}>
              {t('Reject')}
            </button>
          </div>
        </article>
      )) : (
        <p className="settings-integration-note">
          {t('No memory proposals are waiting for review. Agent proposals appear here and only take effect after you approve them.')}
        </p>
      )}

      {decided.length > 0 && (
        <details className="memory-review-history">
          <summary>{t('Reviewed memories ({n})', { n: decided.length })}</summary>
          {decided.map((item) => (
            <p key={item.id}>
              <span data-status={item.status}>{t(statusLabels[item.status])}</span> {item.text}
            </p>
          ))}
        </details>
      )}
    </div>
  )
}
