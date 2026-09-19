import { useEffect, useId, useRef, useState } from 'react'
import { downloadProjectExport, errorMessage, getProjectExport } from '../api/client'
import { projectExportFormat, type ProjectExportPreview } from '../api/projectExport'
import { useLanguage, useT } from '../i18n'
import './project-export.css'

const pageSize = 50
const categoryLabels: Record<string, string> = {
  project: 'Project records', workspace: 'Workspace files', conversations: 'Conversations',
  artifacts: 'Artifact versions', sources: 'Source records', memory: 'Project memories',
  access: 'Access records', collaboration: 'Project discussions', automation: 'Automation records',
  notifications: 'Notifications', feedback: 'Feedback', commercial: 'Commercial actions',
  templates: 'Project templates', 'skill-proposals': 'Skill proposals',
}

function responseStatus(reason: unknown): number | null {
  return reason && typeof reason === 'object' && 'status' in reason ? Number(reason.status) : null
}

function ExportReview({ projectId, onRefresh }: { projectId: string; onRefresh: () => void }) {
  const t = useT()
  const language = useLanguage()
  const [preview, setPreview] = useState<ProjectExportPreview | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [downloading, setDownloading] = useState(false)
  const [page, setPage] = useState(0)
  const activeRef = useRef(false)
  const controllerRef = useRef<AbortController | null>(null)
  const downloadPendingRef = useRef(false)
  const urlsRef = useRef(new Map<string, number>())

  useEffect(() => {
    const controller = new AbortController()
    controllerRef.current = controller
    activeRef.current = true
    const urls = urlsRef.current
    // Let StrictMode's mount cleanup cancel its first effect before starting a server capture.
    queueMicrotask(() => {
      if (controller.signal.aborted) return
      void getProjectExport(projectId, controller.signal).then((result) => {
        if (!activeRef.current || controller.signal.aborted) return
        if (result.project_id !== projectId || result.format !== projectExportFormat
          || !/^[0-9a-f]{64}$/.test(result.fingerprint)) {
          throw new Error('The export preview did not match this project.')
        }
        setPreview(result)
      }).catch((reason: unknown) => {
        if (activeRef.current && !controller.signal.aborted) setError(errorMessage(reason))
      }).finally(() => {
        if (activeRef.current && !controller.signal.aborted) setLoading(false)
      })
    })
    return () => {
      activeRef.current = false
      controller.abort()
      for (const [url, timer] of urls) { window.clearTimeout(timer); URL.revokeObjectURL(url) }
      urls.clear()
    }
  }, [projectId])

  const download = async () => {
    if (!preview || downloadPendingRef.current || !controllerRef.current) return
    downloadPendingRef.current = true
    setDownloading(true); setError(''); setNotice('')
    const controller = controllerRef.current
    try {
      const archive = await downloadProjectExport(projectId, preview.fingerprint, controller.signal)
      if (!activeRef.current || controller.signal.aborted) return
      const url = URL.createObjectURL(archive)
      const link = document.createElement('a')
      link.href = url
      link.download = `${projectId.replace(/[^A-Za-z0-9_-]/g, '_')}-export.zip`
      link.hidden = true
      const timer = window.setTimeout(() => { URL.revokeObjectURL(url); urlsRef.current.delete(url) }, 1000)
      urlsRef.current.set(url, timer)
      document.body.appendChild(link)
      try { link.click() } finally { link.remove() }
      setNotice('Archive sent to your browser. Check your browser downloads to confirm it was saved.')
    } catch (reason: unknown) {
      if (!activeRef.current || controller.signal.aborted) return
      const status = responseStatus(reason)
      if (status === 403 || status === 404 || status === 409) setPreview(null)
      setError(errorMessage(reason))
      if (status === 409) setNotice('Review a fresh export before downloading.')
    } finally {
      downloadPendingRef.current = false
      if (activeRef.current && !controller.signal.aborted) setDownloading(false)
    }
  }

  const rows = preview ? [
    ...preview.entries.map((entry) => ({ path: entry.path, entry })),
    ...preview.directories.map((path) => ({ path, entry: null })),
  ] : []
  const totalPages = Math.max(1, Math.ceil(rows.length / pageSize))
  const formatNumber = (value: number) => value.toLocaleString(language)
  return <div className="project-export-review">
    <p>{t('Review a ZIP archive of this project’s saved records and files before downloading.')}</p>
    {loading && <p role="status">{t('Preparing export preview…')}</p>}
    {error && <p role="alert">{t(error)}</p>}
    {notice && <p role="status">{t(notice)}</p>}
    <div className="project-export-actions">
      <button type="button" disabled={downloading} onClick={onRefresh}>{t('Refresh export preview')}</button>
      <button type="button" disabled={!preview || downloading} onClick={() => void download()}>
        {t(downloading ? 'Preparing ZIP download…' : 'Download reviewed ZIP')}
      </button>
    </div>
    {preview && <>
      <dl className="project-export-summary">
        <div><dt>{t('Project data files')}</dt><dd>{formatNumber(preview.file_count)}</dd></div>
        <div><dt>{t('Uncompressed size')}</dt><dd>{t('{n} bytes', { n: formatNumber(preview.total_bytes) })}</dd></div>
        <div><dt>{t('Folders')}</dt><dd>{formatNumber(preview.directories.length)}</dd></div>
      </dl>
      <p>{t('File counts and sizes describe project data files. The ZIP also includes an integrity manifest.')}</p>
      <ul className="project-export-notes" aria-label={t('Export contents and limits')}>
        {preview.notes.map((note, index) => <li key={index}>{t(note)}</li>)}
      </ul>
      <details className="project-export-provenance"><summary>{t('Export review details')}</summary>
        <dl><dt>{t('Project')}</dt><dd>{preview.project_id}</dd>
          <dt>{t('Prepared for')}</dt><dd>{preview.actor_id}</dd>
          <dt>{t('Revision')}</dt><dd>{preview.revision}</dd>
          <dt>{t('Archive fingerprint')}</dt><dd><code>{preview.fingerprint}</code></dd>
        </dl>
      </details>
      <ul className="project-export-files" aria-label={t('Reviewed export files')}>
        {rows.slice(page * pageSize, (page + 1) * pageSize).map(({ path, entry }) => <li key={`${entry ? 'file' : 'directory'}:${path}`}>
          <code>{path}</code>
          <small>{entry ? `${t(categoryLabels[entry.category] ?? entry.category)} · ${t('{n} bytes', { n: formatNumber(entry.size_bytes) })}` : t('Folder')}</small>
          {entry && <details><summary>SHA-256</summary><code>{entry.sha256}</code></details>}
        </li>)}
      </ul>
      <nav className="project-export-actions" aria-label={t('Export file pages')}>
        <button type="button" disabled={page === 0} onClick={() => setPage(page - 1)}>{t('Previous page')}</button>
        <span>{t('Page {page} of {total}', { page: page + 1, total: totalPages })}</span>
        <button type="button" disabled={page + 1 >= totalPages} onClick={() => setPage(page + 1)}>{t('Next page')}</button>
      </nav>
    </>}
  </div>
}

function ScopedExportPanel({ projectId }: { projectId: string }) {
  const t = useT()
  const bodyId = useId()
  const [open, setOpen] = useState(false)
  const [revision, setRevision] = useState(0)
  return <section className="project-export">
    <button className="project-export-toggle" type="button" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(!open)}>
      <span>{t('Project export')}</span><span aria-hidden="true">{open ? '−' : '+'}</span>
    </button>
    {open && <div id={bodyId} className="project-export-body">
      <ExportReview key={revision} projectId={projectId} onRefresh={() => setRevision(revision + 1)} />
    </div>}
  </section>
}

export default function ProjectExportPanel({ projectId }: { projectId: string }) {
  return <ScopedExportPanel key={projectId} projectId={projectId} />
}
