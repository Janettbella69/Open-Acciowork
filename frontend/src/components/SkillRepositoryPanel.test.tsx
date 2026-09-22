import { StrictMode } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api/client'
import type { SkillFilePreview, SkillImportView, SkillPackageView, SkillRepositoryCapabilities } from '../api/skillRepository'
import type { SkillVersion } from '../api/types'
import { I18nContext } from '../i18n'
import PrivateSkillImport from './PrivateSkillImport'
import SkillRepositoryPanel from './SkillRepositoryPanel'
import SkillsPage from './SkillsPage'

vi.mock('../api/client')

const capabilities: SkillRepositoryCapabilities = { identity_mode: 'local_only', tenant_id: null, actor_id: 'local-operator', can_manage: true }
const timestamp = '2026-09-09T12:00:00Z'
const skill: SkillVersion = { id: 'ophira-tenant:private-research', namespace: 'ophira-tenant', name: 'private-research', description: 'Reviewed private method.', source: 'tenant', version: '1.0.0', content_hash: 'a'.repeat(64), enabled: false }
const files = [
  { path: 'SKILL.md', size_bytes: 45, content_hash: 'c'.repeat(64), preview_kind: 'text' as const },
  { path: 'references/guide.md', size_bytes: 38, content_hash: 'd'.repeat(64), preview_kind: 'text' as const },
]
const candidate: SkillImportView = {
  import: {
    id: `skill_import_${'e'.repeat(64)}`, request_id: 'import_request_001', archive_hash: 'f'.repeat(64), filename: 'research.zip',
    scope: { schema_version: 1, auth_mode: 'local_only', tenant_id: null },
    uploaded_by: { kind: 'local_owner', id: 'local-operator', role: 'owner' },
    published_before_import: false,
    skill, files, scan: { skill_id: skill.id, checked_at: timestamp, ok: true, checks: [{ name: 'secret_scan', status: 'passed', detail: 'No credential patterns detected' }] },
    revision: 1, state: 'pending', history: [], publication_receipt: null, created_at: timestamp, updated_at: timestamp,
  },
  current_target: { current_hash: null, enabled: null, generation: 0 }, can_publish: true,
}
const published: SkillImportView = {
  ...candidate,
  import: { ...candidate.import, state: 'published', revision: 3,
    history: [{ request_id: 'review_request_01', request_hash: '3'.repeat(64), review_revision: 2, decision: 'approve', content_hash: skill.content_hash, reviewer: candidate.import.uploaded_by, note: 'Reviewed original files.', reviewed_at: timestamp, reviewer_binding: '4'.repeat(64), outcome: 'published', expected_target: candidate.current_target }],
    publication_receipt: { proposal_id: candidate.import.id, skill_id: skill.id, content_hash: skill.content_hash, review_revision: 2, reviewer_binding: '4'.repeat(64), previous_selection: candidate.current_target, published_at: timestamp },
  },
  current_target: { current_hash: skill.content_hash, enabled: false, generation: 1 }, can_publish: false,
}
const packageView: SkillPackageView = { skill, files, current_target: published.current_target }
const changed = vi.fn()
const unavailable = vi.fn()

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}
function preview(path: string): SkillFilePreview {
  const file = files.find((row) => row.path === path)!
  return { ...file, text: path === 'SKILL.md' ? 'Use reviewed sources and keep their provenance.' : '<script>neverExecute()</script><img />', truncated: false }
}
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api.errorMessage).mockImplementation((cause) => cause instanceof Error ? cause.message : 'Unknown error')
  vi.mocked(api.getSkillRepositoryCapabilities).mockResolvedValue(capabilities)
  vi.mocked(api.listSkills).mockResolvedValue([skill])
  vi.mocked(api.listSkillImports).mockResolvedValue([candidate])
  vi.mocked(api.getSkillImport).mockResolvedValue(candidate)
  vi.mocked(api.submitSkillImport).mockResolvedValue(candidate)
  vi.mocked(api.reviewSkillImport).mockResolvedValue(published)
  vi.mocked(api.getSkillImportFile).mockImplementation(async (_id, path) => preview(path))
  vi.mocked(api.skillImportDownloadUrl).mockImplementation((id, path) => `/api/skills/imports/${id}/download?path=${path}`)
  vi.mocked(api.listSkillVersions).mockResolvedValue([skill])
  vi.mocked(api.getSkillPackage).mockResolvedValue(packageView)
  vi.mocked(api.getSkillPackageFile).mockImplementation(async (_name, _hash, path) => preview(path))
  vi.mocked(api.skillPackageDownloadUrl).mockImplementation((name, hash) => `/api/skills/repository/${name}/versions/${hash}/download`)
  vi.mocked(api.selectPrivateSkill).mockResolvedValue({ skill: { ...skill, enabled: true }, current_target: { ...published.current_target, enabled: true, generation: 2 } })
})

