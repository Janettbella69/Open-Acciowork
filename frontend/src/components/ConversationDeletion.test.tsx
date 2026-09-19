import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api/client'
import { useProjectDeletion } from '../api/useProjectDeletion'
import { deletionOperation, deletionOperationId, deletionPreview, deletionRequest } from '../test/projectDeletionFixtures'
import ProjectDeletionPanel from './ProjectDeletionPanel'
import ProjectDeletionProgress from './ProjectDeletionProgress'

vi.mock('../api/client')
const accepted = vi.fn()
const operation = (status: 'deleting' | 'blocked' | 'deleted' = 'deleted') => ({
  ...deletionOperation(status), run_id: 'run_a', run_ids: ['run_a', 'run_followup'],
})
function Harness() {
  const deletion = useProjectDeletion(accepted, undefined, 'conversation')
  return <>
    <ProjectDeletionPanel projectId="project_1" runId="run_a" pending={!!deletion.attempt}
      canReviewAgain={false} onReviewAgain={deletion.dismiss} onSubmit={deletion.submit} onAccessCheck={() => {}} />
    <ProjectDeletionProgress deletion={deletion} />
  </>
}
beforeEach(() => {
  vi.resetAllMocks()
  window.history.replaceState({}, '', '/app')
  vi.spyOn(crypto, 'randomUUID').mockReturnValue(deletionOperationId)
  vi.mocked(api.errorMessage).mockImplementation((cause) => cause instanceof Error ? cause.message : 'Unknown error')
  vi.mocked(api.getConversationDeletion).mockResolvedValue({ ...deletionPreview(), run_id: 'run_a', run_ids: ['run_a', 'run_followup'] })
  vi.mocked(api.submitConversationDeletion).mockResolvedValue(operation())
  vi.mocked(api.getConversationDeletionOperation).mockResolvedValue(operation())
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); window.history.replaceState({}, '', '/app') })

it('reviews a whole conversation, requires explicit confirmation and uses the conversation endpoint', async () => {
  render(<Harness />)
  fireEvent.click(screen.getByRole('button', { name: 'Delete conversation' }))
  await screen.findByText(/This deletes the entire conversation/)
  const submit = screen.getByRole('button', { name: 'Permanently delete conversation' })
  expect(submit).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Type the project name to confirm'), { target: { value: 'Supplier research' } })
  fireEvent.change(screen.getByLabelText('Deletion reason'), { target: { value: 'No longer needed' } })
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(submit)
  expect(await screen.findByText('The conversation has been deleted. Other conversations and retained project records remain available.')).toBeInTheDocument()
  expect(api.submitConversationDeletion).toHaveBeenCalledExactlyOnceWith('project_1', 'run_a', deletionRequest, expect.any(AbortSignal))
  expect(api.submitProjectDeletion).not.toHaveBeenCalled()
  expect(window.location.search).toContain('conversation_deletion_run=run_a')
  expect(window.location.search).not.toContain('Supplier')
  expect(accepted).toHaveBeenCalledExactlyOnceWith(operation())
})

it('restores the same conversation operation after reload and rejects an unrelated Run response', async () => {
  window.history.replaceState({}, '', `/app?conversation_deletion_project=project_1&conversation_deletion_operation=${deletionOperationId}&conversation_deletion_run=run_a`)
  vi.mocked(api.getConversationDeletionOperation).mockResolvedValueOnce({ ...operation(), run_id: 'run_wrong' })
  const { result } = renderHook(() => useProjectDeletion(accepted, undefined, 'conversation'))
  await waitFor(() => expect(result.current.attempt?.error).toContain('did not match this conversation'))
  expect(accepted).not.toHaveBeenCalled()
  await act(async () => { await result.current.refresh() })
  expect(accepted).toHaveBeenCalledExactlyOnceWith(operation())
  expect(api.submitConversationDeletion).not.toHaveBeenCalled()
})

it('resends only the same Run and operation after an ambiguous submit then missing status', async () => {
  vi.mocked(api.submitConversationDeletion).mockRejectedValueOnce(new Error('Connection lost'))
  vi.mocked(api.getConversationDeletionOperation).mockRejectedValueOnce(Object.assign(new Error('Missing'), { status: 404 }))
  const { result } = renderHook(() => useProjectDeletion(accepted, undefined, 'conversation'))
  await act(async () => { await result.current.submit('project_1', deletionRequest, 'run_a') })
  expect(result.current.attempt?.phase).toBe('missing')
  await act(async () => { result.current.resend() })
  expect(api.submitConversationDeletion).toHaveBeenCalledTimes(2)
  expect(api.submitConversationDeletion).toHaveBeenLastCalledWith('project_1', 'run_a', deletionRequest, expect.any(AbortSignal))
})

it('notifies the workspace when a blocked conversation deletion finishes', async () => {
  vi.mocked(api.submitConversationDeletion).mockResolvedValueOnce(operation('blocked'))
  vi.mocked(api.retryConversationDeletion).mockResolvedValueOnce(operation('deleted'))
  const { result } = renderHook(() => useProjectDeletion(accepted, undefined, 'conversation'))
  await act(async () => { await result.current.submit('project_1', deletionRequest, 'run_a') })
  await act(async () => { await result.current.retry() })
  expect(accepted.mock.calls.map(([item]) => item.status)).toEqual(['blocked', 'deleted'])
  expect(api.retryConversationDeletion).toHaveBeenCalledExactlyOnceWith('project_1', deletionOperationId, expect.any(AbortSignal))
})
