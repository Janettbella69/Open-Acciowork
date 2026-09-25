import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { RetentionArchivePage, WorkspaceRetention } from '../api/workspaceRecovery'
import { I18nContext } from '../i18n'
import WorkspaceRetentionPanel from './WorkspaceRetentionPanel'

vi.mock('../api/client', () => ({
  getWorkspaceRetention: vi.fn(), getWorkspaceArchivePage: vi.fn(), changeWorkspaceRetention: vi.fn(),
  errorMessage: (reason: unknown) => String(reason instanceof Error ? reason.message : reason),
}))
import { changeWorkspaceRetention, getWorkspaceArchivePage, getWorkspaceRetention } from '../api/client'

const inventory: WorkspaceRetention = {
  project_id: 'project_1', checkpoint_limit: 500, review_limit: 1000,
  retained_bytes_limit: 2_000_000_000, retained_bytes: 120,
  archived_reviews: 0,
  snapshot_files: 3, snapshot_bytes: 240, reclaimable_files: 1,
  reclaimable_bytes: 40, reclaim_fingerprint: 'e'.repeat(64),
  checkpoints: [
    { id: 'before-run_1', run_id: 'run_1', kind: 'run_start', created_at: '2026-09-09T12:00:00Z',
      files: 2, total_bytes: 120, fingerprint: 'a'.repeat(64), referenced_by_reviews: 1 },
    { id: 'before-run_2', run_id: 'run_2', kind: 'run_start', created_at: '2026-09-08T12:00:00Z',
      files: 1, total_bytes: 40, fingerprint: 'b'.repeat(64), referenced_by_reviews: 0 },
  ],
  reviews: [
    { id: 'restore_1', checkpoint_id: 'before-run_1', state: 'preview',
      created_at: '2026-09-09T12:00:00Z', expires_at: '2026-09-09T12:10:00Z',
      fingerprint: 'c'.repeat(64), retained_bytes: 0, retained_discarded_at: null,
      can_delete: true, can_archive: false },
    { id: 'restore_2', checkpoint_id: 'before-run_1', state: 'applied',
      created_at: '2026-09-08T12:00:00Z', expires_at: '2026-09-08T12:10:00Z',
      fingerprint: 'd'.repeat(64), retained_bytes: 120, retained_discarded_at: null,
      can_delete: false, can_archive: false },
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getWorkspaceRetention).mockResolvedValue(inventory)
  vi.mocked(getWorkspaceArchivePage).mockResolvedValue({ project_id: 'project_1', reviews: [], next_cursor: null })
  vi.mocked(changeWorkspaceRetention).mockResolvedValue({
    ...inventory, reviews: inventory.reviews.slice(1),
  })
})

it('pages archived reviews and confirms permanent deletion of one result', async () => {
  const archived = { id: 'restore_archived', checkpoint_id: 'before-run_1',
    created_at: '2026-09-08T12:00:00Z', archived_at: '2026-09-10T12:00:00Z',
    archived_by: 'admin', fingerprint: 'f'.repeat(64) }
  const first: RetentionArchivePage = { project_id: 'project_1', reviews: [archived], next_cursor: archived.id }
  vi.mocked(getWorkspaceRetention).mockResolvedValueOnce({ ...inventory, archived_reviews: 2 })
  vi.mocked(getWorkspaceArchivePage)
    .mockResolvedValueOnce(first)
    .mockResolvedValueOnce({ project_id: 'project_1', reviews: [{ ...archived, id: 'restore_older' }], next_cursor: null })
    .mockResolvedValueOnce({ project_id: 'project_1', reviews: [{ ...archived, id: 'restore_older' }], next_cursor: null })
  vi.mocked(changeWorkspaceRetention).mockResolvedValueOnce({ ...inventory, archived_reviews: 1 })
  const changed = vi.fn()
  render(<WorkspaceRetentionPanel projectId="project_1" onChanged={changed} />)
  fireEvent.click(screen.getByRole('button', { name: 'Recovery history management' }))
  fireEvent.click(await screen.findByRole('button', { name: 'View archived reviews' }))
  await screen.findByText('restore_archived')
  fireEvent.click(screen.getByRole('button', { name: 'Show more archived reviews' }))
  await screen.findByText('restore_older')
  expect(getWorkspaceArchivePage).toHaveBeenCalledWith('project_1', 'restore_archived', expect.any(AbortSignal))
  fireEvent.click(screen.getAllByRole('button', { name: 'Delete archived review' })[0])
  expect(screen.getByText('This archived review result will be permanently removed. Workspace files, recovery points and artifact versions remain.')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('checkbox', { name: 'I confirm this recovery history deletion.' }))
  fireEvent.click(screen.getAllByRole('button', { name: 'Delete archived review' })[0])
  await waitFor(() => expect(changeWorkspaceRetention).toHaveBeenCalledWith(
    'project_1', 'archived', 'restore_archived', 'f'.repeat(64), expect.any(AbortSignal),
  ))
  await waitFor(() => expect(screen.queryByText('restore_archived')).not.toBeInTheDocument())
  expect(screen.getByText('restore_older')).toBeInTheDocument()
  expect(changed).toHaveBeenCalledOnce()
})

