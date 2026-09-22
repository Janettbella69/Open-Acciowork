import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { deleteProject, previewProjectDeletion } from '../api/client'
import type { ProjectDeletionPreview } from '../api/projectErasure'
import { I18nContext } from '../i18n'
import ProjectDeletionPanel from './ProjectDeletionPanel'

vi.mock('../api/client', () => ({
  deleteProject: vi.fn(), previewProjectDeletion: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))
const preview: ProjectDeletionPreview = {
  kind: 'project', project_id: 'project_1', project_name: 'Private project', request_id: 'a'.repeat(32),
  reviewed_at: '2026-09-22T00:00:00Z', fingerprint: 'b'.repeat(64), run_ids: ['run_1', 'run_2'],
  file_count: 12, source_bytes: 1200, native_session_count: 1, memory_count: 2, retained: [],
  schedules: 1, intents: 1, shops: 1, devices: 2, independent_copies: 1,
}
const result = { status: 'deleted' as const, project_id: 'project_1', request_id: preview.request_id }
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(previewProjectDeletion).mockResolvedValue(preview)
  vi.mocked(deleteProject).mockResolvedValue(result)
})

async function reviewAndConfirm() {
  fireEvent.click(screen.getByRole('button', { name: 'Review project deletion' }))
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.change(screen.getByRole('textbox'), { target: { value: preview.project_name } })
}

it('requires the exact name and irreversible confirmation and submits only once', async () => {
  const onDeleted = vi.fn()
  render(<ProjectDeletionPanel projectId="project_1" onDeleted={onDeleted} />)
  expect(previewProjectDeletion).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Review project deletion' }))
  const button = await screen.findByRole('button', { name: 'Permanently delete project' })
  expect(screen.getByText('1 independently copied projects will remain usable.')).toBeInTheDocument()
  expect(button).toBeDisabled()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: preview.project_name } })
  expect(button).toBeDisabled()
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.change(screen.getByRole('textbox'), { target: { value: preview.project_name + ' ' } })
  expect(button).toBeDisabled()
  fireEvent.change(screen.getByRole('textbox'), { target: { value: preview.project_name } })
  fireEvent.click(button); fireEvent.click(button)
  await waitFor(() => expect(onDeleted).toHaveBeenCalledWith('project_1'))
  expect(deleteProject).toHaveBeenCalledExactlyOnceWith('project_1', preview, preview.project_name)
})

it('retries the same reviewed request after a lost response', async () => {
  vi.mocked(deleteProject).mockRejectedValueOnce(new Error('Network disconnected'))
  const onDeleted = vi.fn()
  render(<ProjectDeletionPanel projectId="project_1" onDeleted={onDeleted} />)
  await reviewAndConfirm()
  fireEvent.click(screen.getByRole('button', { name: 'Permanently delete project' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Network disconnected')
  expect(screen.getByText(/A disconnected request does not cancel deletion/)).toBeInTheDocument()
  expect(onDeleted).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Retry reviewed deletion' }))
  await waitFor(() => expect(onDeleted).toHaveBeenCalledWith('project_1'))
  expect(vi.mocked(deleteProject).mock.calls).toEqual([
    ['project_1', preview, preview.project_name], ['project_1', preview, preview.project_name],
  ])
})

it('ignores a late preview after navigation and shows the Chinese entry point', async () => {
  let resolve!: (value: ProjectDeletionPreview) => void
  vi.mocked(previewProjectDeletion).mockReturnValue(new Promise((done) => { resolve = done }))
  const view = render(<ProjectDeletionPanel projectId="project_1" onDeleted={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Review project deletion' }))
  const signal = vi.mocked(previewProjectDeletion).mock.calls[0][1]
  view.rerender(<I18nContext.Provider value="zh-CN"><ProjectDeletionPanel projectId="project_2" onDeleted={vi.fn()} /></I18nContext.Provider>)
  await act(async () => { resolve(preview) })
  expect(signal?.aborted).toBe(true)
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '核对项目删除范围' })).toBeEnabled()
})

it('reports committed deletion after unmount without aborting the mutation', async () => {
  let resolve!: (value: typeof result) => void
  vi.mocked(deleteProject).mockReturnValue(new Promise((done) => { resolve = done }))
  const onDeleted = vi.fn()
  const view = render(<ProjectDeletionPanel projectId="project_1" onDeleted={onDeleted} />)
  await reviewAndConfirm()
  fireEvent.click(screen.getByRole('button', { name: 'Permanently delete project' }))
  view.unmount()
  await act(async () => { resolve(result) })
  expect(onDeleted).toHaveBeenCalledWith('project_1')
})

it('does not clear the workspace for an unverified response or start deletion while active', async () => {
  vi.mocked(deleteProject).mockResolvedValue({ ...result, project_id: 'project_2' })
  const onDeleted = vi.fn()
  const view = render(<ProjectDeletionPanel projectId="project_1" disabled onDeleted={onDeleted} />)
  expect(screen.getByRole('button', { name: 'Review project deletion' })).toBeDisabled()
  view.rerender(<ProjectDeletionPanel projectId="project_1" onDeleted={onDeleted} />)
  await reviewAndConfirm()
  fireEvent.click(screen.getByRole('button', { name: 'Permanently delete project' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Deletion result could not be verified.')
  expect(onDeleted).not.toHaveBeenCalled()
})
