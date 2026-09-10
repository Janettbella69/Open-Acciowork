import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { Artifact } from '../api/types'
import type { ArtifactTextView } from '../api/artifacts'
import { I18nContext } from '../i18n'
import ProjectArtifactsPanel from './ProjectArtifactsPanel'

vi.mock('../api/client', () => ({
  getArtifactText: vi.fn(), saveArtifactText: vi.fn(),
  artifactDownloadUrl: (project: string, id: string) => `/api/projects/${project}/artifacts/${id}/download`,
  errorMessage: (error: unknown) => String(error instanceof Error ? error.message : error),
}))
import { getArtifactText, saveArtifactText } from '../api/client'

const first: Artifact = {
  id: 'artifact_1', project_id: 'project_1', path: 'report.md', name: 'report.md',
  mime: 'text/markdown', version: 1, run_id: null, source_ids: [], checksum: 'a'.repeat(64),
  size_bytes: 20, download_url: 'https://untrusted.test/wrong', content_storage: 'snapshot',
  created_at: '2026-09-09T10:00:00Z', updated_at: '2026-09-09T10:00:00Z',
}
const second: Artifact = { ...first, id: 'artifact_2', version: 2, parent_artifact_id: first.id, checksum: 'b'.repeat(64) }
function view(item = first) { return { artifact: item, content: 'Original <script>text</script>', latest_artifact_id: item.id, editable: true, edit_notice: null } }
function open() { fireEvent.click(screen.getByRole('button', { name: 'Project files · 1' })) }
async function edit() {
  open(); fireEvent.click(screen.getByRole('button', { name: 'Preview and edit' }))
  await screen.findByRole('textbox', { name: 'File contents' })
}
function draft() {
  fireEvent.change(screen.getByRole('textbox', { name: 'File contents' }), { target: { value: 'My revision' } })
  fireEvent.click(screen.getByRole('button', { name: 'Review changes' }))
}
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getArtifactText).mockResolvedValue(view())
  vi.mocked(saveArtifactText).mockResolvedValue(second)
})

it('shows all versions, creates scoped links, and renders source text without HTML execution', async () => {
  const { container } = render(<ProjectArtifactsPanel projectId="project_1" artifacts={[first, second]} onChanged={vi.fn()} />)
  open()
  expect(screen.getByRole('link', { name: 'Download version 1' })).toHaveAttribute('href', '/api/projects/project_1/artifacts/artifact_1/download')
  expect(screen.getByRole('link', { name: 'Download version 2' })).toHaveAttribute('href', '/api/projects/project_1/artifacts/artifact_2/download')
  fireEvent.click(screen.getByRole('button', { name: 'Preview version 1' }))
  expect(await screen.findByRole('textbox', { name: 'File contents' })).toHaveValue('Original <script>text</script>')
  expect(container.querySelector('script')).toBeNull()
})

