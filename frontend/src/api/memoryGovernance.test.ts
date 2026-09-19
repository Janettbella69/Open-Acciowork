import { afterEach, describe, expect, it, vi } from 'vitest'
import { deleteMemory, exportMemory, listMemory, updateMemoryExpiry } from './client'

afterEach(() => vi.unstubAllGlobals())

describe('memory governance API', () => {
  it('requests all authorized memories without caching and carries cancellation', async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response('[]'))
      .mockResolvedValueOnce(new Response('{"count":0,"items":[],"tombstones":[]}'))
    vi.stubGlobal('fetch', fetchMock)
    const signal = new AbortController().signal
    await listMemory(signal)
    await exportMemory(signal)
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/memory', expect.objectContaining({ signal, cache: 'no-store' }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/memory/export', expect.objectContaining({ signal, cache: 'no-store' }))
  })

  it('encodes the item identity and sends the explicit deletion reason', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}'))
    vi.stubGlobal('fetch', fetchMock)
    await deleteMemory('mem/a', 'No longer needed')
    expect(fetchMock).toHaveBeenCalledWith('/api/memory/mem%2Fa', expect.objectContaining({
      method: 'DELETE', cache: 'no-store', body: JSON.stringify({ reason: 'No longer needed' }),
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
    }))
  })

  it.each([{ ttl_seconds: 86400 }, { clear_expiry: true } as const])('sends one expiry choice: %j', async (input) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}'))
    vi.stubGlobal('fetch', fetchMock)
    await updateMemoryExpiry('mem/a', input)
    expect(fetchMock).toHaveBeenCalledWith('/api/memory/mem%2Fa/expiry', expect.objectContaining({
      method: 'PATCH', cache: 'no-store', body: JSON.stringify(input),
    }))
  })

  it.each([403, 404, 503])('preserves status %s for privacy cleanup or retry', async (status) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{"detail":"Unavailable"}', { status })))
    await expect(deleteMemory('mem_1', 'No longer needed')).rejects.toMatchObject({ status, message: 'Unavailable' })
  })
})