it('loads on demand, protects referenced points and confirms exact expired review', async () => {
  const changed = vi.fn()
  render(<WorkspaceRetentionPanel projectId="project_1" onChanged={changed} />)
  expect(getWorkspaceRetention).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Recovery history management' }))
  await screen.findByText('Used by 1 saved reviews')
  const deletePoints = screen.getAllByRole('button', { name: 'Delete recovery point' })
  expect(deletePoints[0]).toBeDisabled()
  expect(deletePoints[1]).toBeEnabled()
  fireEvent.click(screen.getByRole('button', { name: 'Delete expired review' }))
  expect(screen.getByText('This expired review has never changed workspace files.')).toBeInTheDocument()
  expect(screen.getAllByRole('button', { name: 'Delete expired review' })[0]).toBeDisabled()
  fireEvent.click(screen.getByRole('checkbox', { name: 'I confirm this recovery history deletion.' }))
  fireEvent.click(screen.getAllByRole('button', { name: 'Delete expired review' })[0])
  await waitFor(() => expect(changeWorkspaceRetention).toHaveBeenCalledWith(
    'project_1', 'review', 'restore_1', 'c'.repeat(64), expect.any(AbortSignal),
  ))
  expect(changed).toHaveBeenCalledOnce()
})

it('explains that deleting the extra folder preserves the safety point', async () => {
  vi.mocked(changeWorkspaceRetention).mockResolvedValueOnce({
    ...inventory, retained_bytes: 0,
    reviews: [inventory.reviews[0], { ...inventory.reviews[1], retained_bytes: 0,
      retained_discarded_at: '2026-09-10T12:00:00Z', can_archive: true }],
  })
  render(<WorkspaceRetentionPanel projectId="project_1" onChanged={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Recovery history management' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Discard retained workspace folder' }))
  expect(screen.getByText('The extra retained folder will be deleted. Its safety recovery point remains available.')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(screen.getAllByRole('button', { name: 'Discard retained workspace folder' })[0])
  await screen.findByText('Retained folder discarded; safety point kept')
  expect(changeWorkspaceRetention).toHaveBeenCalledWith(
    'project_1', 'copy', 'restore_2', 'd'.repeat(64), expect.any(AbortSignal),
  )
})

it('archives an applied review after the retained folder was discarded', async () => {
  vi.mocked(getWorkspaceRetention).mockResolvedValueOnce({ ...inventory,
    retained_bytes: 0, reviews: [{ ...inventory.reviews[1], retained_bytes: 0,
      retained_discarded_at: '2026-09-10T12:00:00Z', can_archive: true }],
  })
  vi.mocked(changeWorkspaceRetention).mockResolvedValueOnce({
    ...inventory, retained_bytes: 0, reviews: [], archived_reviews: 1,
  })
  render(<WorkspaceRetentionPanel projectId="project_1" onChanged={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Recovery history management' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Archive applied review' }))
  expect(screen.getByText('The applied review will move to the archive. Its result remains available by ID; recovery points can then be removed separately.')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(screen.getAllByRole('button', { name: 'Archive applied review' })[0])
  await waitFor(() => expect(changeWorkspaceRetention).toHaveBeenCalledWith(
    'project_1', 'archive', 'restore_2', 'd'.repeat(64), expect.any(AbortSignal),
  ))
  expect(screen.getByText(/1 archived reviews/)).toBeInTheDocument()
})

it('requires explicit confirmation before reclaiming unreferenced snapshots', async () => {
  vi.mocked(changeWorkspaceRetention).mockResolvedValueOnce({
    ...inventory, snapshot_files: 2, snapshot_bytes: 200,
    reclaimable_files: 0, reclaimable_bytes: 0,
  })
  render(<WorkspaceRetentionPanel projectId="project_1" onChanged={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Recovery history management' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Reclaim unreferenced snapshots' }))
  expect(screen.getByText('Only snapshots with no artifact or recovery point reference will be deleted. Review the file count and byte total above.')).toBeInTheDocument()
  expect(screen.getAllByRole('button', { name: 'Reclaim unreferenced snapshots' })[1]).toBeDisabled()
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(screen.getAllByRole('button', { name: 'Reclaim unreferenced snapshots' })[1])
  await waitFor(() => expect(changeWorkspaceRetention).toHaveBeenCalledWith(
    'project_1', 'blobs', '1 snapshot files', 'e'.repeat(64), expect.any(AbortSignal),
  ))
})

it('shows a scoped permission explanation in Chinese', async () => {
  vi.mocked(getWorkspaceRetention).mockRejectedValue({ status: 403 })
  render(<I18nContext.Provider value="zh-CN"><WorkspaceRetentionPanel projectId="project_1" onChanged={vi.fn()} /></I18nContext.Provider>)
  fireEvent.click(screen.getByRole('button', { name: '恢复历史管理' }))
  await screen.findByText('仅项目所有者或管理员可管理恢复历史。')
  expect(changeWorkspaceRetention).not.toHaveBeenCalled()
})

it('clears a previously loaded retention list after access is revoked', async () => {
  vi.mocked(getWorkspaceRetention).mockResolvedValueOnce(inventory)
    .mockRejectedValueOnce({ status: 403 })
  render(<WorkspaceRetentionPanel projectId="project_1" onChanged={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Recovery history management' }))
  await screen.findByText('Used by 1 saved reviews')
  fireEvent.click(screen.getByRole('button', { name: 'Refresh recovery history' }))
  await screen.findByText('Only a project owner or administrator can manage recovery history.')
  expect(screen.queryByText('Used by 1 saved reviews')).not.toBeInTheDocument()
})