it('requires change review then persists a new version and refreshes project truth', async () => {
  const changed = vi.fn()
  render(<ProjectArtifactsPanel projectId="project_1" artifacts={[first]} onChanged={changed} />)
  await edit()
  fireEvent.change(screen.getByRole('textbox', { name: 'File contents' }), { target: { value: 'My revision' } })
  expect(screen.getByRole('button', { name: 'Save new version' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Review changes' }))
  expect(screen.getByText('Saved version')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Save new version' }))
  await screen.findByText('Version 2 saved. Previous versions are available in history.')
  expect(saveArtifactText).toHaveBeenCalledWith('project_1', 'artifact_1', {
    content: 'My revision', expected_checksum: first.checksum, request_id: expect.any(String),
  }, expect.any(AbortSignal))
  expect(changed).toHaveBeenCalledOnce()
  expect(screen.getByRole('link', { name: 'Download version 1' })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Download version 2' })).toBeInTheDocument()
})

it('keeps the draft after failure, reuses the request id on retry, and prevents double submits', async () => {
  vi.mocked(saveArtifactText).mockRejectedValueOnce(new Error('Connection lost'))
  render(<ProjectArtifactsPanel projectId="project_1" artifacts={[first]} onChanged={vi.fn()} />)
  await edit(); draft()
  fireEvent.click(screen.getByRole('button', { name: 'Save new version' }))
  await screen.findByRole('alert')
  expect(screen.getByRole('textbox', { name: 'File contents' })).toHaveValue('My revision')
  const originalKey = vi.mocked(saveArtifactText).mock.calls[0][2].request_id
  fireEvent.click(screen.getByRole('button', { name: 'Save new version' }))
  expect(screen.getByRole('button', { name: 'Saving version…' })).toBeDisabled()
  await screen.findByText('Version 2 saved. Previous versions are available in history.')
  expect(vi.mocked(saveArtifactText).mock.calls[1][2].request_id).toBe(originalKey)
})

it('keeps unsaved text until the user discards it explicitly', async () => {
  render(<ProjectArtifactsPanel projectId="project_1" artifacts={[first]} onChanged={vi.fn()} />)
  await edit(); draft()
  fireEvent.click(screen.getByRole('button', { name: 'Back to files' }))
  expect(screen.getByRole('alert')).toHaveTextContent('Discard this unsaved draft?')
  fireEvent.click(screen.getByRole('button', { name: 'Keep editing' }))
  expect(screen.getByRole('textbox', { name: 'File contents' })).toHaveValue('My revision')
  fireEvent.click(screen.getByRole('button', { name: 'Back to files' }))
  fireEvent.click(screen.getByRole('button', { name: 'Discard draft' }))
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  expect(saveArtifactText).not.toHaveBeenCalled()
})

it('shows read-only history and project restrictions without a save control', async () => {
  vi.mocked(getArtifactText).mockResolvedValue({ ...view(), editable: false, edit_notice: 'You have read-only access to this project.' })
  render(<ProjectArtifactsPanel projectId="project_1" artifacts={[first]} onChanged={vi.fn()} />)
  open(); fireEvent.click(screen.getByRole('button', { name: 'Preview and edit' }))
  await screen.findByText('You have read-only access to this project.')
  expect(screen.queryByRole('button', { name: 'Save new version' })).not.toBeInTheDocument()
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
})

it('discards delayed private reads and save callbacks after project navigation', async () => {
  let finish!: (value: Artifact) => void
  vi.mocked(saveArtifactText).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const changed = vi.fn()
  const { rerender } = render(<ProjectArtifactsPanel projectId="project_1" artifacts={[first]} onChanged={changed} />)
  await edit(); draft()
  fireEvent.click(screen.getByRole('button', { name: 'Save new version' }))
  const signal = vi.mocked(saveArtifactText).mock.calls[0][3]
  rerender(<ProjectArtifactsPanel projectId="project_2" artifacts={[]} onChanged={changed} />)
  expect(signal?.aborted).toBe(true)
  await act(async () => finish(second))
  expect(changed).not.toHaveBeenCalled()
  expect(screen.queryByText('My revision')).not.toBeInTheDocument()
  expect(screen.queryByText('report.md')).not.toBeInTheDocument()
})

it('rejects a response for a different artifact without exposing its contents', async () => {
  vi.mocked(getArtifactText).mockResolvedValue({ ...view({ ...first, project_id: 'private' }), content: 'private secret' })
  render(<ProjectArtifactsPanel projectId="project_1" artifacts={[first]} onChanged={vi.fn()} />)
  open(); fireEvent.click(screen.getByRole('button', { name: 'Preview and edit' }))
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('Artifact response did not match this selection.'))
  expect(screen.queryByText('private secret')).not.toBeInTheDocument()
})

it('does not display a delayed text response after changing project', async () => {
  let finish!: (value: ArtifactTextView) => void
  vi.mocked(getArtifactText).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const { rerender } = render(<ProjectArtifactsPanel projectId="project_1" artifacts={[first]} onChanged={vi.fn()} />)
  open(); fireEvent.click(screen.getByRole('button', { name: 'Preview and edit' }))
  const signal = vi.mocked(getArtifactText).mock.calls[0][2]
  rerender(<ProjectArtifactsPanel projectId="project_2" artifacts={[]} onChanged={vi.fn()} />)
  await act(async () => finish({ ...view(), content: 'private delayed content' }))
  expect(signal?.aborted).toBe(true)
  expect(screen.queryByText('private delayed content')).not.toBeInTheDocument()
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
})

it('supports Chinese file management and keeps binary files downloadable', async () => {
  render(<I18nContext.Provider value="zh-CN"><ProjectArtifactsPanel projectId="project_1" artifacts={[{ ...first, path: 'photo.png', name: 'photo.png' }]} onChanged={vi.fn()} /></I18nContext.Provider>)
  fireEvent.click(screen.getByRole('button', { name: '项目文件 · 1' }))
  expect(screen.getByRole('link', { name: '下载' })).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '预览并编辑' })).not.toBeInTheDocument()
  expect(screen.getByText('版本历史 · 1')).toBeInTheDocument()
})
