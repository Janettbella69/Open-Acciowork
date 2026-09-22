import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { WorkspaceCheckpoint, WorkspaceRestore } from '../api/workspaceRecovery'
import { I18nContext } from '../i18n'
import WorkspaceRecoveryPanel from './WorkspaceRecoveryPanel'

vi.mock('../api/client', () => ({
  listWorkspaceCheckpoints: vi.fn(), previewWorkspaceRestore: vi.fn(),
  applyWorkspaceRestore: vi.fn(), getWorkspaceRestore: vi.fn(),
  errorMessage: (reason: unknown) => String(reason instanceof Error ? reason.message : reason),
}))
import { applyWorkspaceRestore, getWorkspaceRestore, listWorkspaceCheckpoints, previewWorkspaceRestore } from '../api/client'

const point: WorkspaceCheckpoint = {
  id: 'before-run_1', project_id: 'project_1', run_id: 'run_1', kind: 'run_start',
  created_at: '2026-09-09T12:00:00Z', files: 2, total_bytes: 120,
}
const review: WorkspaceRestore = {
  id: 'restore_1', project_id: 'project_1', checkpoint_id: point.id,
  created_at: point.created_at, expires_at: '2026-09-09T12:10:00Z', state: 'preview',
  changes: [
    { path: 'report.md', action: 'replace', before_kind: 'file', after_kind: 'file' },
    { path: '<script>bad.txt', action: 'remove', before_kind: 'file', after_kind: null },
  ], removes_files: 1, safety_checkpoint_id: null, applied_at: null,
}
const applied: WorkspaceRestore = { ...review, state: 'applied', safety_checkpoint_id: 'safety-restore_1', applied_at: point.created_at }

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listWorkspaceCheckpoints).mockResolvedValue([point])
  vi.mocked(previewWorkspaceRestore).mockResolvedValue(review)
  vi.mocked(applyWorkspaceRestore).mockResolvedValue(applied)
  vi.mocked(getWorkspaceRestore).mockResolvedValue(applied)
})

async function openReview() {
  fireEvent.click(screen.getByRole('button', { name: 'Workspace recovery' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Review file changes' }))
  await screen.findByRole('heading', { name: 'Review workspace changes' })
}
function confirm() {
  fireEvent.click(screen.getByRole('checkbox', { name: 'I confirm all listed file replacements and removals.' }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm and restore files' }))
}

it('loads only when opened and requires file review and explicit confirmation', async () => {
  const onChanged = vi.fn()
  const { container } = render(<WorkspaceRecoveryPanel projectId="project_1" onChanged={onChanged} />)
  expect(listWorkspaceCheckpoints).not.toHaveBeenCalled()
  await openReview()
  expect(screen.getByText('2 changes · 1 files removed from the current workspace')).toBeInTheDocument()
  expect(screen.getByText('<script>bad.txt')).toBeInTheDocument()
  expect(container.querySelector('script')).toBeNull()
  expect(screen.getByRole('button', { name: 'Confirm and restore files' })).toBeDisabled()
  expect(applyWorkspaceRestore).not.toHaveBeenCalled()
  confirm()
  expect(screen.getByRole('button', { name: 'Confirm and restore files' })).toBeDisabled()
  await screen.findByText('Files restored. The previous workspace is saved as a recovery point.')
  expect(applyWorkspaceRestore).toHaveBeenCalledExactlyOnceWith('project_1', review.id, expect.any(AbortSignal))
  expect(onChanged).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button', { name: 'Back to recovery points' }))
  await screen.findByRole('button', { name: 'Review file changes' })
  expect(listWorkspaceCheckpoints).toHaveBeenCalledTimes(2)
})

it('checks the same recovery after a lost response without automatically repeating it', async () => {
  vi.mocked(applyWorkspaceRestore).mockRejectedValueOnce(new Error('Connection lost'))
  const changed = vi.fn()
  render(<WorkspaceRecoveryPanel projectId="project_1" onChanged={changed} />)
  await openReview(); confirm()
  await screen.findByRole('alert')
  expect(screen.getByRole('button', { name: 'Back to recovery points' })).toBeDisabled()
  expect(screen.getByRole('checkbox')).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: 'Check recovery result' }))
  await screen.findByText('Files restored. The previous workspace is saved as a recovery point.')
  expect(getWorkspaceRestore).toHaveBeenCalledWith('project_1', review.id, expect.any(AbortSignal))
  expect(applyWorkspaceRestore).toHaveBeenCalledOnce()
  expect(changed).toHaveBeenCalledOnce()
})

it('preserves the reviewed request on retry and allows a fresh preview after a stale conflict', async () => {
  vi.mocked(applyWorkspaceRestore).mockRejectedValueOnce(new Error('Workspace changed after review. Preview again.'))
  vi.mocked(getWorkspaceRestore).mockResolvedValueOnce(review)
  render(<WorkspaceRecoveryPanel projectId="project_1" onChanged={vi.fn()} />)
  await openReview(); confirm()
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button', { name: 'Check recovery result' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Back to recovery points' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Back to recovery points' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Review file changes' }))
  await screen.findByRole('checkbox')
  expect(screen.getByRole('checkbox')).not.toBeChecked()
  expect(applyWorkspaceRestore).toHaveBeenCalledOnce()
})

