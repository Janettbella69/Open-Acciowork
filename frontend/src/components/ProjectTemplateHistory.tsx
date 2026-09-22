import { useCallback, useEffect, useState } from 'react'
import { getProject, listTemplateApplications, resumeTemplateApplication } from '../api/client'
import { requireTemplateApplication, type TemplateApplication, type TemplateApplications } from '../api/projectTemplates'
import type { OpportunityProject } from '../api/types'
import { useT } from '../i18n'
import { TemplateSeedReview } from './ProjectTemplateEditor'
import { useCollaborationRequest } from './useCollaborationRequest'

type Props = { projectId: string; templateId: string; onUnavailable: () => void; onCreated: (project: OpportunityProject) => void }
export default function ProjectTemplateHistory({ projectId, templateId, onUnavailable, onCreated }: Props) {
  const t = useT()
  const [page, setPage] = useState<TemplateApplications | null>(null)
  const [selected, setSelected] = useState<TemplateApplication | null>(null)
  const { run, busy, error } = useCollaborationRequest(onUnavailable)
  const refresh = useCallback((before?: string) => run(async (signal) => {
    const value = await listTemplateApplications(projectId, templateId, before, signal)
    if (value.project_id !== projectId || value.template_id !== templateId || value.items.some((row) => row.source_project_id !== projectId || row.template_id !== templateId)) throw new Error('Template response did not match the reviewed project creation.')
    return value
  }, (value) => { setPage(value); setSelected(null) }), [projectId, templateId, run])
  useEffect(() => { void refresh() }, [refresh])
  const open = (item: TemplateApplication) => {
    void run(async (signal) => {
      if (item.state === 'applied') {
        const project = await getProject(item.project_id, signal)
        if (project.id !== item.project_id || project.template_origin?.application_id !== item.id) throw new Error('Template response did not match the reviewed project creation.')
        return project
      }
      const result = await resumeTemplateApplication(projectId, templateId, item.id, signal)
      requireTemplateApplication(projectId, templateId, result)
      if (result.application.id !== item.id) throw new Error('Template response did not match the reviewed project creation.')
      return result.project
    }, onCreated)
  }
  return <section aria-label={t('Your project creations')}>
    <h4>{t('Your project creations')}</h4>
    <p>{t('An interrupted creation keeps its reviewed settings. Inspect it here and continue with the same project.')}</p>
    {error && <p role="alert">{t(error)}</p>}
    <button type="button" disabled={busy} onClick={() => void refresh()}>{t('Refresh creation history')}</button>
    {page?.items.length === 0 && <p>{t('No project creations yet.')}</p>}
    <ul className="template-list">{page?.items.map((item) => <li key={item.id}><button type="button" disabled={busy} aria-expanded={selected?.id === item.id} onClick={() => setSelected(selected?.id === item.id ? null : item)}>{item.seed.name} · v{item.template_version} · {t(item.state === 'applied' ? 'Project created' : 'Creation pending')}<small>{new Date(item.created_at).toLocaleString()}</small></button>
      {selected?.id === item.id && <div><TemplateSeedReview seed={item.seed} references={item.references} />
        {(item.state === 'applied' || item.can_resume) ? <button type="button" disabled={busy} onClick={() => open(item)}>{t(item.state === 'applied' ? 'Open created project' : 'Confirm and resume creation')}</button> : <p>{t('Restore this template to continue its pending creations.')}</p>}
      </div>}
    </li>)}</ul>
    {page?.next_before && <button type="button" disabled={busy} onClick={() => void refresh(page.next_before!)}>{t('Older creations')}</button>}
  </section>
}
