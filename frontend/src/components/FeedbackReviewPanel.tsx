import { useEffect, useRef, useState, type FormEvent } from 'react'
import { errorMessage, listFeedback, reviewFeedback, runEventUrl } from '../api/client'
import type { FeedbackItem, FeedbackReview, FeedbackStatus } from '../api/types'
import { useT } from '../i18n'
import './feedback.css'

const statusLabels: Record<FeedbackStatus, string> = {
  open: 'Open', reviewed: 'Reviewed', rejected: 'Rejected', folded_into_skill: 'Folded into Skill',
}

function ReviewForm({ item, onChanged }: { item: FeedbackItem; onChanged: (item: FeedbackItem) => void }) {
  const t = useT()
  const [next, setNext] = useState<FeedbackStatus>(item.status === 'open' ? 'reviewed' : 'open')
  const [note, setNote] = useState('')
  const [skillId, setSkillId] = useState('')
  const [hash, setHash] = useState('')
  const [report, setReport] = useState('')
  const [confirmChange, setConfirmChange] = useState(false)
  const [confirmL2, setConfirmL2] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const inFlight = useRef(false)
  const active = useRef(false)
  useEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])
  const options: FeedbackStatus[] = item.status === 'open' ? ['reviewed', 'rejected']
    : item.status === 'reviewed' ? ['open', 'rejected', 'folded_into_skill'] : ['open']

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (inFlight.current || !note.trim() || !item.revision) return
    inFlight.current = true
    setBusy(true)
    setError('')
    const payload: FeedbackReview = { expected_revision: item.revision, status: next, note: note.trim() }
    if (next === 'folded_into_skill') Object.assign(payload, {
      skill_id: skillId.trim(), skill_content_hash: hash.trim(), regression_report: report.trim(),
      manual_skill_update_confirmed: confirmChange, l2_regression_passed_confirmed: confirmL2,
    })
    try {
      const updated = await reviewFeedback(item.id, payload)
      if (active.current) onChanged(updated)
    } catch (failure) {
      if (active.current) setError(errorMessage(failure))
    } finally {
      inFlight.current = false
      if (active.current) setBusy(false)
    }
  }
  if (!item.context || !item.revision) return <p>{t('Legacy report has no trace anchor. Submit a new anchored report before review.')}</p>
  return (
    <form onSubmit={(event) => void submit(event)}>
      <label>{t('Review decision')}
        <select value={next} onChange={(event) => setNext(event.target.value as FeedbackStatus)} disabled={busy}>
          {options.map((status) => <option key={status} value={status}>{t(statusLabels[status])}</option>)}
        </select>
      </label>
      <label>{t('Review reason')}<textarea value={note} onChange={(event) => setNote(event.target.value)} required maxLength={4000} disabled={busy} /></label>
      {next === 'folded_into_skill' && <>
        <p>{t('This records your manual work and attestation only. It does not modify a Skill or certify an L2 release gate.')}</p>
        <label>{t('Skill ID')}<input value={skillId} onChange={(event) => setSkillId(event.target.value)} required maxLength={200} disabled={busy} /></label>
        <label>{t('Skill content hash')}<input value={hash} onChange={(event) => setHash(event.target.value)} pattern="[0-9a-f]{64}" required disabled={busy} /></label>
        <label>{t('L2 regression report reference')}<input value={report} onChange={(event) => setReport(event.target.value)} required maxLength={1000} disabled={busy} /></label>
        <label><input type="checkbox" checked={confirmChange} onChange={(event) => setConfirmChange(event.target.checked)} required disabled={busy} />{t('I manually updated and reviewed this exact Skill version.')}</label>
        <label><input type="checkbox" checked={confirmL2} onChange={(event) => setConfirmL2(event.target.checked)} required disabled={busy} />{t('I ran and reviewed the full L2 regression report.')}</label>
      </>}
      {error && <p role="alert">{t(error)}</p>}
      <button type="submit" disabled={busy || !note.trim()}>{busy ? t('Saving…') : t('Save review')}</button>
    </form>
  )
}