it('ignores a delayed preview after switching project and aborts its request', async () => {
  let finish!: (value: WorkspaceRestore) => void
  vi.mocked(previewWorkspaceRestore).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const { rerender } = render(<WorkspaceRecoveryPanel projectId="project_1" onChanged={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Workspace recovery' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Review file changes' }))
  const signal = vi.mocked(previewWorkspaceRestore).mock.calls[0][2]
  rerender(<WorkspaceRecoveryPanel projectId="project_2" onChanged={vi.fn()} />)
  await act(async () => finish(review))
  expect(signal?.aborted).toBe(true)
  expect(screen.queryByText('<script>bad.txt')).not.toBeInTheDocument()
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
})

it('does not refresh a different project when a delayed restore response arrives', async () => {
  let finish!: (value: WorkspaceRestore) => void
  vi.mocked(applyWorkspaceRestore).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const changed = vi.fn()
  const { rerender } = render(<WorkspaceRecoveryPanel projectId="project_1" onChanged={changed} />)
  await openReview(); confirm()
  const signal = vi.mocked(applyWorkspaceRestore).mock.calls[0][2]
  rerender(<WorkspaceRecoveryPanel projectId="project_2" onChanged={changed} />)
  await act(async () => finish(applied))
  expect(signal?.aborted).toBe(true)
  expect(changed).not.toHaveBeenCalled()
})

it('rejects a response for a different checkpoint without enabling recovery', async () => {
  vi.mocked(previewWorkspaceRestore).mockResolvedValue({ ...review, checkpoint_id: 'wrong' })
  render(<WorkspaceRecoveryPanel projectId="project_1" onChanged={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Workspace recovery' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Review file changes' }))
  await screen.findByRole('alert')
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  expect(applyWorkspaceRestore).not.toHaveBeenCalled()
})

it('paginates every change without dropping the total or silently hiding removals', async () => {
  vi.mocked(previewWorkspaceRestore).mockResolvedValue({ ...review, removes_files: 101, changes: Array.from({ length: 101 }, (_, index) => ({ path: `file-${index}.txt`, action: 'remove', before_kind: 'file', after_kind: null })) })
  render(<WorkspaceRecoveryPanel projectId="project_1" onChanged={vi.fn()} />)
  await openReview()
  expect(screen.getByText('101 changes · 101 files removed from the current workspace')).toBeInTheDocument()
  expect(screen.queryByText('file-100.txt')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Next changes' }))
  expect(screen.getByText('file-100.txt')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Next changes' })).toBeDisabled()
})

it('provides Chinese recovery controls and an honest empty state', async () => {
  vi.mocked(listWorkspaceCheckpoints).mockResolvedValue([])
  render(<I18nContext.Provider value="zh-CN"><WorkspaceRecoveryPanel projectId="project_1" onChanged={vi.fn()} /></I18nContext.Provider>)
  fireEvent.click(screen.getByRole('button', { name: '工作区恢复' }))
  await screen.findByText('暂无恢复点。每次新运行开始前都会保存工作区副本。')
  expect(screen.getByRole('button', { name: '刷新恢复点' })).toBeInTheDocument()
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
})