function panel(scope = capabilities) {
  return <SkillRepositoryPanel skills={[skill]} capabilities={scope} onChanged={changed} onUnavailable={unavailable} />
}
async function openImport() {
  fireEvent.click(screen.getByRole('button', { name: 'Import reviews' }))
  fireEvent.click(await screen.findByRole('button', { name: /private-research · v1.0.0 Pending/ }))
  await screen.findByRole('heading', { name: 'private-research · v1.0.0' })
}
function review() {
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: 'Review publication' }))
}

describe('private Skill repository', () => {
  it('stages selected bytes only after upload and requires a separate exact publication review', async () => {
    vi.mocked(api.listSkillImports).mockResolvedValue([])
    const { container } = render(panel())
    fireEvent.click(screen.getByRole('button', { name: 'Import reviews' }))
    await screen.findByText('No private Skill imports yet.')
    const file = new File(['frozen ZIP bytes'], 'research.zip', { type: 'application/zip' })
    fireEvent.change(screen.getByLabelText('Choose Skill ZIP'), { target: { files: [file] } })
    expect(api.submitSkillImport).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Upload for review' }))
    expect(await screen.findByRole('heading', { name: 'private-research · v1.0.0' })).toBeInTheDocument()
    expect(api.submitSkillImport).toHaveBeenCalledWith(file, expect.any(String), expect.any(AbortSignal))
    expect(api.reviewSkillImport).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Review publication' })).toBeDisabled()
    await waitFor(() => expect(screen.getByLabelText('Inspect package file')).toBeEnabled())
    fireEvent.change(screen.getByLabelText('Inspect package file'), { target: { value: 'references/guide.md' } })
    expect(await screen.findByText('<script>neverExecute()</script><img />')).toBeInTheDocument()
    expect(container.querySelector('script, img')).toBeNull()
    expect(screen.getByRole('link', { name: 'Download original file' })).toHaveAttribute('download')
    fireEvent.change(screen.getByLabelText('Review note'), { target: { value: ' Reviewed original files. ' } })
    review()
    expect(screen.getByText('Publish this exact package, disabled?')).toBeInTheDocument()
    expect(api.reviewSkillImport).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm private publication' }))
    await screen.findByText('Import review history')
    expect(api.reviewSkillImport).toHaveBeenCalledWith(candidate.import.id, {
      request_id: expect.any(String), decision: 'approve', content_hash: skill.content_hash,
      revision: 1, expected_target: candidate.current_target, note: 'Reviewed original files.',
    }, expect.any(AbortSignal))
    expect(api.setSkillEnabled).not.toHaveBeenCalled()
    expect(api.selectPrivateSkill).not.toHaveBeenCalled()
    expect(api.startRun).not.toHaveBeenCalled()
    expect(changed).toHaveBeenCalledTimes(1)
  })

  it('retains upload bytes and request identity for an uncertain response retry', async () => {
    vi.mocked(api.submitSkillImport).mockRejectedValueOnce(new Error('Connection lost'))
    render(panel())
    fireEvent.click(screen.getByRole('button', { name: 'Import reviews' }))
    await screen.findByRole('button', { name: /private-research · v1.0.0 Pending/ })
    const file = new File(['archive'], 'retry.zip')
    fireEvent.change(screen.getByLabelText('Choose Skill ZIP'), { target: { files: [file] } })
    fireEvent.click(screen.getByRole('button', { name: 'Upload for review' }))
    await screen.findByText('Connection lost')
    fireEvent.click(screen.getByRole('button', { name: 'Upload for review' }))
    await screen.findByRole('heading', { name: 'private-research · v1.0.0' })
    expect(api.submitSkillImport).toHaveBeenCalledTimes(2)
    expect(vi.mocked(api.submitSkillImport).mock.calls[1].slice(0, 2)).toEqual(vi.mocked(api.submitSkillImport).mock.calls[0].slice(0, 2))
  })

  it('retains the reviewed request key after a lost publication response', async () => {
    vi.mocked(api.reviewSkillImport).mockRejectedValueOnce(new Error('Connection lost'))
    render(panel())
    await openImport()
    review()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm private publication' }))
    await screen.findByText('Connection lost')
    fireEvent.click(screen.getByRole('button', { name: 'Confirm private publication' }))
    await screen.findByText('Import review history')
    const calls = vi.mocked(api.reviewSkillImport).mock.calls
    expect(calls).toHaveLength(2)
    expect(calls[1][1]).toEqual(calls[0][1])
  })

  it('prevents double publication while its acknowledgement is pending', async () => {
    const pending = deferred<SkillImportView>()
    vi.mocked(api.reviewSkillImport).mockReturnValueOnce(pending.promise)
    render(panel())
    await openImport()
    review()
    const button = screen.getByRole('button', { name: 'Confirm private publication' })
    fireEvent.click(button); fireEvent.click(button)
    expect(api.reviewSkillImport).toHaveBeenCalledTimes(1)
    await act(async () => pending.resolve(published))
    expect(changed).toHaveBeenCalledTimes(1)
  })

  it('allows an unsafe import to be rejected only with a reason', async () => {
    const dangerous = { ...candidate, import: { ...candidate.import, scan: { ...candidate.import.scan, ok: false } }, can_publish: false }
    vi.mocked(api.getSkillImport).mockResolvedValue(dangerous)
    render(panel())
    await openImport()
    expect(screen.getByRole('checkbox')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Review publication' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Review rejection' })).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Review note'), { target: { value: 'Remove the dangerous command.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Review rejection' }))
    expect(api.reviewSkillImport).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm import rejection' }))
    expect(api.reviewSkillImport).toHaveBeenCalledWith(candidate.import.id, expect.objectContaining({ decision: 'reject', note: 'Remove the dangerous command.' }), expect.any(AbortSignal))
    expect(vi.mocked(api.reviewSkillImport).mock.calls[0][1]).not.toHaveProperty('expected_target')
    await waitFor(() => expect(changed).toHaveBeenCalledTimes(1))
  })

  it('lets a viewer inspect published files and download an exact version without management controls', async () => {
    render(panel({ ...capabilities, can_manage: false }))
    expect(screen.queryByRole('button', { name: 'Import reviews' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /private-research · v1.0.0 Disabled/ }))
    const link = await screen.findByRole('link', { name: 'Download this package version' })
    expect(link.getAttribute('href')).toContain(`/versions/${skill.content_hash}/download`)
    expect(await screen.findByText('Use reviewed sources and keep their provenance.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Review enablement' })).not.toBeInTheDocument()
    expect(api.listSkillImports).not.toHaveBeenCalled()
  })

  it('confirms enablement and historical restore against the inspected current selection', async () => {
    const old = { ...skill, version: '0.9.0', content_hash: '9'.repeat(64) }
    vi.mocked(api.listSkillVersions).mockResolvedValue([skill, old])
    vi.mocked(api.getSkillPackage).mockImplementation(async (_name, hash) => ({ ...packageView, skill: hash === old.content_hash ? old : skill }))
    render(panel())
    fireEvent.click(screen.getByRole('button', { name: /private-research · v1.0.0 Disabled/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Review enablement' }))
    expect(api.selectPrivateSkill).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm repository change' }))
    await waitFor(() => expect(changed).toHaveBeenCalledTimes(1))
    expect(api.selectPrivateSkill).toHaveBeenCalledWith(skill.name, { operation: 'enable', content_hash: skill.content_hash, expected_target: published.current_target }, expect.any(AbortSignal))
    fireEvent.change(screen.getByLabelText('Package version'), { target: { value: old.content_hash } })
    fireEvent.click(await screen.findByRole('button', { name: 'Review version restore' }))
    fireEvent.click(screen.getByRole('button', { name: 'Confirm repository change' }))
    await waitFor(() => expect(changed).toHaveBeenCalledTimes(2))
    expect(api.selectPrivateSkill).toHaveBeenLastCalledWith(skill.name, { operation: 'rollback', content_hash: old.content_hash, expected_target: published.current_target }, expect.any(AbortSignal))
  })

  it('rejects responses outside the current repository and file checksums', async () => {
    vi.mocked(api.getSkillImport).mockResolvedValue({ ...candidate, import: { ...candidate.import, scope: { schema_version: 1, auth_mode: 'trusted_gateway', tenant_id: 'another-tenant' } } })
    render(panel())
    fireEvent.click(screen.getByRole('button', { name: 'Import reviews' }))
    fireEvent.click(await screen.findByRole('button', { name: /private-research · v1.0.0 Pending/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Private Skill response did not match this repository.')
    expect(screen.queryByRole('button', { name: 'Review publication' })).not.toBeInTheDocument()
    vi.mocked(api.getSkillImport).mockResolvedValue(candidate)
    vi.mocked(api.getSkillImportFile).mockResolvedValue({ ...preview('SKILL.md'), content_hash: 'wrong' })
    fireEvent.click(screen.getByRole('button', { name: 'Reload import and current selection' }))
    expect(await screen.findByText('Private Skill file did not match its recorded checksum.')).toBeInTheDocument()
    expect(screen.queryByText('Use reviewed sources and keep their provenance.')).not.toBeInTheDocument()
  })

  it('keeps a review draft across equivalent capability refreshes and Strict Mode', async () => {
    const { rerender } = render(<StrictMode><PrivateSkillImport identifier={candidate.import.id} capabilities={capabilities} onChanged={changed} onUnavailable={unavailable} /></StrictMode>)
    fireEvent.change(await screen.findByLabelText('Review note'), { target: { value: 'My review draft' } })
    review()
    const calls = vi.mocked(api.getSkillImport).mock.calls.length
    rerender(<StrictMode><PrivateSkillImport identifier={candidate.import.id} capabilities={{ ...capabilities }} onChanged={changed} onUnavailable={unavailable} /></StrictMode>)
    expect(screen.getByLabelText('Review note')).toHaveValue('My review draft')
    expect(screen.getByText('Publish this exact package, disabled?')).toBeInTheDocument()
    expect(api.getSkillImport).toHaveBeenCalledTimes(calls)
  })

  it('discards a late publication response after leaving the review', async () => {
    const pending = deferred<SkillImportView>()
    vi.mocked(api.reviewSkillImport).mockReturnValueOnce(pending.promise)
    const view = render(panel())
    await openImport()
    review()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm private publication' }))
    const signal = vi.mocked(api.reviewSkillImport).mock.calls[0][2]!
    view.unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => pending.resolve(published))
    expect(changed).not.toHaveBeenCalled()
  })

  it('removes private drafts when refreshed access loses the administrator role', async () => {
    render(<SkillsPage />)
    await screen.findByRole('heading', { name: 'private research' })
    fireEvent.click(screen.getByRole('button', { name: 'Private repository' }))
    await openImport()
    fireEvent.change(screen.getByLabelText('Review note'), { target: { value: 'Private administrator draft' } })
    vi.mocked(api.getSkillRepositoryCapabilities).mockResolvedValue({ ...capabilities, can_manage: false })
    fireEvent(window, new Event('focus'))
    await waitFor(() => expect(screen.queryByLabelText('Review note')).not.toBeInTheDocument())
    expect(screen.queryByRole('button', { name: 'Import reviews' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Installed/ }))
    const card = (await screen.findByRole('heading', { name: 'private research' })).closest('article')!
    expect(within(card).queryByRole('button', { name: 'Enable' })).not.toBeInTheDocument()
  })

  it('renders the human publication review in Chinese', async () => {
    render(<I18nContext.Provider value="zh-CN">{panel()}</I18nContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: '导入审核' }))
    fireEvent.click(await screen.findByRole('button', { name: /private-research · v1.0.0/ }))
    await screen.findByLabelText('审核备注')
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: '核对发布' }))
    expect(screen.getByText('发布此版本并保持禁用？')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认私有发布' })).toBeInTheDocument()
  })
})
