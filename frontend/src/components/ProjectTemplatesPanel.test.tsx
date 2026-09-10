import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { StrictMode } from 'react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { ProjectTemplate, TemplateApplied, TemplateDetail, TemplateLibrary } from '../api/projectTemplates'
import type { OpportunityProject } from '../api/types'
import { I18nContext } from '../i18n'
import ProjectTemplatesPanel from './ProjectTemplatesPanel'

vi.mock('../api/client', () => ({
  listProjectTemplates: vi.fn(), getTemplateVersion: vi.fn(), saveProjectTemplate: vi.fn(), archiveProjectTemplate: vi.fn(),
  applyProjectTemplate: vi.fn(), listTemplateApplications: vi.fn(), resumeTemplateApplication: vi.fn(), getProject: vi.fn(),
  templateReferenceUrl: (p: string, t: string, v: number, a: string) => `/api/projects/${p}/templates/${t}/versions/${v}/references/${a}/download`,
  errorMessage: (reason: unknown) => reason instanceof Error ? reason.message : String(reason),
}))
import { applyProjectTemplate, archiveProjectTemplate, getTemplateVersion, listProjectTemplates, listTemplateApplications, resumeTemplateApplication, saveProjectTemplate } from '../api/client'

const date = '2026-09-10T00:00:00Z'
const artifact = { id: 'artifact_a', project_id: 'project_a', name: 'source.txt', path: 'uploads/source.txt', mime: 'text/plain', version: 1, checksum: 'a'.repeat(64), size_bytes: 12, run_id: null, source_ids: [], created_by: 'owner', created_at: date, updated_at: date, download_url: '/api/projects/project_a/artifacts/artifact_a/download' }
const project: OpportunityProject = {
  id: 'project_a', name: 'Original', objective: 'Source objective', product_brief: 'Reusable instructions', target_markets: ['Japan'], constraints: ['Confirm current price'],
  artifacts: [artifact], stage: 'discovery', revision: 3, created_at: date, updated_at: date,
  hypotheses: [], evidence: [], decisions: [], assessments: {}, stage_decisions: [],
}
const template: ProjectTemplate = {
  id: 'template_a', project_id: project.id, created_by: 'owner', revision: 1, archived: false, title: 'Supplier template', description: 'A reusable supplier brief',
  latest_version: 1, content_hash: 'b'.repeat(64), created_at: date, updated_at: date, can_manage: true, can_apply: true,
  versions: [{ version: 1, title: 'Supplier template', content_hash: 'b'.repeat(64), created_at: date, created_by: 'owner', file_count: 1, size_bytes: 12 }],
}
const seed = { name: 'New supplier', objective: 'Verify new quotes', product_brief: 'Use current evidence', target_markets: ['Japan'], constraints: ['Confirm MOQ'] }
const reference = { artifact_id: artifact.id, name: artifact.name, mime: artifact.mime, version: 1, checksum: artifact.checksum, size_bytes: artifact.size_bytes }
const detail: TemplateDetail = { project_id: project.id, template, version: { version: 1, title: template.title, description: template.description, content_hash: template.content_hash, created_at: date, created_by: 'owner', source_project_revision: 3, seed, references: [reference] } }
const library: TemplateLibrary = { project_id: project.id, actor_id: 'owner', can_save: true, can_create_project: true, items: [template] }
const application = { id: 'template_use_a', source_project_id: project.id, template_id: template.id, template_version: 1, project_id: 'project_new', state: 'applied' as const, created_at: date, created_by: 'owner', seed, references: [reference], can_resume: false }
const created: TemplateApplied = { application, project: { ...project, ...seed, id: application.project_id, template_origin: { source_project_id: project.id, template_id: template.id, version: 1, title: template.title, content_hash: template.content_hash, seed_hash: 'c'.repeat(64), application_id: application.id, applied_by: 'owner', created_at: date, references: [] } } }

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(listProjectTemplates).mockResolvedValue(library)
  vi.mocked(getTemplateVersion).mockResolvedValue(detail)
  vi.mocked(saveProjectTemplate).mockResolvedValue(template)
  vi.mocked(applyProjectTemplate).mockResolvedValue(created)
  vi.mocked(archiveProjectTemplate).mockResolvedValue({ ...template, revision: 2, archived: true, can_apply: false })
  vi.mocked(listTemplateApplications).mockResolvedValue({ project_id: project.id, template_id: template.id, items: [application], next_before: null })
  vi.mocked(resumeTemplateApplication).mockResolvedValue(created)
})
function open() { fireEvent.click(screen.getByRole('button', { name: 'Project templates' })) }
async function version() {
  open()
  fireEvent.click(await screen.findByRole('button', { name: /Supplier template v1/ }))
  return screen.findByRole('region', { name: 'Template versions' })
}

