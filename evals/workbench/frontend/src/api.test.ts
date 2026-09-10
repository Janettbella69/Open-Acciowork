import { afterEach, expect, it, vi } from 'vitest'
import { api, exportUrl, fileUrl } from './api'

afterEach(() => { vi.unstubAllGlobals() })

it('uses the standalone same-origin API for review writes and downloaded artifacts', async () => {
  const fetchMock = vi.fn(async () => Response.json({ revision: 2 }))
  vi.stubGlobal('fetch', fetchMock)
  const label = { label: false, rationale: 'Owner evidence review', owner_confirmed: true, revision: 1 }
  await api.label('gold-123', label)
  expect(fetchMock).toHaveBeenCalledWith('/api/acciowork/items/gold-123/label', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', 'X-Eval-Review': 'owner-ui' },
    body: JSON.stringify(label),
  })
  expect(fileUrl('abc123')).toBe('/api/acciowork/files/abc123')
  expect(exportUrl('evidence-coverage')).toBe('/api/acciowork/labels/evidence-coverage/export')
})

it('preserves conflict messages from the independent review service', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ detail: '复核版本已变化，请刷新' }, { status: 409 })))
  await expect(api.review('trace-123', { status: 'reviewed', open_codes: [], notes: '', revision: 0 }))
    .rejects.toThrow('复核版本已变化，请刷新')
})

it('saves a review again after reusing the server response with read-only audit metadata', async () => {
  let revision = 0
  const bodies: Record<string, unknown>[] = []
  vi.stubGlobal('fetch', vi.fn(async (_path: string, options: RequestInit) => {
    const body = JSON.parse(String(options.body)) as Record<string, unknown>
    bodies.push(body)
    const allowed = ['status', 'open_codes', 'notes', 'revision']
    if (Object.keys(body).some(key => !allowed.includes(key))) {
      return Response.json({ detail: 'Extra review fields are forbidden' }, { status: 422 })
    }
    if (body.revision !== revision) return Response.json({ detail: 'Stale revision' }, { status: 409 })
    revision += 1
    return Response.json({ ...body, revision, reviewed_at: '2026-09-10T06:00:00Z', annotator: 'local-reviewer' })
  }))

  const first = await api.review('trace-123', { status: 'reviewed', open_codes: ['missing-source'], notes: 'First review', revision: 0 })
  expect(first).toMatchObject({ revision: 1, reviewed_at: '2026-09-10T06:00:00Z', annotator: 'local-reviewer' })
  const second = await api.review('trace-123', { ...first, status: 'flagged', notes: 'Follow up on the source' })

  expect(second).toMatchObject({ revision: 2, status: 'flagged', notes: 'Follow up on the source' })
  expect(bodies).toEqual([
    { status: 'reviewed', open_codes: ['missing-source'], notes: 'First review', revision: 0 },
    { status: 'flagged', open_codes: ['missing-source'], notes: 'Follow up on the source', revision: 1 },
  ])
})
