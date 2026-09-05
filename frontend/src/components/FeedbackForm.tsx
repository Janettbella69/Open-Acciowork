import { useId, useRef, useState, type FormEvent } from 'react'
import { errorMessage, submitFeedback } from '../api/client'
import type { FeedbackCreate } from '../api/types'
import { useT } from '../i18n'
import './feedback.css'

type FeedbackFormProps = {
  target: Omit<FeedbackCreate, 'user_note'>
  label: string
}

/** Mount/key per anchor so an in-flight response never changes another report. */
export default function FeedbackForm({ target, label }: FeedbackFormProps) {
  const t = useT()
  const noteId = useId()
  const [expanded, setExpanded] = useState(false)
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [sent, setSent] = useState(false)
  const [error, setError] = useState('')
  const inFlight = useRef(false)

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!note.trim() || inFlight.current || sent) return
    inFlight.current = true
    setBusy(true)
    setError('')
    try {
      await submitFeedback({ ...target, user_note: note.trim() })
      setSent(true)
      setExpanded(false)
    } catch (failure) {
      setError(errorMessage(failure))
    } finally {
      inFlight.current = false
      setBusy(false)
    }
  }

  return (
    <div className="feedback-form">
      <button type="button" disabled={sent || busy} onClick={() => setExpanded(!expanded)} aria-expanded={expanded}>
        {sent ? `✓ ${t('Feedback sent')}` : t(label)}
      </button>
      {sent && <small role="status">{t('Saved as an unlabeled candidate for human review. No Skill or run was changed.')}</small>}
      {expanded && (
        <form onSubmit={(event) => void submit(event)}>
          <label htmlFor={noteId}>{t('What is wrong, and why?')}</label>
          <textarea id={noteId} required maxLength={4000} value={note} disabled={busy}
            onChange={(event) => setNote(event.target.value)} />
          <small>{t('Explain the source, number, or reasoning issue. Do not include credentials or private information.')}</small>
          {error && <p role="alert">{t(error)}</p>}
          <button type="submit" disabled={busy || !note.trim()}>{busy ? t('Sending…') : t('Submit feedback')}</button>
          <button type="button" disabled={busy} onClick={() => setExpanded(false)}>{t('Cancel')}</button>
        </form>
      )}
    </div>
  )
}
