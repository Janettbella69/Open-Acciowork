import { useEffect, useId, useRef, useState } from 'react'
import {
  applyWorkspaceRestore, errorMessage, getWorkspaceRestore,
  listWorkspaceCheckpoints, previewWorkspaceRestore,
} from '../api/client'
import type { WorkspaceCheckpoint, WorkspaceRestore } from '../api/workspaceRecovery'
import { useT } from '../i18n'
import WorkspaceRetentionPanel from './WorkspaceRetentionPanel'
import './workspace-recovery.css'

type Props = { projectId: string; onChanged: () => void }
const pageSize = 100

function Recovery({ projectId, onChanged }: Props) {
  const t = useT()
  const regionId = useId()
  const [open, setOpen] = useState(false)
  const [points, setPoints] = useState<WorkspaceCheckpoint[]>([])
  const [view, setView] = useState<WorkspaceRestore | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [uncertain, setUncertain] = useState(false)
  const [page, setPage] = useState(0)
  const controller = useRef<AbortController | null>(null)
  const active = useRef(false)
  useEffect(() => {
    active.current = true
    controller.current = new AbortController()
    return () => { active.current = false; controller.current?.abort() }
  }, [])

  const load = async () => {
    if (busy) return
    setBusy(true); setError('')
    try {
      const result = await listWorkspaceCheckpoints(projectId, controller.current?.signal)
      if (!active.current) return
      if (result.some((item) => item.project_id !== projectId)) throw new Error('Recovery response did not match this project.')
      setPoints(result)
    } catch (reason) {
      if (active.current) setError(errorMessage(reason))
    } finally { if (active.current) setBusy(false) }
  }
  const preview = async (checkpointId: string) => {
    if (busy) return
    setBusy(true); setError(''); setConfirmed(false); setPage(0)
    try {
      const result = await previewWorkspaceRestore(projectId, checkpointId, controller.current?.signal)
      if (!active.current) return
      if (result.project_id !== projectId || result.checkpoint_id !== checkpointId || result.state !== 'preview') {
        throw new Error('Recovery response did not match this selection.')
      }
      setView(result); setUncertain(false)
    } catch (reason) {
      if (active.current) setError(errorMessage(reason))
    } finally { if (active.current) setBusy(false) }
  }
  const recover = async (checkOnly = false) => {
    if (!view || busy || (!checkOnly && !confirmed)) return
    setBusy(true); setError('')
    if (!checkOnly) setUncertain(true)
    try {
      const action = checkOnly ? getWorkspaceRestore : applyWorkspaceRestore
      const result = await action(projectId, view.id, controller.current?.signal)
      if (!active.current) return
      if (result.project_id !== projectId || result.id !== view.id || result.checkpoint_id !== view.checkpoint_id) {
        throw new Error('Recovery response did not match this selection.')
      }
      setView(result)
      setUncertain(result.state === 'applying')
      if (result.state === 'applied') onChanged()
    } catch (reason) {
      if (active.current) setError(errorMessage(reason))
    } finally { if (active.current) setBusy(false) }
  }
  const back = () => {
    setView(null); setConfirmed(false); setUncertain(false); void load()
  }
  return <section className="workspace-recovery" aria-label={t('Workspace recovery')}>
    <button type="button" className="workspace-recovery-toggle" aria-expanded={open} aria-controls={regionId}
      onClick={() => { setOpen(!open); if (!open && !view) void load() }}>
      {t('Workspace recovery')} <span aria-hidden="true">{open ? '−' : '+'}</span>
    </button>
    {open && <div id={regionId} className="workspace-recovery-body" aria-busy={busy}>
      <p>{t('Restore files to a saved starting point. Project evidence, decisions and conversation history stay available.')}</p>
      {error && <p role="alert">{t(error)}</p>}
      {busy && <p role="status">{t('Preparing recovery…')}</p>}
      {!view ? <>
        <button type="button" disabled={busy} onClick={() => void load()}>{t('Refresh recovery points')}</button>
        {!busy && !error && points.length === 0 && <p>{t('No recovery points yet. A workspace copy is saved before each new run.')}</p>}
        <ul className="recovery-points">{points.map((point) => <li key={point.id}>
          <div><strong>{t(point.kind === 'run_start' ? 'Before run' : 'Before file recovery')}</strong>
            <time dateTime={point.created_at}>{new Date(point.created_at).toLocaleString()}</time>
            <small>{t('{count} files · {size} bytes', { count: point.files, size: point.total_bytes.toLocaleString() })}</small>
            {point.run_id && <code>{point.run_id}</code>}
          </div>
          <button type="button" disabled={busy} onClick={() => void preview(point.id)}>{t('Review file changes')}</button>
        </li>)}</ul>
        <WorkspaceRetentionPanel projectId={projectId} onChanged={() => void load()} />
      </> : view.state === 'applied' ? <>
        <p role="status">{t('Files restored. The previous workspace is saved as a recovery point.')}</p>
        <button type="button" disabled={busy} onClick={back}>{t('Back to recovery points')}</button>
      </> : <>
        <h4>{t('Review workspace changes')}</h4>
        <p>{t('{count} changes · {removed} files removed from the current workspace', { count: view.changes.length, removed: view.removes_files })}</p>
        <p>{t('Current files will be saved before recovery. You can restore that copy later.')}</p>
        {view.changes.length === 0 ? <p>{t('Files already match this recovery point.')}</p> : <>
          <ul className="recovery-changes" aria-label={t('File changes')}>
            {view.changes.slice(page * pageSize, (page + 1) * pageSize).map((change) => <li key={change.path}>
              <span>{t({ restore: 'Restore', replace: 'Replace', remove: 'Remove' }[change.action])}</span>
              <code>{change.path}</code>
              <small>{t(change.before_kind === 'directory' || change.after_kind === 'directory' ? 'Includes directory change' : 'File')}</small>
            </li>)}
          </ul>
          {view.changes.length > pageSize && <div className="recovery-actions">
            <button type="button" disabled={page === 0} onClick={() => setPage(page - 1)}>{t('Previous changes')}</button>
            <span>{t('Page {page} of {total}', { page: page + 1, total: Math.ceil(view.changes.length / pageSize) })}</span>
            <button type="button" disabled={(page + 1) * pageSize >= view.changes.length} onClick={() => setPage(page + 1)}>{t('Next changes')}</button>
          </div>}
          <label className="recovery-confirm"><input type="checkbox" checked={confirmed} disabled={busy || uncertain}
            onChange={(event) => setConfirmed(event.target.checked)} />
            {t('I confirm all listed file replacements and removals.')}
          </label>
        </>}
        {uncertain && <p role="status">{t('Recovery result is not confirmed. Check its status or retry this same recovery.')}</p>}
        <div className="recovery-actions">
          <button type="button" disabled={busy || uncertain} onClick={back}>{t('Back to recovery points')}</button>
          {(uncertain || view.state === 'applying') && <button type="button" disabled={busy} onClick={() => void recover(true)}>{t('Check recovery result')}</button>}
          {view.changes.length > 0 && <button type="button" disabled={busy || !confirmed} onClick={() => void recover()}>{t('Confirm and restore files')}</button>}
        </div>
        <small>{t('This review expires at {time}. File changes after review require a new preview.', { time: new Date(view.expires_at).toLocaleTimeString() })}</small>
      </>}
    </div>}
  </section>
}

export default function WorkspaceRecoveryPanel(props: Props) {
  return <Recovery key={props.projectId} {...props} />
}
