import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getProjectDeletionOperation, retryProjectDeletion, submitProjectDeletion } from './client'
import { useProjectDeletion } from './useProjectDeletion'
import { deferred, deletionOperation, deletionOperationId, deletionRequest } from '../test/projectDeletionFixtures'
import type { ProjectDeletionOperation } from './projectDeletion'

vi.mock('./client', () => ({ getProjectDeletionOperation: vi.fn(), retryProjectDeletion: vi.fn(), submitProjectDeletion: vi.fn(),
  errorMessage: (cause: unknown) => cause instanceof Error ? cause.message : 'Unknown error' }))
beforeEach(() => {
  vi.resetAllMocks()
  window.history.replaceState({}, '', '/app')
  vi.mocked(submitProjectDeletion).mockResolvedValue(deletionOperation())
  vi.mocked(getProjectDeletionOperation).mockResolvedValue(deletionOperation())
  vi.mocked(retryProjectDeletion).mockResolvedValue(deletionOperation())
})
afterEach(() => { cleanup(); vi.useRealTimers(); window.history.replaceState({}, '', '/app') })

describe('useProjectDeletion', () => {
  it('persists only operation identifiers before sending once, and accepts blocked without claiming completion', async () => {
    window.history.replaceState({}, '', '/app?brief=Private%20brief&agent=agent_1')
    const pending = deferred<ProjectDeletionOperation>()
    vi.mocked(submitProjectDeletion).mockReturnValueOnce(pending.promise)
    const accepted = vi.fn()
    const { result } = renderHook(() => useProjectDeletion(accepted))
    act(() => { void result.current.submit('project_1', deletionRequest); void result.current.submit('project_1', deletionRequest) })
    expect(submitProjectDeletion).toHaveBeenCalledTimes(1)
    const params = new URLSearchParams(window.location.search)
    expect(params.get('deletion_project')).toBe('project_1')
    expect(params.get('deletion_operation')).toBe(deletionOperationId)
    expect(params.has('brief')).toBe(false)
    expect(window.location.search).not.toContain('Supplier')
    expect(window.location.search).not.toContain('needed')
    expect(accepted).not.toHaveBeenCalled()
    await act(async () => { pending.resolve(deletionOperation('blocked')) })
    expect(result.current.attempt?.operation?.status).toBe('blocked')
    expect(result.current.attempt?.request).toBeNull()
    expect(accepted).toHaveBeenCalledExactlyOnceWith(deletionOperation('blocked'))
  })

  it.each([403, 404, 409])('does not treat a definite POST %s as accepted or query it automatically', async (status) => {
    vi.mocked(submitProjectDeletion).mockRejectedValueOnce(Object.assign(new Error('Review changed'), { status }))
    const accepted = vi.fn()
    const accessCheck = vi.fn()
    const { result } = renderHook(() => useProjectDeletion(accepted, accessCheck))
    await act(async () => { await result.current.submit('project_1', deletionRequest) })
    expect(accepted).not.toHaveBeenCalled()
    expect(getProjectDeletionOperation).not.toHaveBeenCalled()
    expect(result.current.attempt?.operation).toBeNull()
    expect(result.current.attempt?.request).toBeNull()
    expect(result.current.attempt?.phase).toBe(status === 403 ? 'forbidden' : 'rejected')
    if (status === 403 || status === 404) expect(accessCheck).toHaveBeenCalledExactlyOnceWith('project_1')
    else expect(accessCheck).not.toHaveBeenCalled()
  })

  it('checks an uncertain POST before permitting the exact same request to be resent after 404', async () => {
    vi.mocked(submitProjectDeletion).mockRejectedValueOnce(new Error('Connection lost'))
    vi.mocked(getProjectDeletionOperation).mockRejectedValueOnce(Object.assign(new Error('Not found'), { status: 404 }))
    const accepted = vi.fn()
    const { result } = renderHook(() => useProjectDeletion(accepted))
    await act(async () => { await result.current.submit('project_1', deletionRequest) })
    expect(getProjectDeletionOperation).toHaveBeenCalledExactlyOnceWith('project_1', deletionOperationId, expect.any(AbortSignal))
    expect(result.current.attempt?.phase).toBe('missing')
    expect(accepted).not.toHaveBeenCalled()
    vi.mocked(submitProjectDeletion).mockResolvedValueOnce(deletionOperation('deleted'))
    await act(async () => { result.current.resend() })
    expect(submitProjectDeletion).toHaveBeenNthCalledWith(2, 'project_1', deletionRequest, expect.any(AbortSignal))
    expect(result.current.attempt?.operation?.status).toBe('deleted')
    expect(accepted).toHaveBeenCalledTimes(1)
    act(() => result.current.dismiss())
    expect(result.current.attempt).toBeNull()
    expect(window.location.search).toBe('')
  })

  it.each([undefined, 503])('recovers an uncertain POST (status %s) from the authoritative operation record', async (status) => {
    vi.mocked(submitProjectDeletion).mockRejectedValueOnce(Object.assign(new Error('Response lost'), { status }))
    vi.mocked(getProjectDeletionOperation).mockResolvedValueOnce(deletionOperation('deleting'))
    const accepted = vi.fn()
    const { result } = renderHook(() => useProjectDeletion(accepted))
    await act(async () => { await result.current.submit('project_1', deletionRequest) })
    expect(result.current.attempt?.accepted).toBe(true)
    expect(result.current.attempt?.operation?.status).toBe('deleting')
    expect(accepted).toHaveBeenCalledTimes(1)
  })

  it.each(['blocked', 'deleting'] as const)('restores a progress link once under StrictMode and retries only the same %s operation', async (status) => {
    window.history.replaceState({}, '', `/app?deletion_project=project_1&deletion_operation=${deletionOperationId}`)
    vi.mocked(getProjectDeletionOperation).mockResolvedValueOnce({ ...deletionOperation(status), can_retry: true })
    const accepted = vi.fn()
    const { result } = renderHook(() => useProjectDeletion(accepted), { wrapper: StrictMode })
    await waitFor(() => expect(result.current.attempt?.operation?.status).toBe(status))
    expect(getProjectDeletionOperation).toHaveBeenCalledTimes(1)
    await act(async () => { await result.current.retry() })
    expect(retryProjectDeletion).toHaveBeenCalledExactlyOnceWith('project_1', deletionOperationId, expect.any(AbortSignal))
    expect(submitProjectDeletion).not.toHaveBeenCalled()
    expect(accepted).toHaveBeenCalledTimes(1)
    expect(result.current.attempt?.operation?.status).toBe('deleting')
  })

  it.each([403, 404, 503])('handles progress status %s with appropriate private-state retention', async (status) => {
    const { result } = renderHook(() => useProjectDeletion(vi.fn()))
    await act(async () => { await result.current.submit('project_1', deletionRequest) })
    vi.mocked(getProjectDeletionOperation).mockRejectedValueOnce(Object.assign(new Error('Read failed'), { status }))
    await act(async () => { await result.current.refresh() })
    expect(result.current.attempt?.operation).toEqual(status === 503 ? deletionOperation() : null)
    expect(result.current.attempt?.accepted).toBe(true)
    act(() => result.current.resend())
    expect(submitProjectDeletion).toHaveBeenCalledTimes(1)
  })

  it('ignores a late accepted response after unmount while retaining its refreshable URL', async () => {
    const pending = deferred<ProjectDeletionOperation>()
    vi.mocked(submitProjectDeletion).mockReturnValueOnce(pending.promise)
    const accepted = vi.fn()
    const { result, unmount } = renderHook(() => useProjectDeletion(accepted))
    act(() => { void result.current.submit('project_1', deletionRequest) })
    const signal = vi.mocked(submitProjectDeletion).mock.calls[0][2]!
    unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => { pending.resolve(deletionOperation('deleted')) })
    expect(accepted).not.toHaveBeenCalled()
    expect(window.location.search).toContain(deletionOperationId)
  })

  it('rejects responses belonging to another project or operation', async () => {
    vi.mocked(submitProjectDeletion).mockResolvedValueOnce({ ...deletionOperation('deleted'), project_id: 'other_project' })
    vi.mocked(getProjectDeletionOperation).mockResolvedValueOnce({ ...deletionOperation('deleted'), operation_id: 'foreign_operation' })
    const accepted = vi.fn()
    const { result } = renderHook(() => useProjectDeletion(accepted))
    await act(async () => { await result.current.submit('project_1', deletionRequest) })
    expect(accepted).not.toHaveBeenCalled()
    expect(result.current.attempt?.operation).toBeNull()
    expect(result.current.attempt?.error).toBe('The deletion response did not match this operation.')
  })
})
