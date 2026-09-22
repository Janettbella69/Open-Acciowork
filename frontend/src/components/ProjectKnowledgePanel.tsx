import { useEffect, useId, useState } from 'react'
import { apiBaseUrl, errorMessage, getProjectKnowledge } from '../api/client'
import type { KnowledgeSearch, ReferenceDocument, ReferenceMatch, ReferenceStatus } from '../api/knowledge'
import { useT } from '../i18n'
import './project-knowledge.css'

const statusLabels: Record<ReferenceStatus, string> = {
  indexed: 'Searchable', unsupported: 'Format not indexed', unavailable: 'File unavailable',
  changed: 'File differs from registered version', too_large: 'File exceeds search size limit',
  invalid_text: 'Text could not be decoded', budget_exceeded: 'Outside this search reading limit',
  invalid_document: 'Office file could not be read safely',
}

function Location({ match }: { match: ReferenceMatch }) {
  const t = useT()
  const location = match.source_location
  let label = ''
  if (location?.kind === 'paragraph') label = t('Body text paragraph {n}', { n: location.paragraph ?? '' })
  if (location?.kind === 'cell') label = t('Sheet {sheet} · cell {cell}', { sheet: location.sheet ?? '', cell: location.cell ?? '' })
  if (location?.kind === 'slide') label = t('Slide {n}', { n: location.slide ?? '' })
  if (location?.kind === 'slide_notes') label = t('Speaker notes · slide {n}', { n: location.slide ?? '' })
  return <small>{label && <>{label}<br /></>}{t(location ? 'Extracted text lines {start}–{end} · column {column}' : 'Lines {start}–{end} · column {column}', { start: match.start_line, end: match.end_line, column: match.start_column })}</small>
}

function Source({ document, projectId }: { document: ReferenceDocument; projectId: string }) {
  const t = useT()
  return <div className="project-knowledge-source">
    <strong>{document.name}</strong><span>{t('Version {n}', { n: document.version })}</span>
    <code>{document.path}</code>
    {document.status === 'indexed' && <a href={`${apiBaseUrl}/api/projects/${encodeURIComponent(projectId)}/knowledge/${encodeURIComponent(document.artifact_id)}/download`}>{t('Download')}</a>}
  </div>
}

function SearchResult({ projectId, query }: { projectId: string; query: string }) {
  const t = useT()
  const [state, setState] = useState<{ data: KnowledgeSearch | null; error: string }>({ data: null, error: '' })
  useEffect(() => {
    const controller = new AbortController()
    let active = true
    void getProjectKnowledge(projectId, query, controller.signal).then((data) => {
      if (!active) return
      if (data.project_id !== projectId) {
        setState({ data: null, error: 'Reference response did not match this project.' })
      } else setState({ data, error: '' })
    }).catch((error: unknown) => {
      if (active) setState({ data: null, error: errorMessage(error) })
    })
    return () => { active = false; controller.abort() }
  }, [projectId, query])
  if (state.error) return <p role="alert">{t(state.error)}</p>
  if (!state.data) return <p role="status">{t('Searching project references…')}</p>
  const data = state.data
  const skipped = data.documents.filter((document) => document.status !== 'indexed')
  return <div aria-live="polite">
    <p>{t('{indexed} of {total} files searchable', { indexed: data.indexed_documents, total: data.total_documents })}</p>
    {data.omitted_documents > 0 && <p>{t('{n} older files are outside this search limit.', { n: data.omitted_documents })}</p>}
    {data.total_documents === 0 && <p>{t('Attach reference files to a project message to make them searchable.')}</p>}
    {query && <p>{t('{shown} of {total} matching files', { shown: data.matches.length, total: data.matching_documents })}</p>}
    {query && data.matches.length === 0 && <p>{t('No matching passages in the searchable files. Try fewer terms and check file coverage below.')}</p>}
    {data.matches.map((match) => {
      const document = data.documents.find((item) => item.artifact_id === match.artifact_id && item.status === 'indexed')
      if (!document) return null
      return <article className="project-knowledge-match" key={match.artifact_id}>
        <Source document={document} projectId={projectId} />
        {document.extraction_notice && <div className="project-knowledge-notice"><strong>{t('Extraction notes — check before citing')}</strong><pre>{t(document.extraction_notice)}</pre></div>}
        <Location match={match} />
        <pre>{match.excerpt}</pre>
        {match.excerpt_truncated && <small>{t('Excerpt only. Download the source for full context.')}</small>}
        <details><summary>{t('Reference provenance')}</summary>
          <dl><dt>{t('Artifact')}</dt><dd>{document.artifact_id}</dd>
            <dt>{t('Registered')}</dt><dd>{document.created_at}</dd>
            <dt>SHA-256</dt><dd>{document.checksum}</dd>
            {document.run_id && <><dt>{t('Run')}</dt><dd>{document.run_id}</dd></>}
            {document.source_ids.length > 0 && <><dt>{t('Source records')}</dt><dd>{document.source_ids.join(', ')}</dd></>}
          </dl>
        </details>
      </article>
    })}
    <details open={!query || skipped.length > 0}>
      <summary>{t('File coverage')}</summary>
      <ul className="project-knowledge-files">{data.documents.map((document) => <li key={document.artifact_id}>
        <Source document={document} projectId={projectId} />
        <small>{t(statusLabels[document.status])}</small>
        {document.extraction_notice && !data.matches.some((match) => match.artifact_id === document.artifact_id) && <small>{t(document.extraction_notice)}</small>}
      </li>)}</ul>
    </details>
  </div>
}

function ScopedPanel({ projectId, revision }: { projectId: string; revision: string }) {
  const t = useT()
  const bodyId = useId()
  const [open, setOpen] = useState(false)
  const [input, setInput] = useState('')
  const [query, setQuery] = useState('')
  const [refresh, setRefresh] = useState(0)
  return <section className="project-knowledge">
    <button className="project-knowledge-toggle" type="button" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(!open)}>
      <span>{t('Project knowledge')}</span><span aria-hidden="true">{open ? '−' : '+'}</span>
    </button>
    {open && <div id={bodyId} className="project-knowledge-body">
      <p>{t('Search current project files by words or phrases. All terms must match. References need verification before becoming evidence.')}</p>
      <form className="project-knowledge-search" onSubmit={(event) => { event.preventDefault(); setQuery(input.trim()); setRefresh(refresh + 1) }}>
        <label>{t('Search references')}<input type="search" value={input} maxLength={200} onChange={(event) => setInput(event.target.value)} /></label>
        <button type="submit">{t('Search')}</button>
        <button type="button" onClick={() => setRefresh(refresh + 1)}>{t('Refresh references')}</button>
      </form>
      <SearchResult key={`${query}:${refresh}:${revision}`} projectId={projectId} query={query} />
    </div>}
  </section>
}

export default function ProjectKnowledgePanel({ projectId, revision = '' }: { projectId: string; revision?: string }) {
  return <ScopedPanel key={projectId} projectId={projectId} revision={revision} />
}