function ProjectFeedbackReview({ projectId }: { projectId: string }) {
  const t = useT()
  const [items, setItems] = useState<FeedbackItem[]>([])
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [error, setError] = useState('')
  const inFlight = useRef(false)
  const active = useRef(false)
  const nextOffset = useRef(0)
  // A refresh may replace the visible page, but must never lower a revision
  // already observed from either a review response or another loaded page.
  const latest = useRef(new Map<string, FeedbackItem>())
  useEffect(() => {
    active.current = true
    return () => { active.current = false }
  }, [])

  const remember = (incoming: FeedbackItem): FeedbackItem => {
    const previous = latest.current.get(incoming.id)
    if (previous && (previous.revision ?? 0) > (incoming.revision ?? 0)) return previous
    latest.current.set(incoming.id, incoming)
    return incoming
  }

  const changed = (updated: FeedbackItem) => {
    if (!active.current || updated.project_id !== projectId) return
    const newest = remember(updated)
    setItems((current) => current.map((row) => row.id === newest.id ? newest : row))
  }

  const load = async (append = false) => {
    if (inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError('')
    const offset = append ? nextOffset.current : 0
    try {
      const page = await listFeedback(projectId, undefined, offset)
      if (!active.current) return
      // Server offset counts every row consumed, including duplicates caused by
      // newer records being inserted before a previously fetched page.
      nextOffset.current = offset + page.length
      const incoming = page.map(remember)
      setItems((current) => {
        const merged = new Map((append ? current : []).map((row) => [row.id, row]))
        for (const row of incoming) merged.set(row.id, latest.current.get(row.id) ?? row)
        return [...merged.values()]
      })
      setHasMore(page.length === 200)
      setLoaded(true)
    } catch (failure) {
      if (active.current) setError(errorMessage(failure))
    } finally {
      inFlight.current = false
      if (active.current) setBusy(false)
    }
  }

  const download = () => {
    // Explicit operator export. Omit request identities/session binding metadata.
    const rows = items.filter((item) => item.status !== 'rejected' && item.context && item.run_id).map((item) => ({
      id: item.id, project_id: item.project_id, run_id: item.run_id, message_id: item.message_id,
      evidence_id: item.evidence_id, kind: item.kind, user_note: item.user_note, source: item.source,
      candidate_state: 'unlabeled', status: item.status, created_at: item.created_at, context: item.context,
    }))
    const url = URL.createObjectURL(new Blob([JSON.stringify(rows, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `feedback-candidates-${projectId}.json`
    link.click()
    URL.revokeObjectURL(url)
  }

  return (
    <details className="feedback-review" onToggle={(event) => {
      if (event.currentTarget.open && !loaded) void load()
    }}>
      <summary>{t('Feedback review and evaluation candidates')}</summary>
      <p>{t('Only an administrator can review reports. Candidates are not gold labels; the project owner must label and calibrate them personally.')}</p>
      <div className="feedback-review-actions">
        <button type="button" disabled={busy} onClick={() => void load()}>{t('Refresh feedback')}</button>
        <button type="button" disabled={!items.some((item) => item.context && item.status !== 'rejected')} onClick={download}>{t('Export loaded unlabeled candidates')}</button>
      </div>
      <small>{t('Exports only the loaded, non-rejected reports. Review business-data sensitivity before moving the file into evaluation storage.')}</small>
      {error && <p role="alert">{t(error)}</p>}
      {loaded && !items.length && <p>{t('No feedback has been recorded for this project.')}</p>}
      {items.map((item) => (
        <article key={item.id}>
          <strong>{t(statusLabels[item.status])} · {item.kind}</strong>
          <p>{item.user_note}</p>
          <small>{item.source === 'system' ? t('Automatically collected; no quality verdict assigned.') : t('User report')}</small>
          {item.context && <>
            <p>{t('Trace')} <code>{item.context.trace_id}</code> · {t('Run')} <code>{item.run_id}</code></p>
            {item.run_id && <a href={runEventUrl(item.run_id)} target="_blank" rel="noreferrer">{t('Open recorded event trace')}</a>}
            {item.context.event_id && <p>{t('Message / event')} <code>{item.context.event_id}</code></p>}
            {item.context.message_text && <blockquote>{item.context.message_text}</blockquote>}
            {item.context.evidence && <blockquote>
              <code>{item.context.evidence.id}</code> · {item.context.evidence.claim}<br />
              {item.context.evidence.source_url
                ? <a href={item.context.evidence.source_url} target="_blank" rel="noreferrer">{item.context.evidence.source_title}</a>
                : item.context.evidence.source_title}
            </blockquote>}
          </>}
          {(item.reviews ?? []).map((review, index) => <p key={index}>
            {t(statusLabels[review.to_status])} · {review.note}
            {review.skill_id && <> · <code>{review.skill_id}@{review.skill_content_hash}</code> · {review.regression_report}</>}
          </p>)}
          <details><summary>{t('Review this report')}</summary>
            <ReviewForm key={`${item.id}:${item.revision}`} item={item} onChanged={changed} />
          </details>
        </article>
      ))}
      {hasMore && <button type="button" disabled={busy} onClick={() => void load(true)}>{t('Load more feedback')}</button>}
    </details>
  )
}

export default function FeedbackReviewPanel({ projectId }: { projectId: string }) {
  // Keep page state, pagination, and outstanding requests inside one project
  // lifetime even when a caller changes projectId without providing a React key.
  return <ProjectFeedbackReview key={projectId} projectId={projectId} />
}
