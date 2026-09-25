import { useEffect, useId, useRef, useState } from 'react'
import { changeWorkspaceRetention, errorMessage, getWorkspaceArchivePage, getWorkspaceRetention } from '../api/client'
import type { RetentionArchivedReview, WorkspaceRetention } from '../api/workspaceRecovery'
import { useT } from '../i18n'

type Kind = 'checkpoint' | 'review' | 'copy' | 'archive' | 'archived' | 'blobs'
type Selection = { kind: Kind; id: string; fingerprint: string }
type Props = { projectId: string; onChanged: () => void }
const pageSize = 50

export default function WorkspaceRetentionPanel({ projectId, onChanged }: Props) {
  const t = useT()
  const regionId = useId()
  const [open, setOpen] = useState(false)
  const [inventory, setInventory] = useState<WorkspaceRetention | null>(null)
  const [archivedReviews, setArchivedReviews] = useState<RetentionArchivedReview[] | null>(null)
  const [archiveCursor, setArchiveCursor] = useState<string | null>(null)
  const [selection, setSelection] = useState<Selection | null>(null)
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [shownPoints, setShownPoints] = useState(pageSize)
  const [shownReviews, setShownReviews] = useState(pageSize)
  const controller = useRef<AbortController | null>(null)

  useEffect(() => {
    controller.current = new AbortController()
    return () => controller.current?.abort()
  }, [])

  const accept = (value: WorkspaceRetention) => {
    if (value.project_id !== projectId) throw new Error('Retention response did not match this project.')
    setInventory(value)
    setSelection(null)
    setConfirmed(false)
  }
  const load = async () => {
    if (busy) return
    setBusy(true); setError('')
    try {
      const value = await getWorkspaceRetention(projectId, controller.current?.signal)
      if (!controller.current?.signal.aborted) {
        accept(value); setArchivedReviews(null); setArchiveCursor(null)
      }
    } catch (reason) {
      if (!controller.current?.signal.aborted) {
        const status = reason && typeof reason === 'object' && 'status' in reason ? Number(reason.status) : null
        if (status === 403 || status === 404) {
          setInventory(null); setArchivedReviews(null); setSelection(null); setConfirmed(false)
        }
        setError(status === 403 ? 'Only a project owner or administrator can manage recovery history.' : errorMessage(reason))
      }
    } finally { if (!controller.current?.signal.aborted) setBusy(false) }
  }
  const loadArchives = async (cursor?: string) => {
    if (busy) return
    setBusy(true); setError('')
    try {
      const page = await getWorkspaceArchivePage(projectId, cursor, controller.current?.signal)
      if (!controller.current?.signal.aborted) {
        if (page.project_id !== projectId) throw new Error('Archive response did not match this project.')
        setArchivedReviews((current) => cursor ? [...(current ?? []), ...page.reviews] : page.reviews)
        setArchiveCursor(page.next_cursor)
      }
    } catch (reason) {
      if (!controller.current?.signal.aborted) {
        const status = reason && typeof reason === 'object' && 'status' in reason ? Number(reason.status) : null
        if (status === 403 || status === 404) {
          setInventory(null); setArchivedReviews(null); setSelection(null); setConfirmed(false)
        }
        setError(status === 403 ? 'Only a project owner or administrator can manage recovery history.' : errorMessage(reason))
      }
    } finally { if (!controller.current?.signal.aborted) setBusy(false) }
  }
  const choose = (kind: Kind, id: string, fingerprint: string) => {
    setSelection({ kind, id, fingerprint }); setConfirmed(false); setError('')
  }
  const submit = async () => {
    if (!selection || !confirmed || busy) return
    setBusy(true); setError('')
    try {
      const value = await changeWorkspaceRetention(
        projectId, selection.kind, selection.id, selection.fingerprint,
        controller.current?.signal,
      )
      if (!controller.current?.signal.aborted) {
        accept(value); onChanged()
        if (archivedReviews !== null && (selection.kind === 'archive' || selection.kind === 'archived')) {
          const page = await getWorkspaceArchivePage(projectId, undefined, controller.current?.signal)
          if (!controller.current?.signal.aborted) {
            if (page.project_id !== projectId) throw new Error('Archive response did not match this project.')
            setArchivedReviews(page.reviews); setArchiveCursor(page.next_cursor)
          }
        }
      }
    } catch (reason) {
      if (!controller.current?.signal.aborted) {
        const status = reason && typeof reason === 'object' && 'status' in reason ? Number(reason.status) : null
        if (status === 403 || status === 404) {
          setInventory(null); setArchivedReviews(null); setSelection(null); setConfirmed(false)
        }
        setError(status === 403 ? 'Only a project owner or administrator can manage recovery history.' : errorMessage(reason))
      }
    } finally { if (!controller.current?.signal.aborted) setBusy(false) }
  }
  const action = selection?.kind === 'checkpoint' ? 'Delete recovery point'
    : selection?.kind === 'review' ? 'Delete expired review'
      : selection?.kind === 'copy' ? 'Discard retained workspace folder'
        : selection?.kind === 'archive' ? 'Archive applied review'
          : selection?.kind === 'archived' ? 'Delete archived review' : 'Reclaim unreferenced snapshots'

  return <section className="workspace-retention" aria-label={t('Recovery history management')}>
    <button type="button" aria-expanded={open} aria-controls={regionId}
      onClick={() => { setOpen(!open); if (!open && !inventory) void load() }}>
      {t('Recovery history management')} <span aria-hidden="true">{open ? '−' : '+'}</span>
    </button>
    {open && <div id={regionId} aria-busy={busy}>
      <p>{t('Project owners and administrators can remove old recovery records after review. No history is removed automatically.')}</p>
      {error && <p role="alert">{t(error)}</p>}
      {busy && <p role="status">{t('Checking recovery history…')}</p>}
      <button type="button" disabled={busy} onClick={() => void load()}>{t('Refresh recovery history')}</button>
      {inventory && <>
        <p>{t('{points} of {pointLimit} recovery points · {reviews} of {reviewLimit} active reviews · {archives} archived reviews · {bytes} of {byteLimit} retained folder bytes', {
          points: inventory.checkpoints.length, pointLimit: inventory.checkpoint_limit,
          reviews: inventory.reviews.length, reviewLimit: inventory.review_limit,
          archives: inventory.archived_reviews,
          bytes: inventory.retained_bytes.toLocaleString(), byteLimit: inventory.retained_bytes_limit.toLocaleString(),
        })}</p>
        <p>{t('{files} snapshot files · {bytes} bytes · {reclaimable} files ({reclaimBytes} bytes) unreferenced', {
          files: inventory.snapshot_files, bytes: inventory.snapshot_bytes.toLocaleString(),
          reclaimable: inventory.reclaimable_files, reclaimBytes: inventory.reclaimable_bytes.toLocaleString(),
        })}</p>
        {inventory.reclaimable_files > 0 && <button type="button" disabled={busy}
          onClick={() => choose('blobs', `${inventory.reclaimable_files} snapshot files`, inventory.reclaim_fingerprint)}>
          {t('Reclaim unreferenced snapshots')}</button>}
        {selection && <div className="recovery-retention-confirm">
          <h5>{t(action)}</h5>
          <code>{selection.id}</code>
          <p>{t(selection.kind === 'checkpoint'
            ? 'This recovery point cannot be restored after deletion. Project evidence and artifact versions remain.'
            : selection.kind === 'review'
              ? 'This expired review has never changed workspace files.'
              : selection.kind === 'copy'
                ? 'The extra retained folder will be deleted. Its safety recovery point remains available.'
                : selection.kind === 'archive'
                  ? 'The applied review will move to the archive. Its result remains available by ID; recovery points can then be removed separately.'
                  : selection.kind === 'archived'
                    ? 'This archived review result will be permanently removed. Workspace files, recovery points and artifact versions remain.'
                  : 'Only snapshots with no artifact or recovery point reference will be deleted. Review the file count and byte total above.')}</p>
          <label className="recovery-confirm"><input type="checkbox" checked={confirmed} disabled={busy}
            onChange={(event) => setConfirmed(event.target.checked)} />
            {t('I confirm this recovery history deletion.')}</label>
          <div className="recovery-actions">
            <button type="button" disabled={busy} onClick={() => { setSelection(null); setConfirmed(false) }}>{t('Cancel')}</button>
            <button type="button" disabled={busy || !confirmed} onClick={() => void submit()}>{t(action)}</button>
          </div>
        </div>}
        <h5>{t('Recovery points')}</h5>
        <ul className="recovery-retention-list">{inventory.checkpoints.slice(0, shownPoints).map((item) => <li key={item.id}>
          <div><code>{item.id}</code><small>{new Date(item.created_at).toLocaleString()} · {t('{count} files · {size} bytes', {
            count: item.files, size: item.total_bytes.toLocaleString(),
          })}</small>
            {item.referenced_by_reviews > 0 && <small>{t('Used by {count} saved reviews', { count: item.referenced_by_reviews })}</small>}
          </div>
          <button type="button" disabled={busy || item.referenced_by_reviews > 0}
            onClick={() => choose('checkpoint', item.id, item.fingerprint)}>{t('Delete recovery point')}</button>
        </li>)}</ul>
        {inventory.checkpoints.length > shownPoints && <button type="button" onClick={() => setShownPoints(shownPoints + pageSize)}>{t('Show more recovery points')}</button>}
        <h5>{t('Recovery reviews and retained folders')}</h5>
        <ul className="recovery-retention-list">{inventory.reviews.slice(0, shownReviews).map((item) => <li key={item.id}>
          <div><code>{item.id}</code><small>{new Date(item.created_at).toLocaleString()} · {t(item.state)}</small>
            {item.state === 'applied' && <small>{item.retained_discarded_at
              ? t('Retained folder discarded; safety point kept')
              : t('{size} retained folder bytes', { size: item.retained_bytes.toLocaleString() })}</small>}
          </div>
          {item.can_delete && <button type="button" disabled={busy}
            onClick={() => choose('review', item.id, item.fingerprint)}>{t('Delete expired review')}</button>}
          {item.state === 'applied' && !item.retained_discarded_at && <button type="button" disabled={busy}
            onClick={() => choose('copy', item.id, item.fingerprint)}>{t('Discard retained workspace folder')}</button>}
          {item.can_archive && <button type="button" disabled={busy}
            onClick={() => choose('archive', item.id, item.fingerprint)}>{t('Archive applied review')}</button>}
        </li>)}</ul>
        {inventory.reviews.length > shownReviews && <button type="button" onClick={() => setShownReviews(shownReviews + pageSize)}>{t('Show more recovery reviews')}</button>}
        <h5>{t('Archived recovery reviews')}</h5>
        <p>{t('Archived reviews remain available by ID until an administrator deletes each result.')}</p>
        {archivedReviews === null
          ? <button type="button" disabled={busy || inventory.archived_reviews === 0} onClick={() => void loadArchives()}>{t('View archived reviews')}</button>
          : <>
            <ul className="recovery-retention-list">{archivedReviews.map((item) => <li key={item.id}>
              <div><code>{item.id}</code>
                <small>{t('Created {time} · Recovery point {id}', {
                  time: new Date(item.created_at).toLocaleString(), id: item.checkpoint_id,
                })}</small>
                <small>{t('Archived {time} by {actor}', {
                  time: new Date(item.archived_at).toLocaleString(), actor: item.archived_by,
                })}</small>
              </div>
              <button type="button" disabled={busy} onClick={() => choose('archived', item.id, item.fingerprint)}>{t('Delete archived review')}</button>
            </li>)}</ul>
            {archivedReviews.length === 0 && <p>{t('No archived reviews.')}</p>}
            {archiveCursor && <button type="button" disabled={busy} onClick={() => void loadArchives(archiveCursor)}>{t('Show more archived reviews')}</button>}
          </>}
      </>}
    </div>}
  </section>
}