it('loads on demand, shows immutable reference downloads and renders source text safely', async () => {
  vi.mocked(getTemplateVersion).mockResolvedValue({ ...detail, version: { ...detail.version, description: '<script>private reference</script>' } })
  const { container } = render(<ProjectTemplatesPanel project={project} onCreated={vi.fn()} />)
  expect(listProjectTemplates).not.toHaveBeenCalled()
  await version()
  expect(await screen.findByRole('link', { name: /Download reference · source.txt · v1/ })).toHaveAttribute('href', `/api/projects/${project.id}/templates/${template.id}/versions/1/references/${artifact.id}/download`)
  expect(screen.getByText('<script>private reference</script>')).toBeInTheDocument()
  expect(container.querySelector('script')).toBeNull()
})

it('requires review before saving and selects only explicitly checked file versions', async () => {
  render(<ProjectTemplatesPanel project={project} onCreated={vi.fn()} />)
  open(); fireEvent.click(await screen.findByRole('button', { name: 'Save project as a template' }))
  const checkbox = screen.getByRole('checkbox', { name: /source.txt · v1/ })
  expect(checkbox).not.toBeChecked()
  fireEvent.click(checkbox)
  fireEvent.change(screen.getByRole('textbox', { name: 'Template title' }), { target: { value: 'Curated template' } })
  fireEvent.click(screen.getByRole('button', { name: 'Review template choices' }))
  expect(saveProjectTemplate).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and save template' }))
  await waitFor(() => expect(saveProjectTemplate).toHaveBeenCalledOnce())
  expect(vi.mocked(saveProjectTemplate).mock.calls[0][1]).toMatchObject({ title: 'Curated template', artifact_ids: [artifact.id], seed: { name: project.name, objective: project.objective } })
  expect(vi.mocked(saveProjectTemplate).mock.calls[0][1].seed).not.toHaveProperty('evidence')
})

it('keeps a reviewed creation and its request key after a lost response', async () => {
  vi.mocked(applyProjectTemplate).mockRejectedValueOnce(new Error('Connection lost'))
  const onCreated = vi.fn()
  render(<ProjectTemplatesPanel project={project} onCreated={onCreated} />)
  await version()
  fireEvent.click(await screen.findByRole('button', { name: 'Create project from this version' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'New project name' }), { target: { value: 'Personalized project' } })
  fireEvent.click(screen.getByRole('checkbox', { name: /source.txt/ }))
  fireEvent.click(screen.getByRole('button', { name: 'Review template choices' }))
  expect(applyProjectTemplate).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and create project' }))
  await screen.findByText('Connection lost')
  expect(screen.getByText('Personalized project')).toBeInTheDocument()
  const payload = vi.mocked(applyProjectTemplate).mock.calls[0][2]
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and create project' }))
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created.project))
  expect(vi.mocked(applyProjectTemplate).mock.calls[1][2]).toEqual(payload)
  expect(payload).toMatchObject({ version: 1, content_hash: template.content_hash, artifact_ids: [artifact.id], seed: { name: 'Personalized project' } })
})

