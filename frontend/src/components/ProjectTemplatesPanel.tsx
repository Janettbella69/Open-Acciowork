import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { archiveProjectTemplate, getTemplateVersion, listProjectTemplates, templateReferenceUrl } from '../api/client'
import { requireCollaborationScope } from '../api/collaboration'
import { requireTemplateDetail, type ProjectTemplate, type TemplateDetail, type TemplateLibrary } from '../api/projectTemplates'
import type { OpportunityProject } from '../api/types'
import { useT } from '../i18n'
import ProjectTemplateEditor, { TemplateSeedReview } from './ProjectTemplateEditor'
import ProjectTemplateHistory from './ProjectTemplateHistory'
import { useCollaborationRequest, useCommentRequestKey } from './useCollaborationRequest'
import './project-templates.css'

type Props = { project: OpportunityProject; onCreated: (project: OpportunityProject) => void }
type VersionProps = Props & { template: ProjectTemplate; canCreate: boolean; onUnavailable: () => void; onChanged: () => void }

function TemplateVersions({ project, template, canCreate, onCreated, onUnavailable, onChanged }: VersionProps) {
  const t = useT()
  const [version, setVersion] = useState(template.latest_version)
  const [detail, setDetail] = useState<TemplateDetail | null>(null)
  const [mode, setMode] = useState<'save' | 'apply' | 'archive' | null>(null)
  const [history, setHistory] = useState(false)
  const { run, busy, error } = useCollaborationRequest(onUnavailable)
  const keys = useCommentRequestKey()
  const refresh = useCallback(() => run(async (signal) => {
    const value = await getTemplateVersion(project.id, template.id, version, signal)
    requireTemplateDetail(project.id, template.id, version, value)
    return value
  }, (value) => { setDetail(value); setMode(null) }), [project.id, template.id, version, run])
  useEffect(() => { void refresh() }, [refresh])
  function archive() {
    if (!detail) return
    const data = { expected_revision: detail.template.revision, archived: !detail.template.archived }
    void run(async (signal) => {
      const value = await archiveProjectTemplate(project.id, template.id, { ...data, request_id: keys.forPayload(data) }, signal)
      requireCollaborationScope(project.id, value)
      if (value.id !== template.id) throw new Error('Template response did not match the reviewed version.')
      return value
    }, (value) => { keys.clear(); setDetail({ ...detail, template: value }); setMode(null); onChanged() })
  }
  return <section className="template-detail" aria-label={t('Template versions')}>
    {error && <p role="alert">{t(error)}</p>}
    <div className="template-actions">
      <label>{t('Saved template version')}<select disabled={busy || mode !== null} value={version} onChange={(e) => { setDetail(null); setVersion(Number(e.target.value)) }}>{template.versions.map((row) => <option key={row.version} value={row.version}>v{row.version} · {row.title}</option>)}</select></label>
      <button type="button" disabled={busy} onClick={() => void refresh()}>{t('Reload version and discard edits')}</button>
    </div>
    {detail && <>
      <h4>{detail.version.title} · v{detail.version.version}</h4><p>{detail.version.description}</p>
      <small>{detail.version.created_by} · {new Date(detail.version.created_at).toLocaleString()} · SHA-256 {detail.version.content_hash}</small>
      {!mode && <>
        <TemplateSeedReview seed={detail.version.seed} references={detail.version.references} />
        <ul>{detail.version.references.map((row) => <li key={row.artifact_id}><a href={templateReferenceUrl(project.id, template.id, version, row.artifact_id)} download rel="noreferrer">{t('Download reference')} · {row.name} · v{row.version}</a></li>)}</ul>
        <div className="template-actions">
          {template.can_apply && <button type="button" disabled={busy} onClick={() => setMode('apply')}>{t('Create project from this version')}</button>}
          {template.can_manage && <>
            {!template.archived && <button type="button" disabled={busy} onClick={() => setMode('save')}>{t('Save a new template version')}</button>}
            <button type="button" disabled={busy} onClick={() => setMode('archive')}>{t(template.archived ? 'Restore template' : 'Archive template')}</button>
          </>}
        </div>
      </>}
      {mode === 'archive' && <div className="template-review"><p>{t(detail.template.archived ? 'Restore this template so it can create new projects again?' : 'Archive this template? Existing projects and saved versions remain available.')}</p><p>{detail.template.title} · {t('Revision')} {detail.template.revision}</p><div className="template-actions"><button type="button" disabled={busy} onClick={archive}>{t(detail.template.archived ? 'Confirm restore template' : 'Confirm archive template')}</button><button type="button" disabled={busy} onClick={() => setMode(null)}>{t('Cancel')}</button></div></div>}
      {(mode === 'save' || mode === 'apply') && !template.archived && <ProjectTemplateEditor key={`${mode}:${detail.version.version}:${detail.template.revision}`} project={project} detail={detail} mode={mode} onUnavailable={onUnavailable} onCancel={() => setMode(null)} onCreated={onCreated} onSaved={(value) => { setMode(null); setDetail(null); setVersion(value.latest_version); onChanged() }} />}
    </>}
    {canCreate && <><button type="button" aria-expanded={history} onClick={() => setHistory(!history)}>{t('Your project creations')}</button>{history && <ProjectTemplateHistory projectId={project.id} templateId={template.id} onUnavailable={onUnavailable} onCreated={onCreated} />}</>}
  </section>
}

