import { useEffect, useRef, useState } from 'react'
import { deleteProject, errorMessage, previewProjectDeletion } from '../api/client'
import type { ProjectDeletionPreview } from '../api/projectErasure'
import { useT } from '../i18n'
import './project-export.css'

type Props = { projectId: string; disabled?: boolean; onDeleted: (projectId: string) => void }

function ProjectDeletionForm({ projectId, disabled, onDeleted }: Props) {
  const t = useT()
  const [preview, setPreview] = useState<ProjectDeletionPreview | null>(null)
  const [name, setName] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [uncertain, setUncertain] = useState(false)
  const pending = useRef(false)
  const active = useRef(true)
  const controller = useRef<AbortController | null>(null)
  useEffect(() => {
    active.current = true
    return () => { active.current = false; controller.current?.abort() }
  }, [])
  const review = async () => {
    if (pending.current || disabled) return
    pending.current = true
    controller.current = new AbortController()
    setBusy(true); setError(''); setPreview(null); setConfirmed(false); setName(''); setUncertain(false)
    try {
      const result = await previewProjectDeletion(projectId, controller.current.signal)
      if (!active.current) return
      if (result.kind !== 'project' || result.project_id !== projectId) throw new Error('Deletion preview did not match this project.')
      setPreview(result)
    } catch (failure) {
      if (active.current) setError(errorMessage(failure))
    } finally {
      pending.current = false
      if (active.current) setBusy(false)
    }
  }
  const remove = async () => {
    if (pending.current || !preview || !confirmed || name !== preview.project_name || disabled) return
    pending.current = true
    setBusy(true); setError('')
    try {
      const result = await deleteProject(projectId, preview, name)
      if (result.status !== 'deleted' || result.project_id !== projectId || result.request_id !== preview.request_id) throw new Error('Deletion result could not be verified.')
      onDeleted(projectId)
      if (active.current) { setPreview(null); setConfirmed(false); setName(''); setUncertain(false) }
    } catch (failure) {
      if (active.current) { setError(errorMessage(failure)); setUncertain(true) }
    } finally {
      pending.current = false
      if (active.current) setBusy(false)
    }
  }
  return <section className="project-export project-deletion" aria-label={t('Delete project')}>
    <div className="project-export-body">
      <button type="button" disabled={busy || disabled} onClick={() => void review()}>{t('Review project deletion')}</button>
      {busy && <p role="status">{t('Checking project deletion…')}</p>}
      {error && <p role="alert">{t(error)}</p>}
      {uncertain && <p role="status">{t('The request may already be committed. Retry the same confirmation to check its result. A disconnected request does not cancel deletion.')}</p>}
      {preview && <div className="project-export-review">
        <strong>{t('Permanently delete {name}', { name: preview.project_name })}</strong>
        <p>{t('{runs} runs · {files} stored records/files · {memories} source memories', { runs: preview.run_ids.length, files: preview.file_count, memories: preview.memory_count })}</p>
        <p>{t('{schedules} schedules · {intents} standing intents · {shops} shop logins · {devices} device scopes', { schedules: preview.schedules, intents: preview.intents, shops: preview.shops, devices: preview.devices })}</p>
        <p>{t('Deletes this project, all conversations, saved files and versions, evidence, decisions, comments, templates, project memories and attributable shared context. Related automation and project access are removed.')}</p>
        <p>{t('Other projects and independent copies, published shared Skills, global credentials, handwritten shared Agent files, existing backups and external services remain. Unattributed runtime and OS caches are outside this deletion.')}</p>
        {preview.independent_copies > 0 && <p>{t('{n} independently copied projects will remain usable.', { n: preview.independent_copies })}</p>}
        <label className="project-deletion-name">{t('Type the project name to confirm')}<input value={name} disabled={busy || disabled} autoComplete="off" spellCheck={false} onChange={(event) => setName(event.target.value)} /></label>
        <label><input type="checkbox" checked={confirmed} disabled={busy || disabled} onChange={(event) => setConfirmed(event.target.checked)} /> {t('I understand this permanently deletes the project and its saved outputs.')}</label>
        <div className="project-export-actions">
          <button type="button" className="project-deletion-confirm" disabled={busy || disabled || !confirmed || name !== preview.project_name} onClick={() => void remove()}>{t(uncertain ? 'Retry reviewed deletion' : 'Permanently delete project')}</button>
          <button type="button" disabled={busy} onClick={() => { setPreview(null); setConfirmed(false); setName('') }}>{t('Close review')}</button>
        </div>
      </div>}
    </div>
  </section>
}

export default function ProjectDeletionPanel(props: Props) {
  return <ProjectDeletionForm key={props.projectId} {...props} />
}