it('shows durable pending choices and resumes the same creation after a fresh mount', async () => {
  const pending = { ...application, state: 'ready' as const, can_resume: true }
  vi.mocked(listTemplateApplications).mockResolvedValue({ project_id: project.id, template_id: template.id, items: [pending], next_before: null })
  const onCreated = vi.fn()
  render(<ProjectTemplatesPanel project={project} onCreated={onCreated} />)
  await version()
  fireEvent.click(screen.getByRole('button', { name: 'Your project creations' }))
  const history = await screen.findByRole('region', { name: 'Your project creations' })
  fireEvent.click(await within(history).findByRole('button', { name: /New supplier · v1 · Creation pending/ }))
  expect(within(history).getByText('Verify new quotes')).toBeInTheDocument()
  expect(resumeTemplateApplication).not.toHaveBeenCalled()
  fireEvent.click(within(history).getByRole('button', { name: 'Confirm and resume creation' }))
  await waitFor(() => expect(onCreated).toHaveBeenCalledWith(created.project))
  expect(resumeTemplateApplication).toHaveBeenCalledWith(project.id, template.id, application.id, expect.any(AbortSignal))
})

it('uses the reviewed template revision for archive confirmation', async () => {
  render(<ProjectTemplatesPanel project={project} onCreated={vi.fn()} />)
  await version(); fireEvent.click(await screen.findByRole('button', { name: 'Archive template' }))
  expect(archiveProjectTemplate).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm archive template' }))
  await waitFor(() => expect(archiveProjectTemplate).toHaveBeenCalledOnce())
  expect(vi.mocked(archiveProjectTemplate).mock.calls[0][2]).toMatchObject({ expected_revision: 1, archived: true })
})

it('clears private drafts after access loss and ignores a late creation after navigation', async () => {
  vi.mocked(getTemplateVersion).mockRejectedValueOnce(Object.assign(new Error('Access revoked'), { status: 403 }))
  const onCreated = vi.fn()
  const view = render(<ProjectTemplatesPanel project={project} onCreated={onCreated} />)
  open(); fireEvent.click(await screen.findByRole('button', { name: /Supplier template v1/ }))
  await screen.findByText('Project template access changed. Refresh after signing in with your current account and role.')
  expect(screen.queryByText(template.title)).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh templates' }))
  fireEvent.click(await screen.findByRole('button', { name: /Supplier template v1/ }))
  fireEvent.click(await screen.findByRole('button', { name: 'Create project from this version' }))
  let resolve!: (value: TemplateApplied) => void
  vi.mocked(applyProjectTemplate).mockReturnValueOnce(new Promise((done) => { resolve = done }))
  fireEvent.click(screen.getByRole('button', { name: 'Review template choices' }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and create project' }))
  view.rerender(<ProjectTemplatesPanel project={{ ...project, id: 'project_elsewhere' }} onCreated={onCreated} />)
  await act(async () => { resolve(created) })
  expect(onCreated).not.toHaveBeenCalled()
  expect(screen.queryByText('New supplier')).not.toBeInTheDocument()
})

it('keeps viewers read-only and works after StrictMode effect restart in Chinese', async () => {
  vi.mocked(listProjectTemplates).mockResolvedValue({ ...library, can_save: false, can_create_project: false, items: [{ ...template, can_manage: false, can_apply: false }] })
  render(<StrictMode><I18nContext.Provider value="zh-CN"><ProjectTemplatesPanel project={project} onCreated={vi.fn()} /></I18nContext.Provider></StrictMode>)
  fireEvent.click(screen.getByRole('button', { name: '项目模板' }))
  fireEvent.click(await screen.findByRole('button', { name: /Supplier template v1/ }))
  await screen.findByRole('link', { name: /下载参考文件/ })
  expect(screen.queryByRole('button', { name: '将项目保存为模板' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '使用此版本创建项目' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '归档模板' })).not.toBeInTheDocument()
})
