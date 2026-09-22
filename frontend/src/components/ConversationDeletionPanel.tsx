import { useEffect, useRef, useState } from 'react'
import { deleteConversation, errorMessage, previewConversationDeletion } from '../api/client'
import type { ConversationDeletionPreview } from '../api/conversationErasure'
import { useT } from '../i18n'
import './project-export.css'

type Props = { projectId: string; runId: string; disabled?: boolean; onDeleted: (projectId: string, runIds: string[]) => void }

function DeletionForm({ projectId, runId, disabled, onDeleted }: Props) {
  const t = useT()
  const [preview, setPreview] = useState<ConversationDeletionPreview | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [uncertain, setUncertain] = useState(false)
  const active = useRef(true)
  const pending = useRef(false)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => {
    active.current = true
    return () => { active.current = false; controller.current?.abort() }
  }, [])

  const review = async () => {
    if (pending.current || disabled) return
    pending.current = true
    controller.current = new AbortController()
    setBusy(true); setError(''); setConfirmed(false); setPreview(null); setUncertain(false)
    try {
      const result = await previewConversationDeletion(projectId, runId, controller.current.signal)
      if (!active.current) return
      if (result.kind !== 'conversation' || result.project_id !== projectId || result.selected_run_id !== runId || !result.run_ids.includes(runId)) throw new Error('Deletion preview did not match this conversation.')
      setPreview(result)
    } catch (failure) {
      if (active.current) setError(errorMessage(failure))
    } finally {
      pending.current = false
      if (active.current) setBusy(false)
    }
  }
  const remove = async () => {
    if (pending.current || !preview || !confirmed || disabled) return
    pending.current = true
    setBusy(true); setError('')
    try {
      // An HTTP abort cannot cancel a committed deletion. Keep the same reviewed
      // request for retry, and deliver success even if its panel has unmounted.
      const result = await deleteConversation(projectId, preview)
      if (result.status !== 'deleted' || result.project_id !== projectId || result.request_id !== preview.request_id || !result.run_ids.includes(runId)) throw new Error('Deletion result could not be verified.')
      onDeleted(projectId, result.run_ids)
      if (active.current) { setPreview(null); setConfirmed(false); setUncertain(false) }
    } catch (failure) {
      if (active.current) { setError(errorMessage(failure)); setUncertain(true) }
    } finally {
      pending.current = false
      if (active.current) setBusy(false)
    }
  }
  return <section className="project-export" aria-label={t('Delete conversation')}>
    <div className="project-export-body">
      <button type="button" disabled={busy || disabled} onClick={() => void review()}>{t('Review conversation deletion')}</button>
      {busy && <p role="status">{t('Checking conversation deletion…')}</p>}
      {error && <p role="alert">{t(error)}</p>}
      {uncertain && <p role="status">{t('The request may already be committed. Retry the same confirmation to check its result. A disconnected request does not cancel deletion.')}</p>}
      {preview && <div className="project-export-review">
        <strong>{t('Delete this conversation and its continuations')}</strong>
        <p>{t('{runs} runs · {files} stored records/files · {memories} source memories', { runs: preview.run_ids.length, files: preview.file_count, memories: preview.memory_count })}</p>
        <p>{t('Removes conversation text, events, native session history and its attributable memory and diary entries. You cannot resume the deleted conversation.')}</p>
        <p>{t('Saved project files, evidence, decisions, comments, templates and Skills remain. Other conversations, independent copies, credentials, existing backups and external services remain; unattributed runtime caches and OS temporary files are outside this deletion.')}</p>
        <label><input type="checkbox" checked={confirmed} disabled={busy || disabled} onChange={(event) => setConfirmed(event.target.checked)} /> {t('I understand this deletion cannot be undone and saved project outputs will remain.')}</label>
        <div className="project-export-actions">
          <button type="button" disabled={busy || disabled || !confirmed} onClick={() => void remove()}>{t(uncertain ? 'Retry reviewed deletion' : 'Permanently delete conversation')}</button>
          <button type="button" disabled={busy} onClick={() => { setPreview(null); setConfirmed(false) }}>{t('Close review')}</button>
        </div>
      </div>}
    </div>
  </section>
}

export default function ConversationDeletionPanel(props: Props) {
  return <DeletionForm key={`${props.projectId}:${props.runId}`} {...props} />
}
