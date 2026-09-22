import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { deleteConversation, previewConversationDeletion } from '../api/client'
import type { ConversationDeletionPreview } from '../api/conversationErasure'
import { I18nContext } from '../i18n'
import ConversationDeletionPanel from './ConversationDeletionPanel'

vi.mock('../api/client', () => ({
  deleteConversation: vi.fn(), previewConversationDeletion: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))
const preview: ConversationDeletionPreview = {
  kind: 'conversation', project_id: 'project_1', selected_run_id: 'run_1', request_id: 'a'.repeat(32),
  reviewed_at: '2026-09-22T00:00:00Z', fingerprint: 'b'.repeat(64), run_ids: ['run_1', 'run_2'],
  file_count: 12, source_bytes: 1200, native_session_count: 1, memory_count: 2, retained: [],
}
const result = { status: 'deleted' as const, project_id: 'project_1', request_id: preview.request_id, run_ids: preview.run_ids }
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(previewConversationDeletion).mockResolvedValue(preview)
  vi.mocked(deleteConversation).mockResolvedValue(result)
})

it('requires reviewed scope and explicit irreversible confirmation, then clears every continuation', async () => {
  const onDeleted = vi.fn()
  render(<ConversationDeletionPanel projectId="project_1" runId="run_1" onDeleted={onDeleted} />)
  expect(previewConversationDeletion).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Review conversation deletion' }))
  expect(await screen.findByText('12', { exact: false })).toHaveTextContent('2 runs · 12 stored records/files · 2 source memories')
  const button = screen.getByRole('button', { name: 'Permanently delete conversation' })
  expect(button).toBeDisabled()
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(button); fireEvent.click(button)
  await waitFor(() => expect(onDeleted).toHaveBeenCalledWith('project_1', ['run_1', 'run_2']))
  expect(deleteConversation).toHaveBeenCalledExactlyOnceWith('project_1', preview)
})

it('retains the exact reviewed request after a lost response and does not claim cancellation', async () => {
  vi.mocked(deleteConversation).mockRejectedValueOnce(new Error('Network disconnected'))
  const onDeleted = vi.fn()
  render(<ConversationDeletionPanel projectId="project_1" runId="run_1" onDeleted={onDeleted} />)
  fireEvent.click(screen.getByRole('button', { name: 'Review conversation deletion' }))
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: 'Permanently delete conversation' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Network disconnected')
  expect(onDeleted).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Retry reviewed deletion' }))
  await waitFor(() => expect(onDeleted).toHaveBeenCalled())
  expect(vi.mocked(deleteConversation).mock.calls.map((call) => call[1])).toEqual([preview, preview])
})

it('ignores a late preview after navigation and preserves Chinese confirmation text', async () => {
  let resolve!: (value: ConversationDeletionPreview) => void
  vi.mocked(previewConversationDeletion).mockReturnValue(new Promise((done) => { resolve = done }))
  const view = render(<ConversationDeletionPanel projectId="project_1" runId="run_1" onDeleted={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: 'Review conversation deletion' }))
  view.rerender(<I18nContext.Provider value="zh-CN"><ConversationDeletionPanel projectId="project_2" runId="run_2" onDeleted={vi.fn()} /></I18nContext.Provider>)
  await act(async () => { resolve(preview) })
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: '核对会话删除范围' })).toBeEnabled()
})

it('does not abort a committed operation when the panel unmounts', async () => {
  let resolve!: (value: typeof result) => void
  vi.mocked(deleteConversation).mockReturnValue(new Promise((done) => { resolve = done }))
  const onDeleted = vi.fn()
  const view = render(<ConversationDeletionPanel projectId="project_1" runId="run_1" onDeleted={onDeleted} />)
  fireEvent.click(screen.getByRole('button', { name: 'Review conversation deletion' }))
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: 'Permanently delete conversation' }))
  view.unmount()
  await act(async () => { resolve(result) })
  expect(onDeleted).toHaveBeenCalledWith('project_1', ['run_1', 'run_2'])
})
