import { useEffect, useId, useRef, useState } from 'react'
import { errorMessage, getProjectDeletion } from '../api/client'
import { requireDeletionPreview, type DeletionDomain, type DeletionRetained, type ProjectDeletionPreview, type ProjectDeletionRequest } from '../api/projectDeletion'
import { useLanguage, useT } from '../i18n'
import ProjectExportPanel from './ProjectExportPanel'
import './project-deletion.css'

type Props = {
  projectId: string
  pending: boolean
  canReviewAgain: boolean
  onReviewAgain: () => void
  onSubmit: (projectId: string, request: ProjectDeletionRequest) => Promise<void>
  onAccessCheck: () => void
}

export function DeletionScope({ domains, retained }: { domains: DeletionDomain[]; retained: DeletionRetained[] }) {
  const t = useT()
  const language = useLanguage()
  return <>
    <h4>{t('Application content in this deletion')}</h4>
    <ul className="project-deletion-domains">{domains.map((domain) => <li key={domain.id}>
      <strong>{t(domain.label)}</strong><span>{t('{files} files · {bytes} bytes', {
        files: domain.files.toLocaleString(language), bytes: domain.bytes.toLocaleString(language),
      })}</span>
    </li>)}</ul>
    <h4>{t('Copies and records retained')}</h4>
    <p>{t('Deleting this project does not recall messages already sent or erase backups and downloaded copies.')}</p>
    <ul className="project-deletion-notes">{retained.map((item) => <li key={item.code}>{t(item.message)} {item.count > 0 && <span>{t('Count: {n}', { n: item.count.toLocaleString(language) })}</span>}</li>)}</ul>
  </>
}

function Review({ projectId, pending, onSubmit, onAccessCheck }: Props) {
  const t = useT()
  const language = useLanguage()
  const [preview, setPreview] = useState<ProjectDeletionPreview | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [name, setName] = useState('')
  const [reason, setReason] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const inFlight = useRef(false)
  const active = useRef<AbortController | null>(null)
  const accessCheck = useRef(onAccessCheck)
  useEffect(() => {
    const controller = new AbortController()
    active.current = controller
    queueMicrotask(() => {
      if (controller.signal.aborted) return
      void getProjectDeletion(projectId, controller.signal).then((result) => {
        if (controller.signal.aborted) return
        requireDeletionPreview(projectId, result)
        setPreview(result)
      }).catch((cause) => {
        if (controller.signal.aborted) return
        const status = cause && typeof cause === 'object' && 'status' in cause ? Number(cause.status) : null
        setError(status === 403 ? 'Only an authorized project owner or administrator can delete this project.' : errorMessage(cause))
        if (status === 403 || status === 404) accessCheck.current()
      }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    })
    return () => controller.abort()
  }, [projectId])
  const submit = () => {
    if (!preview || pending || inFlight.current || !preview.can_delete || preview.blockers.length
      || !confirmed || name !== preview.project_name || !reason.trim() || reason.trim().length > 1000) return
    inFlight.current = true
    const request = { operation_id: crypto.randomUUID(), fingerprint: preview.fingerprint,
      confirmation_name: name, reason: reason.trim() }
    setPreview(null); setName(''); setReason(''); setConfirmed(false)
    // The Workspace owns this request after confirmation, even if the review is closed.
    void onSubmit(projectId, request).finally(() => { inFlight.current = false })
  }
  return <div className="project-deletion-body">
    {loading && <p role="status">{t('Preparing deletion review…')}</p>}
    {error && <p role="alert">{t(error)}</p>}
    {preview && <>
      <h3>{preview.project_name}</h3><code>{preview.project_id}</code>
      <p>{t('Review the application content and retained copies before permanently deleting this project. Only an authorized owner or administrator can confirm.')}</p>
      <dl className="project-deletion-counts">{Object.entries(preview.counts).map(([key, value]) => <div key={key}>
        <dt>{t({ files: 'Files', bytes: 'Uncompressed size', runs: 'Runs', sessions: 'Sessions', memories: 'Memories', shops: 'Shops' }[key] ?? key)}</dt>
        <dd>{key === 'bytes' ? t('{n} bytes', { n: value.toLocaleString(language) }) : value.toLocaleString(language)}</dd>
      </div>)}</dl>
      <DeletionScope domains={preview.domains} retained={preview.retained} />
      {preview.blockers.length > 0 && <section role="alert"><h4>{t('Resolve these blockers before deleting')}</h4>
        <ul className="project-deletion-notes">{preview.blockers.map((item, index) => <li key={`${item.code}:${index}`}>{t(item.message)}</li>)}</ul>
      </section>}
      {!preview.can_delete && <p>{t('Deletion cannot start from this review. Resolve the listed blockers or ask an authorized project administrator.')}</p>}
      <p>{t('You can save an export before confirming deletion.')}</p>
      <ProjectExportPanel projectId={projectId} />
      <form onSubmit={(event) => { event.preventDefault(); submit() }}>
        <fieldset disabled={pending || !preview.can_delete || preview.blockers.length > 0}>
          <legend>{t('Confirm permanent project deletion')}</legend>
          <label>{t('Type the project name to confirm')}<input value={name} onChange={(event) => setName(event.target.value)} autoComplete="off" required /></label>
          <label>{t('Deletion reason')}<textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={1000} rows={3} required /></label>
          <small>{t('Only a hash of your deletion reason is retained. Do not include private content or credentials.')}</small>
          <label className="project-deletion-confirm"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />{t('I understand that submitted deletion is irreversible and retained copies are managed separately.')}</label>
          <button type="submit" className="project-deletion-danger" disabled={!confirmed || name !== preview.project_name || !reason.trim()}>{t('Permanently delete project')}</button>
        </fieldset>
      </form>
    </>}
    {!loading && !preview && !error && <p>{t('Check the deletion progress panel for the result of your request.')}</p>}
  </div>
}

function ScopedPanel(props: Props) {
  const t = useT()
  const region = useId()
  const [open, setOpen] = useState(false)
  const [revision, setRevision] = useState(0)
  return <section className="project-deletion">
    <button className="project-deletion-toggle" type="button" aria-expanded={open} aria-controls={region} onClick={() => setOpen(!open)}>
      <span>{t('Delete project')}</span><span aria-hidden="true">{open ? '−' : '+'}</span>
    </button>
    {open && <div id={region}>
      <div className="project-deletion-body project-deletion-actions"><button type="button" disabled={props.pending && !props.canReviewAgain} onClick={() => {
        if (props.canReviewAgain) props.onReviewAgain()
        setRevision((value) => value + 1)
      }}>{t(props.canReviewAgain ? 'Review deletion again' : 'Refresh deletion review')}</button></div>
      {props.pending ? <p className="project-deletion-body">{t('A deletion request is already being tracked. Check its progress before starting another.')}</p>
        : <Review key={revision} {...props} />}
    </div>}
  </section>
}

export default function ProjectDeletionPanel(props: Props) {
  return <ScopedPanel key={props.projectId} {...props} />
}
