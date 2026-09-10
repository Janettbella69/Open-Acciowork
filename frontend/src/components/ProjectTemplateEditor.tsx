import { useState } from 'react'
import { applyProjectTemplate, saveProjectTemplate } from '../api/client'
import { requireTemplateApplication, type ProjectSeed, type ProjectTemplate, type TemplateDetail, type TemplateReference } from '../api/projectTemplates'
import type { OpportunityProject } from '../api/types'
import { useT } from '../i18n'
import { useCollaborationRequest, useCommentRequestKey } from './useCollaborationRequest'

export function TemplateSeedReview({ seed, references }: { seed: ProjectSeed; references: TemplateReference[] }) {
  const t = useT()
  return <div className="template-review">
    <strong>{seed.name}</strong>
    <p>{seed.objective}</p><p className="template-text">{seed.product_brief}</p>
    <p>{t('Target markets')}: {seed.target_markets.join(' · ') || '—'}</p>
    <p className="template-text">{t('Constraints')}: {seed.constraints.join('\n') || '—'}</p>
    <strong>{t('Selected reference versions')}</strong>
    {references.length ? <ul>{references.map((row) => <li key={row.artifact_id}>{row.name} · v{row.version}<small>{row.size_bytes.toLocaleString()} B · SHA-256 {row.checksum}</small></li>)}</ul> : <p>{t('No reference files selected.')}</p>}
  </div>
}

type Props = {
  project: OpportunityProject; detail?: TemplateDetail; mode: 'save' | 'apply'
  onUnavailable: () => void; onCancel: () => void; onSaved: (value: ProjectTemplate) => void
  onCreated: (project: OpportunityProject) => void
}
export default function ProjectTemplateEditor({ project, detail, mode, onUnavailable, onCancel, onSaved, onCreated }: Props) {
  const t = useT()
  const initial = detail?.version.seed ?? project
  const [title, setTitle] = useState(detail?.version.title ?? project.name)
  const [description, setDescription] = useState(detail?.version.description ?? '')
  const [name, setName] = useState(initial.name)
  const [objective, setObjective] = useState(initial.objective)
  const [brief, setBrief] = useState(initial.product_brief)
  const [markets, setMarkets] = useState(initial.target_markets.join('\n'))
  const [constraints, setConstraints] = useState(initial.constraints.join('\n'))
  const [selected, setSelected] = useState<string[]>(mode === 'save' ? detail?.version.references.map((row) => row.artifact_id) ?? [] : [])
  const [reviewing, setReviewing] = useState(false)
  const { run, busy, error } = useCollaborationRequest(onUnavailable)
  const keys = useCommentRequestKey()
  const references: TemplateReference[] = mode === 'apply' ? detail?.version.references ?? [] : project.artifacts.map((row) => ({ ...row, artifact_id: row.id }))
  const chosen = references.filter((row) => selected.includes(row.artifact_id))
  const lines = (value: string) => value.split('\n').map((row) => row.trim()).filter(Boolean)
  const seed = { name: name.trim(), objective: objective.trim(), product_brief: brief.trim(), target_markets: lines(markets), constraints: lines(constraints) }
  const excessive = chosen.length > 20 || chosen.reduce((sum, row) => sum + row.size_bytes, 0) > 50 * 1024 * 1024
    || seed.target_markets.length > 50 || seed.constraints.length > 100 || seed.target_markets.some((row) => row.length > 160) || seed.constraints.some((row) => row.length > 1000)

  function confirm() {
    if (excessive || !reviewing) return
    if (mode === 'apply' && detail) {
      const data = { version: detail.version.version, content_hash: detail.version.content_hash, seed, artifact_ids: selected }
      void run(async (signal) => {
        const result = await applyProjectTemplate(project.id, detail.template.id, { ...data, request_id: keys.forPayload(data) }, signal)
        requireTemplateApplication(project.id, detail.template.id, result)
        return result
      }, (result) => { keys.clear(); onCreated(result.project) })
    } else {
      const data = { title: title.trim(), description: description.trim(), seed, artifact_ids: selected, ...(detail ? { expected_revision: detail.template.revision } : {}) }
      void run(async (signal) => {
        const result = await saveProjectTemplate(project.id, { ...data, request_id: keys.forPayload(data) }, detail?.template.id, signal)
        if (result.project_id !== project.id || (detail && result.id !== detail.template.id)) throw new Error('Template response did not match the reviewed version.')
        return result
      }, (result) => { keys.clear(); onSaved(result) })
    }
  }
  return <form className="template-editor" onSubmit={(event) => { event.preventDefault(); if (!excessive) setReviewing(true) }}>
    <h4>{t(mode === 'apply' ? 'Create project from this version' : detail ? 'Save a new template version' : 'Save project as a template')}</h4>
    <p>{t('Choose reusable instructions and reference files. Evidence, decisions, team access and scheduled tasks belong to each new project.')}</p>
    {error && <p role="alert">{t(error)}</p>}
    {excessive && <p role="alert">{t('Use at most 20 files totaling 50 MiB, 50 markets and 100 constraints. Shorten any overlong line.')}</p>}
    {!reviewing ? <>
      <fieldset disabled={busy}>
        {mode === 'save' && <><label>{t('Template title')}<input required maxLength={160} value={title} onChange={(e) => setTitle(e.target.value)} /></label><label>{t('Template description')}<textarea maxLength={2000} value={description} onChange={(e) => setDescription(e.target.value)} /></label></>}
        <label>{t('New project name')}<input required maxLength={160} value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label>{t('Project objective')}<textarea required maxLength={4000} value={objective} onChange={(e) => setObjective(e.target.value)} /></label>
        <label>{t('Reusable project brief')}<textarea maxLength={12000} value={brief} onChange={(e) => setBrief(e.target.value)} /></label>
        <label>{t('Target markets, one per line')}<textarea maxLength={8050} value={markets} onChange={(e) => setMarkets(e.target.value)} /></label>
        <label>{t('Constraints, one per line')}<textarea maxLength={64000} value={constraints} onChange={(e) => setConstraints(e.target.value)} /></label>
        <fieldset><legend>{t('Select saved reference versions')}</legend>
          {references.length ? references.map((row) => <label className="template-check" key={row.artifact_id}><input type="checkbox" checked={selected.includes(row.artifact_id)} onChange={(e) => setSelected(e.target.checked ? [...selected, row.artifact_id] : selected.filter((id) => id !== row.artifact_id))} /><span>{row.name} · v{row.version}<small>{row.size_bytes.toLocaleString()} B · {row.checksum.slice(0, 12)}</small></span></label>) : <p>{t('No saved reference files are available.')}</p>}
        </fieldset>
      </fieldset>
      <button type="submit" disabled={busy || excessive}>{t('Review template choices')}</button>
    </> : <>
      {mode === 'save' && <p><strong>{title}</strong> · {description}</p>}
      {detail && <p>{detail.version.title} · v{detail.version.version}</p>}
      <TemplateSeedReview seed={seed} references={chosen} />
      {mode === 'apply' && <p>{t('Create a separate project with these settings. Send its first message when you are ready to start research.')}</p>}
      <div className="template-actions"><button type="button" disabled={busy || excessive} onClick={confirm}>{t(busy ? 'Saving…' : mode === 'apply' ? 'Confirm and create project' : 'Confirm and save template')}</button><button type="button" disabled={busy} onClick={() => setReviewing(false)}>{t('Edit choices')}</button></div>
    </>}
    <button type="button" disabled={busy} onClick={onCancel}>{t('Cancel')}</button>
  </form>
}
