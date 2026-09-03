import { afterEach, describe, expect, it, vi } from 'vitest'
import { createSchedule, retryRun, startRun } from './client'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('API validation errors', () => {
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
})
