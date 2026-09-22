import { useEffect, useId, useRef, useState } from 'react'
import { downloadProjectExport, errorMessage, previewProjectExport } from '../api/client'
import type { ProjectExportPreview } from '../api/projectExports'
import { useT } from '../i18n'
import './project-export.css'

type Props = { projectId: string; runId?: string }

function ExportForm({ projectId, runId }: Props) {
  const t = useT()
  const [preview, setPreview] = useState<ProjectExportPreview | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const active = useRef(true)
  const operation = useRef<AbortController | null>(null)
  useEffect(() => {
    active.current = true
    return () => { active.current = false; operation.current?.abort() }
  }, [])

  const review = async (conversation: boolean) => {
    if (operation.current || (conversation && !runId)) return
    const controller = new AbortController()
    operation.current = controller
    setBusy(true); setPreview(null); setError(''); setNotice('')
    try {
      const result = await previewProjectExport(projectId, conversation ? runId : undefined, controller.signal)
      if (!active.current || controller.signal.aborted) return
      if (result.project_id !== projectId || result.selected_run_id !== (conversation ? runId : null) || result.kind !== (conversation ? 'session' : 'project')) {
        throw new Error('Export preview did not match this selection.')
      }
      setPreview(result)
    } catch (failure) {
      if (active.current && !controller.signal.aborted) setError(errorMessage(failure))
    } finally {
      if (active.current) setBusy(false)
      operation.current = null
    }
  }
  const download = async () => {
    if (!preview || operation.current) return
    const controller = new AbortController()
    operation.current = controller
    setBusy(true); setError(''); setNotice('')
    try {
      const blob = await downloadProjectExport(projectId, preview, controller.signal)
      if (!active.current || controller.signal.aborted) return
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url; link.download = `ophira-${preview.kind}-${projectId}.zip`
      document.body.append(link); link.click(); link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
      setNotice('Data archive downloaded. Keep this business information private.')
      setPreview(null)
    } catch (failure) {
      if (active.current && !controller.signal.aborted) { setError(errorMessage(failure)); setPreview(null) }
    } finally {
      if (active.current) setBusy(false)
      operation.current = null
    }
  }

  return <div className="project-export-body">
    <p>{t('Download your project records and files, or the current conversation and its registered outputs. Finish the selected runs first.')}</p>
    <div className="project-export-actions">
      <button type="button" disabled={busy} onClick={() => void review(false)}>{t('Review project export')}</button>
      <button type="button" disabled={busy || !runId} onClick={() => void review(true)}>{t('Review conversation export')}</button>
    </div>
    {busy && <p role="status">{t('Preparing data export…')}</p>}
    {error && <p role="alert">{t(error)}</p>}
    {notice && <p role="status">{t(notice)}</p>}
    {preview && <div className="project-export-review">
      <strong>{t(preview.kind === 'project' ? 'Project archive' : 'Conversation archive')}</strong>
      <p>{t('{files} files · {runs} runs · {bytes} bytes before compression', { files: preview.file_count, runs: preview.run_ids.length, bytes: preview.total_bytes })}</p>
      <p>{t('The ZIP contains readable conversation text, structured records, a file manifest and available artifact versions. It is unencrypted and does not automatically restore an application.')}</p>
      {preview.warnings.length > 0 && <p role="alert">{t('{n} older artifact versions are unavailable; their IDs remain listed in the archive.', { n: preview.warnings.length })}</p>}
      <p>{t('Credentials, browser logins, internal SDK transcripts, shared agent memory and other projects are excluded. Export personal memories and credentials separately in Settings.')}</p>
      <button type="button" disabled={busy} onClick={() => void download()}>{t('Download reviewed ZIP')}</button>
    </div>}
  </div>
}

function ScopedPanel(props: Props) {
  const t = useT()
  const bodyId = useId()
  const [open, setOpen] = useState(false)
  return <section className="project-export">
    <button type="button" className="project-export-toggle" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(!open)}>
      <span>{t('Export project data')}</span><span aria-hidden="true">{open ? '−' : '+'}</span>
    </button>
    {open && <div id={bodyId}><ExportForm {...props} /></div>}
  </section>
}

export default function ProjectExportPanel(props: Props) {
  return <ScopedPanel key={`${props.projectId}:${props.runId ?? ''}`} {...props} />
}
