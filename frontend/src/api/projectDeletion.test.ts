import { afterEach, describe, expect, it, vi } from 'vitest'
import { getProjectDeletion, getProjectDeletionOperation, retryProjectDeletion, submitProjectDeletion } from './client'
import { deletionRequest } from '../test/projectDeletionFixtures'

afterEach(() => vi.unstubAllGlobals())
describe('project deletion API', () => {
  it('encodes both identifiers, preserves the reviewed payload and bypasses caches', async () => {
    const fetchMock = vi.fn<typeof fetch>(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const signal = new AbortController().signal
    await getProjectDeletion('project/one', signal)
    await submitProjectDeletion('project/one', deletionRequest, signal)
    await getProjectDeletionOperation('project/one', 'operation/one', signal)
    await retryProjectDeletion('project/one', 'operation/one', signal)
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/projects/project%2Fone/deletion', expect.objectContaining({ signal, cache: 'no-store' }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/projects/project%2Fone/deletion', expect.objectContaining({ method: 'POST', body: JSON.stringify(deletionRequest), signal, cache: 'no-store' }))
    expect(fetchMock).toHaveBeenNthCalledWith(3, '/api/project-deletions/project%2Fone/operation%2Fone', expect.objectContaining({ signal, cache: 'no-store' }))
    expect(fetchMock).toHaveBeenNthCalledWith(4, '/api/project-deletions/project%2Fone/operation%2Fone/retry', expect.objectContaining({ method: 'POST', signal, cache: 'no-store' }))
    expect(fetchMock.mock.calls[3][1]).not.toHaveProperty('body')
  })
  it.each([403, 404, 409, 503])('preserves status %s for recovery decisions', async (status) => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'Deletion not confirmed' }), { status })))
    await expect(submitProjectDeletion('project_1', deletionRequest)).rejects.toMatchObject({ status, message: 'Deletion not confirmed' })
  })
})
