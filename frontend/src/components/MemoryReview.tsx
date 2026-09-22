import { useEffect, useRef, useState } from 'react'
import { correctMemory, decideMemory, errorMessage, listMemory } from '../api/client'
import type { MemoryItem, MemoryKind, MemorySensitivity } from '../api/types'
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

type MemoryDraft = {
  text: string
  kind: MemoryKind
  sensitivity: MemorySensitivity
}

type MemoryReviewProps = { sourceRunId?: string; revision?: number }

export default function MemoryReview(props: MemoryReviewProps) {
  return <MemoryReviewContent key={props.sourceRunId ?? 'all'} {...props} />
}

function MemoryReviewContent({ sourceRunId, revision = 0 }: MemoryReviewProps) {
  const t = useT()
  const language = useLanguage()
  const [items, setItems] = useState<MemoryItem[]>([])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [loadError, setLoadError] = useState('')
  const [editingId, setEditingId] = useState('')
  const [draft, setDraft] = useState<MemoryDraft | null>(null)
  const actionInFlightRef = useRef('')
  const requestRef = useRef(0)
  const [reloadVersion, setReloadVersion] = useState(0)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    if (actionInFlightRef.current) return
    let active = true
    const request = ++requestRef.current
    const controller = new AbortController()
    const current = () => active && request === requestRef.current
    setLoading(true)
    void listMemory(sourceRunId, controller.signal)
      .then((memories) => {
        if (!current()) return
        if (sourceRunId && memories.some((item) => item.source_run_id !== sourceRunId)) {
          throw new Error('Memory response did not match this run.')
        }
        setItems(memories)
        setLoadError('')
      })
      .catch((cause) => { if (current()) setLoadError(errorMessage(cause)) })
      .finally(() => { if (current()) setLoading(false) })
    return () => { active = false; controller.abort() }
  }, [sourceRunId, revision, reloadVersion])

  const decide = async (memoryId: string, decision: 'approve' | 'reject') => {
    if (actionInFlightRef.current) return
    actionInFlightRef.current = memoryId
    requestRef.current += 1
    setBusy(memoryId)
    setError('')
    try {
      const updated = await decideMemory(memoryId, decision)
      setItems((current) => current.map((item) => (item.id === updated.id ? updated : item)))
    } catch (decisionError) {
      setError(errorMessage(decisionError))
    } finally {
      actionInFlightRef.current = ''
      setBusy('')
      setReloadVersion((value) => value + 1)
    }
  }

  const beginEdit = (item: MemoryItem) => {
    if (actionInFlightRef.current) return
    setEditingId(item.id)
    setDraft({ text: item.text, kind: item.kind, sensitivity: item.sensitivity })
    setError('')
  }

  const cancelEdit = () => {
    if (actionInFlightRef.current) return
    setEditingId('')
    setDraft(null)
  }

  const saveEdit = async (item: MemoryItem) => {
    if (!draft || !draft.text.trim() || actionInFlightRef.current) return
    actionInFlightRef.current = item.id
    requestRef.current += 1
    setBusy(item.id)
    setError('')
    try {
      const updated = await correctMemory(item.id, {
        text: draft.text.trim(),
        kind: draft.kind,
        sensitivity: draft.sensitivity,
      })
      setItems((current) => current.map((entry) => entry.id === updated.id ? updated : entry))
      setEditingId('')
      setDraft(null)
    } catch (editError) {
      setError(errorMessage(editError))
    } finally {
      actionInFlightRef.current = ''
      setBusy('')
      setReloadVersion((value) => value + 1)
    }
  }

  const proposed = items.filter((item) => item.status === 'proposed')
  const decided = items.filter((item) => item.status !== 'proposed')

  const renderEditor = (item: MemoryItem) => draft && (
    <form
      className="memory-review-editor"
      aria-label={t('Edit memory')}
      onSubmit={(event) => { event.preventDefault(); void saveEdit(item) }}
    >
      <label>
        {t('Memory text')}
        <textarea
          value={draft.text}
          onChange={(event) => setDraft((current) => current ? { ...current, text: event.target.value } : current)}
          rows={3}
          required
        />
      </label>
      <label>
        {t('Kind')}
        <select value={draft.kind} onChange={(event) => setDraft((current) => current ? { ...current, kind: event.target.value as MemoryKind } : current)}>
          {Object.entries(kindLabels).map(([kind, label]) => <option key={kind} value={kind}>{t(label)}</option>)}
        </select>
      </label>
      <label>
        {t('Sensitivity')}
        <select value={draft.sensitivity} onChange={(event) => setDraft((current) => current ? { ...current, sensitivity: event.target.value as MemorySensitivity } : current)}>
          <option value="normal">{t('Normal')}</option>
          <option value="sensitive">{t('Sensitive')}</option>
        </select>
      </label>
      <small>{t('Saving a correction creates a new approved version.')}</small>
      <div className="memory-review-actions">
        <button type="submit" disabled={busy === item.id || !draft.text.trim()}>{busy === item.id ? t('Saving…') : t('Save')}</button>
        <button type="button" disabled={busy === item.id} onClick={cancelEdit}>{t('Cancel')}</button>
      </div>
    </form>
  )

  return (
    <div className="settings-card memory-review">
      {error && <p className="conversation-error" role="alert">{t(error)}</p>}
      {loadError && <p className="conversation-error" role="alert">{t(loadError)}</p>}
      {loading && <p role="status">{t('Refreshing memories…')}</p>}
      <button type="button" disabled={Boolean(busy) || loading} onClick={() => setReloadVersion((value) => value + 1)}>{t('Refresh')}</button>

      {proposed.length ? proposed.map((item) => (
        <article key={item.id} className="memory-review-item">
          {editingId === item.id ? renderEditor(item) : (
            <>
              <div>
                <p>{item.text}</p>
                <small>
                  {t(kindLabels[item.kind])} · {t(scopeLabels[item.scope])} · {t('Proposed {date}', {
                    date: new Date(item.created_at).toLocaleDateString(language === 'zh-CN' ? 'zh-CN' : 'en'),
                  })}
                </small>
              </div>
              <div className="memory-review-actions">
                <button type="button" disabled={Boolean(busy)} onClick={() => beginEdit(item)}>{t('Edit')}</button>
                <button type="button" disabled={Boolean(busy)} onClick={() => void decide(item.id, 'approve')}>{t('Approve')}</button>
                <button type="button" data-tone="danger" disabled={Boolean(busy)} onClick={() => void decide(item.id, 'reject')}>{t('Reject')}</button>
              </div>
            </>
          )}
        </article>
      )) : !loading && !error && !loadError && (
        <p className="settings-integration-note">
          {t('No memory proposals are waiting for review. Agent proposals appear here and only take effect after you approve them.')}
        </p>
      )}

      {decided.length > 0 && (
        <details className="memory-review-history">
          <summary>{t('Reviewed memories ({n})', { n: decided.length })}</summary>
          {decided.map((item) => (
            <article key={item.id} className="memory-review-item memory-review-history-item">
              {editingId === item.id ? renderEditor(item) : (
                <>
                  <div>
                    <p><span data-status={item.status}>{t(statusLabels[item.status])}</span> {item.text}</p>
                    <small>{t(kindLabels[item.kind])} · {t(scopeLabels[item.scope])} · v{item.version}</small>
                  </div>
                  <div className="memory-review-actions">
                    <button type="button" disabled={Boolean(busy)} onClick={() => beginEdit(item)}>{t('Edit')}</button>
                  </div>
                </>
              )}
            </article>
          ))}
        </details>
      )}
    </div>
  )
}
