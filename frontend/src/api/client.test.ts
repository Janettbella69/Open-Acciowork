import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  applyCommercialAction,
  cancelCommercialAction,
  createSchedule,
  getCommercialAction,
  getProjectMetrics,
  getProjectKnowledge,
  getProject,
  getRunTelemetry,
  listMemory,
  retryRun,
  resumePermissionRun,
  reviewCommercialAction,
  startRun,
} from './client'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('API client', () => {
  it('encodes the memory source Run and reads without browser caching', async () => {
    const controller = new AbortController()
    const fetchMock = vi.fn(async () => new Response('[]', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(listMemory('run/a b', controller.signal)).resolves.toEqual([])
    expect(fetchMock).toHaveBeenCalledWith('/api/memory?source_run_id=run%2Fa%20b', expect.objectContaining({
      signal: controller.signal, cache: 'no-store',
    }))
  })
  it('encodes reference search input and disables cached project results', async () => {
    const payload = { project_id: 'project/one', matches: [] }
    const controller = new AbortController()
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(getProjectKnowledge('project/one', '报价 & MOQ', controller.signal)).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith('/api/projects/project%2Fone/knowledge?q=%E6%8A%A5%E4%BB%B7+%26+MOQ&limit=30',
      expect.objectContaining({ signal: controller.signal, cache: 'no-store' }))
  })
  it('re-reads the encoded project ledger without a cached response and forwards cancellation', async () => {
    const payload = { id: 'project/one ?#' }
    const controller = new AbortController()
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(getProject(payload.id, controller.signal)).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith('/api/projects/project%2Fone%20%3F%23',
      expect.objectContaining({ signal: controller.signal, cache: 'no-store' }))
  })
  it('encodes the exact project metrics path and forwards its cancellation signal', async () => {
    const payload = { schema_version: '1', project_id: 'project/one ?#' }
    const controller = new AbortController()
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(payload), { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(getProjectMetrics('project/one ?#', 30, controller.signal)).resolves.toEqual(payload)

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/projects/project%2Fone%20%3F%23/metrics?days=30',
      expect.objectContaining({ signal: controller.signal }),
    )
    const init = (fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1]
    expect(init.body).toBeUndefined()
    expect(init.method ?? 'GET').toBe('GET')
  })

  it('preserves metrics access-denied status and native request cancellation', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      JSON.stringify({ detail: 'Project access denied' }), { status: 403 },
    )))
    await expect(getProjectMetrics('project_1', 7)).rejects.toMatchObject({
      status: 403, message: 'Project access denied',
    })

    const aborted = new DOMException('The request was aborted.', 'AbortError')
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(aborted))
    await expect(getProjectMetrics('project_1', 7, new AbortController().signal)).rejects.toBe(aborted)
  })

  it('resumes a saved approval with a bodyless POST to the exact encoded run', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ id: 'run/1', status: 'queued' }), { status: 202 }))
    vi.stubGlobal('fetch', fetchMock)
    await expect(resumePermissionRun('run/1')).resolves.toEqual({ id: 'run/1', status: 'queued' })
    expect(fetchMock).toHaveBeenCalledWith('/api/runs/run%2F1/resume-permission', expect.objectContaining({ method: 'POST' }))
    expect((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].body).toBeUndefined()
  })
  it('reads run telemetry from the encoded durable-run endpoint', async () => {
    const payload = { schema_version: '1', run_id: 'run/1' }
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => payload,
    } as Response))
    vi.stubGlobal('fetch', fetchMock)

    await expect(getRunTelemetry('run/1')).resolves.toBe(payload)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/runs/run%2F1/telemetry',
      expect.objectContaining({ headers: expect.any(Object) }),
    )
  })

  it('turns FastAPI detail arrays into actionable field messages', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false,
      status: 422,
      json: async () => ({
        detail: [
          { loc: ['body', 'prompt'], msg: 'Field required', type: 'missing' },
          { loc: ['body', 'mode'], msg: 'Input should be standard or deep_research', type: 'literal_error' },
        ],
      }),
    } as Response)))

    await expect(startRun('project_1', '')).rejects.toThrow(
      'body.prompt: Field required; body.mode: Input should be standard or deep_research',
    )
  })

  it('sends only the typed retry strategy to the persisted-run endpoint', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 202,
      json: async () => ({ id: 'run_retry' }),
    } as Response))
    vi.stubGlobal('fetch', fetchMock)

    await retryRun('run/source id', 'new_branch')

    expect(fetchMock).toHaveBeenCalledWith('/api/runs/run%2Fsource%20id/retry', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ strategy: 'new_branch' }),
    }))
  })

  it('serializes a bounded schedule permission ceiling without expanding it', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 201,
      json: async () => ({ id: 'schedule_1' }),
    } as Response))
    vi.stubGlobal('fetch', fetchMock)
    const permissionCeiling = [
      { tool_name: 'Write', scope: 'path:.' },
      { tool_name: 'Edit', scope: 'path:.' },
    ]

    await createSchedule({
      project_id: 'project_1',
      prompt: 'Prepare the report',
      cron: '0 8 * * *',
      timezone: 'UTC',
      kind: 'task',
      permission_ceiling: permissionCeiling,
    })

    expect(fetchMock).toHaveBeenCalledWith('/api/schedules', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({
        project_id: 'project_1',
        prompt: 'Prepare the report',
        cron: '0 8 * * *',
        timezone: 'UTC',
        kind: 'task',
        permission_ceiling: permissionCeiling,
      }),
    }))
  })

  it('uses the human control-plane commercial action endpoints and bounded approval', async () => {
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => ({ id: 'action_1' }),
    } as Response))
    vi.stubGlobal('fetch', fetchMock)

    await getCommercialAction('project/1', 'action/1')
    await reviewCommercialAction('project/1', 'action/1', 'approve', 'Reviewed target')
    await reviewCommercialAction('project/1', 'action/1', 'reject', 'Wrong target')
    await applyCommercialAction('project/1', 'action/1', 'Commit reviewed diff')
    await cancelCommercialAction('project/1', 'action/1', 'Withdraw proposal')

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/projects/project%2F1/commercial-actions/action%2F1',
      expect.objectContaining({ headers: expect.any(Object) }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/projects/project%2F1/commercial-actions/action%2F1/review',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          decision: 'approve',
          reason: 'Reviewed target',
          valid_for_seconds: 3_600,
        }),
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      '/api/projects/project%2F1/commercial-actions/action%2F1/review',
      expect.objectContaining({
        body: JSON.stringify({ decision: 'reject', reason: 'Wrong target' }),
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      '/api/projects/project%2F1/commercial-actions/action%2F1/apply',
      expect.objectContaining({ body: JSON.stringify({ reason: 'Commit reviewed diff' }) }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      '/api/projects/project%2F1/commercial-actions/action%2F1/cancel',
      expect.objectContaining({ body: JSON.stringify({ reason: 'Withdraw proposal' }) }),
    )
  })
})
