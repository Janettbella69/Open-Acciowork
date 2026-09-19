import { afterEach, describe, expect, it, vi } from 'vitest'
import { downloadProjectExport, getProjectExport } from './client'

afterEach(() => vi.unstubAllGlobals())

describe('project export API', () => {
  it('scopes preview and binary requests, sends only the reviewed fingerprint and preserves bytes', async () => {
    const bytes = new Uint8Array([80, 75, 3, 4, 0, 255, 128])
    const fetchMock = vi.fn().mockResolvedValueOnce(new Response('{}', { status: 200 }))
      .mockResolvedValueOnce(new Response(bytes, { status: 200, headers: { 'Content-Type': 'application/zip' } }))
    vi.stubGlobal('fetch', fetchMock)
    const signal = new AbortController().signal
    await getProjectExport('project/one', signal)
    const blob = await downloadProjectExport('project/one', 'a'.repeat(64), signal)
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/projects/project%2Fone/export', expect.objectContaining({ signal, cache: 'no-store' }))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/projects/project%2Fone/export/download', {
      method: 'POST', signal, cache: 'no-store', body: JSON.stringify({ fingerprint: 'a'.repeat(64) }),
      headers: { Accept: 'application/zip', 'Content-Type': 'application/json' },
    })
    const content = await new Promise<ArrayBuffer>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(reader.result as ArrayBuffer)
      reader.onerror = () => reject(reader.error)
      reader.readAsArrayBuffer(blob)
    })
    expect(new Uint8Array(content)).toEqual(bytes)
  })

  it.each([403, 409])('retains API error status %s and details instead of downloading an error body', async (status) => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'Review again' }), { status })))
    await expect(downloadProjectExport('project_1', 'a'.repeat(64))).rejects.toMatchObject({ status, message: 'Review again' })
  })

  it.each([
    ['text/html', '<html>Sign in</html>', 'The project export response was not a ZIP archive.'],
    ['application/zip', '', 'The project export archive was empty.'],
  ])('rejects invalid download content %s', async (contentType, body, message) => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(body, { status: 200, headers: { 'Content-Type': contentType } })))
    await expect(downloadProjectExport('project_1', 'a'.repeat(64))).rejects.toThrow(message)
  })

  it('preserves cancellation instead of manufacturing a download', async () => {
    const abort = new DOMException('Aborted', 'AbortError')
    vi.stubGlobal('fetch', vi.fn(async () => { throw abort }))
    const controller = new AbortController()
    controller.abort()
    await expect(downloadProjectExport('project_1', 'a'.repeat(64), controller.signal)).rejects.toBe(abort)
  })
})
