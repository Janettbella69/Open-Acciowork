import { afterEach, describe, expect, it, vi } from 'vitest'
import { getSkillProposal, getSkillProposalFile, listSkillProposals, reviewSkillProposal, skillProposalDownloadUrl } from './client'

afterEach(() => vi.unstubAllGlobals())

describe('Skill proposal HTTP contract', () => {
  it('reads bounded state pages and forwards opaque cursors and cancellation without caches', async () => {
    const payload = { items: [], next_cursor: 'opaque:/+?&' }
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(payload)))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()
    await expect(listSkillProposals('pending', null, controller.signal)).resolves.toEqual(payload)
    await listSkillProposals('rejected', payload.next_cursor, controller.signal)
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/skill-proposals?state=pending&limit=50',
      expect.objectContaining({ cache: 'no-store', signal: controller.signal }))
    const [url, init] = fetchMock.mock.calls[1] as unknown as [string, RequestInit]
    expect(new URL(url, 'http://localhost').searchParams.get('cursor')).toBe(payload.next_cursor)
    expect(new URL(url, 'http://localhost').searchParams.get('state')).toBe('rejected')
    expect(init.body).toBeUndefined()
    expect(init.method ?? 'GET').toBe('GET')
  })

  it('binds encoded detail, preview and attachment URLs to the exact frozen identity', async () => {
    const fetchMock = vi.fn(async () => new Response('{}'))
    vi.stubGlobal('fetch', fetchMock)
    const id = 'proposal/one ?#'
    const path = 'references/a & b?#.md'
    const hash = 'a'.repeat(64)
    const signal = new AbortController().signal
    await getSkillProposal(id, signal)
    await getSkillProposalFile(id, path, hash, signal)
    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/skill-proposals/proposal%2Fone%20%3F%23',
      expect.objectContaining({ cache: 'no-store', signal }))
    const previewUrl = new URL((fetchMock.mock.calls[1] as unknown as [string])[0], 'http://localhost')
    const attachment = new URL(skillProposalDownloadUrl(id, path, hash), 'http://localhost')
    expect(previewUrl.pathname).toBe('/api/skill-proposals/proposal%2Fone%20%3F%23/files')
    expect(attachment.pathname).toBe('/api/skill-proposals/proposal%2Fone%20%3F%23/download')
    for (const url of [previewUrl, attachment]) {
      expect([...url.searchParams]).toEqual([['path', path], ['content_hash', hash]])
    }
  })

  it('posts only the human decision and reviewed compare-and-swap values exactly once', async () => {
    const response = { state: 'published', revision: 3, publication_receipt: { proposal_id: 'proposal_1' } }
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(response)))
    vi.stubGlobal('fetch', fetchMock)
    const request = { decision: 'approve' as const, revision: 1, content_hash: 'a'.repeat(64), note: 'Reviewed',
      expected_target: { current_hash: 'b'.repeat(64), enabled: true, generation: 5 } }
    await expect(reviewSkillProposal('proposal/1', request)).resolves.toEqual(response)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock).toHaveBeenCalledWith('/api/skill-proposals/proposal%2F1/review',
      expect.objectContaining({ method: 'POST', body: JSON.stringify(request) }))
  })

  it.each([403, 409])('preserves review HTTP %s for the UI and never retries', async (status) => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ detail: 'Review unavailable' }), { status }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(reviewSkillProposal('proposal_1', { decision: 'reject', revision: 1,
      content_hash: 'a'.repeat(64), note: 'Needs revision' })).rejects.toMatchObject({ status, message: 'Review unavailable' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('preserves native cancellation instead of replacing it with a review or permission result', async () => {
    const aborted = new DOMException('Cancelled', 'AbortError')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(aborted))
    await expect(getSkillProposal('proposal_1', new AbortController().signal)).rejects.toBe(aborted)
  })
})
