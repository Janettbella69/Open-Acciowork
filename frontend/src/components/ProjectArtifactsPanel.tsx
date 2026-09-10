import { useEffect, useId, useRef, useState } from 'react'
import { artifactDownloadUrl, errorMessage, getArtifactText, saveArtifactText } from '../api/client'
import type { ArtifactTextView } from '../api/artifacts'
import type { Artifact } from '../api/types'
import { useT } from '../i18n'
import './project-artifacts.css'

const textSuffixes = new Set(['txt', 'md', 'markdown', 'csv', 'tsv', 'json', 'jsonl', 'yaml', 'yml', 'xml', 'html', 'htm', 'rst'])

function textPreviewAvailable(item: Artifact) {
  return item.size_bytes <= 512 * 1024 && textSuffixes.has(item.path.split('.').at(-1)?.toLowerCase() ?? '')
    && !item.path.split('/').some((part) => part.startsWith('.'))
}

function Editor({ projectId, artifactId, onClose, onSaved }: {
  projectId: string; artifactId: string; onClose: () => void; onSaved: (item: Artifact) => void
}) {
  const t = useT()
  const [view, setView] = useState<ArtifactTextView | null>(null)
  const [draft, setDraft] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState(false)
  const [discard, setDiscard] = useState(false)
  const requestRef = useRef<{ content: string; id: string } | null>(null)
  const controllerRef = useRef<AbortController | null>(null)
  const activeRef = useRef(false)
  useEffect(() => {
    const controller = new AbortController()
    controllerRef.current = controller
    activeRef.current = true
    void getArtifactText(projectId, artifactId, controller.signal).then((data) => {
      if (!activeRef.current) return
      if (data.artifact.project_id !== projectId || data.artifact.id !== artifactId) {
        setError('Artifact response did not match this selection.')
        return
      }
      setView(data)
      setDraft(data.content)
    }).catch((reason: unknown) => {
      if (activeRef.current) setError(errorMessage(reason))
    })
    return () => { activeRef.current = false; controller.abort() }
  }, [projectId, artifactId])
  const dirty = view !== null && view.content !== draft
  const save = async () => {
    if (!view?.editable || busy || !dirty) return
    setBusy(true)
    setError('')
    // Preserve the key across network retries of this exact draft.
    if (requestRef.current?.content !== draft) requestRef.current = { content: draft, id: crypto.randomUUID() }
    try {
      const result = await saveArtifactText(projectId, artifactId, {
        content: draft, expected_checksum: view.artifact.checksum, request_id: requestRef.current.id,
      }, controllerRef.current?.signal)
      if (!activeRef.current) return
      if (result.project_id !== projectId || result.path !== view.artifact.path
        || result.parent_artifact_id !== artifactId) throw new Error('Artifact response did not match this selection.')
      onSaved(result)
    } catch (reason: unknown) {
      if (activeRef.current) setError(errorMessage(reason))
    } finally {
      if (activeRef.current) setBusy(false)
    }
  }
  const downloadDraft = () => {
    const url = URL.createObjectURL(new Blob([draft], { type: 'text/plain;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `${view?.artifact.name ?? 'artifact'}.draft.txt`
    link.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  }
  return <section className="artifact-editor" aria-label={t('Artifact text editor')}>
    <div className="artifact-editor-actions">
      <button type="button" disabled={busy} onClick={() => dirty ? setDiscard(true) : onClose()}>{t('Back to files')}</button>
      {view && <a href={artifactDownloadUrl(projectId, artifactId)}>{t('Download saved version')}</a>}
    </div>
    {discard && <div className="artifact-edit-notice" role="alert">
      <p>{t('Discard this unsaved draft? Saved versions will be kept.')}</p>
      <button type="button" onClick={onClose}>{t('Discard draft')}</button>
      <button type="button" onClick={() => setDiscard(false)}>{t('Keep editing')}</button>
    </div>}
    {error && <p role="alert">{t(error)}{dirty && ` ${t('Your draft is kept here. Save a copy before leaving.')}`}</p>}
    {!view && !error && <p role="status">{t('Loading saved artifact…')}</p>}
    {view && <>
      <h3>{view.artifact.name} · {t('Version {n}', { n: view.artifact.version })}</h3>
      <code>{view.artifact.path}</code>
      {view.edit_notice && <p className="artifact-edit-notice">{t(view.edit_notice)}</p>}
      {view.editable ? <label className="artifact-text-label">{t('File contents')}
        <textarea spellCheck={false} value={draft} disabled={busy} maxLength={512 * 1024}
          onChange={(event) => { setDraft(event.target.value); setPreview(false) }} />
      </label> : <pre>{view.content}</pre>}
      {dirty && <div className="artifact-editor-actions">
        <button type="button" disabled={busy} onClick={() => setPreview(!preview)} aria-expanded={preview}>{t('Review changes')}</button>
        <button type="button" disabled={busy} onClick={downloadDraft}>{t('Download draft')}</button>
      </div>}
      {preview && dirty && <div className="artifact-change-preview">
        <section><h4>{t('Saved version')}</h4><pre>{view.content}</pre></section>
        <section><h4>{t('Your changes')}</h4><pre>{draft}</pre></section>
      </div>}
      {view.editable && <div className="artifact-editor-actions">
        <button type="button" disabled={!dirty || busy || !preview} onClick={() => void save()}>{t(busy ? 'Saving version…' : 'Save new version')}</button>
        <small>{t('Review changes before saving. Previous versions remain downloadable.')}</small>
      </div>}
    </>}
  </section>
}

function ScopedArtifacts({ projectId, artifacts, onChanged }: {
  projectId: string; artifacts: Artifact[]; onChanged: () => void
}) {
  const t = useT()
  const bodyId = useId()
  const [open, setOpen] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [saved, setSaved] = useState<Artifact[]>([])
  const [notice, setNotice] = useState('')
  const [query, setQuery] = useState('')
  const byId = new Map(artifacts.filter((item) => item.project_id === projectId).map((item) => [item.id, item]))
  for (const item of saved) if (!byId.has(item.id)) byId.set(item.id, item)
  const groups = new Map<string, Artifact[]>()
  for (const item of byId.values()) groups.set(item.path, [...(groups.get(item.path) ?? []), item])
  const files = [...groups.values()].map((items) => items.sort((a, b) => b.version - a.version))
    .sort((a, b) => b[0].created_at.localeCompare(a[0].created_at))
    .filter((items) => items[0].path.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
  return <section className="project-artifacts">
    <button className="project-artifacts-toggle" type="button" disabled={selected !== null}
      aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(!open)}>
      <span>{t('Project files')} · {groups.size}</span><span aria-hidden="true">{open ? '−' : '+'}</span>
    </button>
    {open && <div id={bodyId} className="project-artifacts-body">
      {notice && <p role="status">{notice}</p>}
      {selected ? <Editor key={selected} projectId={projectId} artifactId={selected} onClose={() => setSelected(null)}
        onSaved={(item) => {
          setSaved((current) => [...current.filter((row) => row.id !== item.id), item])
          setSelected(null)
          setNotice(t('Version {n} saved. Previous versions are available in history.', { n: item.version }))
          onChanged()
        }} /> : <>
        <p>{t('Download any saved version. Preview and edit text files with a version history.')}</p>
        <label>{t('Find a file')}<input type="search" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
        {!files.length && <p>{t('No saved files match this view.')}</p>}
        {files.map((versions) => {
          const latest = versions[0]
          return <article className="project-artifact-file" key={latest.path}>
            <header><strong>{latest.name}</strong><span>{t('Version {n}', { n: latest.version })}</span></header>
            <code>{latest.path}</code>
            <div className="artifact-editor-actions">
              <a href={artifactDownloadUrl(projectId, latest.id)}>{t('Download')}</a>
              {textPreviewAvailable(latest) && <button type="button" onClick={() => { setSelected(latest.id); setNotice('') }}>{t('Preview and edit')}</button>}
            </div>
            <details><summary>{t('Version history')} · {versions.length}</summary>
              <ul>{versions.map((item) => <li key={item.id}>
                <span>{t('Version {n}', { n: item.version })} · {item.created_at}</span>
                {item.created_by && <small>{t('Edited by {name}', { name: item.created_by })}</small>}
                {item.run_id && <small>{t('Run')}: <code>{item.run_id}</code></small>}
                {item.source_ids.length > 0 && <small>{t('Source records')}: <code>{item.source_ids.join(', ')}</code></small>}
                {item.content_storage !== 'snapshot' && <small>{t('Legacy version — availability is checked when opened.')}</small>}
                <div className="artifact-editor-actions">
                  <a href={artifactDownloadUrl(projectId, item.id)}>{t('Download version {n}', { n: item.version })}</a>
                  {textPreviewAvailable(item) && <button type="button" onClick={() => { setSelected(item.id); setNotice('') }}>{t('Preview version {n}', { n: item.version })}</button>}
                </div>
                <small>SHA-256: <code>{item.checksum}</code></small>
              </li>)}</ul>
            </details>
          </article>
        })}
      </>}
    </div>}
  </section>
}

export default function ProjectArtifactsPanel(props: { projectId: string; artifacts: Artifact[]; onChanged: () => void }) {
  return <ScopedArtifacts key={props.projectId} {...props} />
}