function TemplateLibraryView({ project, onCreated }: Props) {
  const t = useT()
  const [library, setLibrary] = useState<TemplateLibrary | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [unavailable, setUnavailable] = useState(false)
  const blocked = useRef(false)
  const generation = useRef(0)
  const onUnavailable = useCallback(() => {
    generation.current += 1; blocked.current = true
    setLibrary(null); setSelected(null); setCreating(false); setUnavailable(true)
  }, [])
  const { run, busy, error } = useCollaborationRequest(onUnavailable)
  const refresh = useCallback(() => {
    const observed = generation.current
    return run(async (signal) => {
      const value = await listProjectTemplates(project.id, signal)
      requireCollaborationScope(project.id, value, ...value.items)
      return value
    }, (value) => { if (observed === generation.current && !blocked.current) { setLibrary(value); setUnavailable(false) } })
  }, [project.id, run])
  useEffect(() => {
    void refresh()
    const check = () => { if (!document.hidden && !blocked.current) void refresh() }
    const timer = window.setInterval(check, 15_000)
    window.addEventListener('focus', check)
    return () => { window.clearInterval(timer); window.removeEventListener('focus', check) }
  }, [refresh])
  const item = library?.items.find((row) => row.id === selected)
  return <div className="project-templates-body">
    <p>{t('Turn reusable project instructions and chosen file versions into a starting point for your next project.')}</p>
    {error && <p role="alert">{t(error)}</p>}
    {unavailable && <p role="alert">{t('Project template access changed. Refresh after signing in with your current account and role.')}</p>}
    <button type="button" disabled={busy} onClick={() => { blocked.current = false; void refresh() }}>{t(busy ? 'Loading templates…' : 'Refresh templates')}</button>
    {library && <div key={`${library.actor_id}:${library.can_save}:${library.can_create_project}`}>
      {!library.can_create_project && <p>{t('A project owner can create a new project from these templates.')}</p>}
      {library.can_save && <button type="button" onClick={() => { setCreating(true); setSelected(null) }}>{t('Save project as a template')}</button>}
      {creating && library.can_save && <ProjectTemplateEditor project={project} mode="save" onUnavailable={onUnavailable} onCancel={() => setCreating(false)} onCreated={onCreated} onSaved={(value) => { setCreating(false); setSelected(value.id); void refresh() }} />}
      {!creating && <>
        {library.items.length === 0 && <p>{t('No project templates yet.')}</p>}
        <ul className="template-list">{library.items.map((row) => <li key={row.id}><button type="button" aria-expanded={selected === row.id} onClick={() => setSelected(selected === row.id ? null : row.id)}><strong>{row.title}</strong><span>v{row.latest_version} · {row.created_by}{row.archived ? ` · ${t('Archived')}` : ''}</span></button></li>)}</ul>
        {item && <TemplateVersions key={item.id} project={project} template={item} canCreate={library.can_create_project} onUnavailable={onUnavailable} onChanged={() => void refresh()} onCreated={onCreated} />}
      </>}
    </div>}
  </div>
}

function ScopedTemplates(props: Props) {
  const t = useT()
  const id = useId()
  const [open, setOpen] = useState(false)
  const origin = props.project.template_origin
  return <section className="project-templates">
    <button type="button" className="project-templates-toggle" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}><span>{t('Project templates')}</span><span aria-hidden="true">{open ? '−' : '+'}</span></button>
    {origin && <p className="template-origin">{t('Created from template')}: {origin.title} · v{origin.version}<small>{origin.applied_by} · {new Date(origin.created_at).toLocaleString()}</small></p>}
    {open && <div id={id}><TemplateLibraryView {...props} /></div>}
  </section>
}
export default function ProjectTemplatesPanel(props: Props) { return <ScopedTemplates key={props.project.id} {...props} /> }
