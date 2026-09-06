import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from './client'
import type { OpportunityProject, OphiraEvent, PendingPermissionView, ProtocolMetadata, RunRecord } from './types'
import { useOpportunityRuntime } from './useOpportunityRuntime'

vi.mock('./client')

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((complete) => { resolve = complete })
  return { promise, resolve }
}

const projectA = { id: 'project_a', name: 'A' } as OpportunityProject
const projectB = { id: 'project_b', name: 'B' } as OpportunityProject
const runA = { id: 'run_a', project_id: projectA.id, status: 'running' } as RunRecord
const runB = { id: 'run_b', project_id: projectB.id, status: 'running' } as RunRecord
const protocol = {
  event_types: ['run.completed', 'assistant.text.delta', 'tool.completed', 'permission.requested', 'permission.resolved'],
} as ProtocolMetadata

class MockEventSource {
  static instances: MockEventSource[] = []
  listeners = new Map<string, EventListener>()
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  closed = false

  constructor(public url: string) { MockEventSource.instances.push(this) }
  addEventListener(type: string, listener: EventListener) { this.listeners.set(type, listener) }
  close() { this.closed = true }
  emit(type: string, data: Record<string, unknown>) {
    const event = { type, run_id: runA.id, project_id: projectA.id, data } as OphiraEvent
    this.listeners.get(type)?.({ data: JSON.stringify(event) } as MessageEvent<string>)
  }
}

beforeEach(() => {
  vi.resetAllMocks()
  MockEventSource.instances = []
  vi.stubGlobal('EventSource', MockEventSource)
  vi.mocked(api.getProtocol).mockResolvedValue(protocol)
  vi.mocked(api.errorMessage).mockImplementation((cause) => cause instanceof Error ? cause.message : 'Unknown failure')
  vi.mocked(api.listProjects).mockResolvedValue([projectA, projectB])
  vi.mocked(api.listPendingPermissions).mockResolvedValue([])
  vi.mocked(api.listProjectRuns).mockImplementation(async (id) => [id === projectA.id ? runA : runB])
  vi.mocked(api.getProject).mockImplementation(async (id) => id === projectA.id ? projectA : projectB)
  vi.mocked(api.getRun).mockResolvedValue({ ...runA, status: 'completed' })
  vi.mocked(api.getRunTelemetry).mockRejectedValue(new Error('No telemetry fixture'))
  vi.mocked(api.runEventUrl).mockImplementation((id) => `/api/runs/${id}/events`)
})

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

async function setup() {
  const hook = renderHook(() => useOpportunityRuntime())
  await waitFor(() => expect(hook.result.current.projects).toHaveLength(2))
  await act(async () => { await hook.result.current.selectProject(projectA) })
  return hook
}

describe('project selection isolation', () => {
  it('ignores an old project refresh after selecting another project', async () => {
    const { result } = await setup()
    const refresh = deferred<OpportunityProject>()
    vi.mocked(api.getProject).mockReturnValueOnce(refresh.promise)
    act(() => {
      MockEventSource.instances[0].emit('tool.completed', { tool_name: 'mcp__ophira_ledger__write' })
    })
    await act(async () => { await result.current.selectProject(projectB) })
    await act(async () => { refresh.resolve(projectA) })
    expect(result.current.project?.id).toBe(projectB.id)
    expect(result.current.run?.id).toBe(runB.id)
  })

  it('ignores a terminal run refresh after selecting another run', async () => {
    const { result } = await setup()
    const refresh = deferred<RunRecord>()
    vi.mocked(api.getRun).mockReturnValueOnce(refresh.promise)
    act(() => { MockEventSource.instances[0].emit('run.completed', { status: 'completed' }) })
    await act(async () => { await result.current.selectProject(projectB) })
    await act(async () => { refresh.resolve({ ...runA, status: 'completed' }) })
    expect(result.current.run?.id).toBe(runB.id)
    expect(result.current.isActive).toBe(true)
  })

  it('does not resurrect a project when a start request finishes after reset', async () => {
    const { result } = await setup()
    const creation = deferred<OpportunityProject>()
    vi.mocked(api.createProject).mockReturnValueOnce(creation.promise)
    let pending!: ReturnType<typeof result.current.start>
    act(() => { pending = result.current.start('Start another project') })
    act(() => { result.current.reset() })
    await act(async () => { creation.resolve(projectA); await pending })
    expect(result.current.project).toBeNull()
    expect(result.current.run).toBeNull()
    expect(api.startRun).not.toHaveBeenCalled()
  })

  it('keeps a newer stream open when older protocol discovery finishes late', async () => {
    const { result } = await setup()
    const discovery = deferred<ProtocolMetadata>()
    vi.mocked(api.getProtocol).mockReturnValueOnce(discovery.promise)
    let selecting!: ReturnType<typeof result.current.selectProject>
    act(() => { selecting = result.current.selectProject(projectA) })
    await waitFor(() => expect(api.getProtocol).toHaveBeenCalledTimes(3))
    await act(async () => { await result.current.selectProject(projectB) })
    const sourceB = MockEventSource.instances.at(-1)!
    await act(async () => { discovery.resolve(protocol); await selecting })
    expect(sourceB.url).toContain(runB.id)
    expect(sourceB.closed).toBe(false)
    expect(MockEventSource.instances).toHaveLength(2)
    expect(result.current.run?.id).toBe(runB.id)
  })

  it('ignores cancellation and queued stream callbacks from the old selection', async () => {
    const { result } = await setup()
    const sourceA = MockEventSource.instances[0]
    const cancellation = deferred<RunRecord>()
    vi.mocked(api.cancelRun).mockReturnValueOnce(cancellation.promise)
    let cancelling!: Promise<void>
    act(() => { cancelling = result.current.cancel() })
    await act(async () => { await result.current.selectProject(projectB) })
    act(() => {
      sourceA.emit('assistant.text.delta', { text: 'Old project answer' })
      sourceA.emit('run.completed', { status: 'completed' })
      sourceA.onopen?.()
      sourceA.onerror?.()
    })
    await act(async () => { cancellation.resolve({ ...runA, status: 'cancelled' }); await cancelling })
    expect(result.current.project?.id).toBe(projectB.id)
    expect(result.current.run?.id).toBe(runB.id)
    expect(result.current.assistantText).toBe('')
    expect(result.current.connection).toBe('connecting')
    expect(MockEventSource.instances.at(-1)?.closed).toBe(false)
  })

  it('does not open a stream after unmount during protocol discovery', async () => {
    const { result, unmount } = await setup()
    const discovery = deferred<ProtocolMetadata>()
    vi.mocked(api.getProtocol).mockReturnValueOnce(discovery.promise)
    let selecting!: ReturnType<typeof result.current.selectProject>
    act(() => { selecting = result.current.selectProject(projectB) })
    await waitFor(() => expect(api.getProtocol).toHaveBeenCalledTimes(3))
    unmount()
    await act(async () => { discovery.resolve(protocol); await selecting })
    expect(MockEventSource.instances).toHaveLength(1)
    expect(MockEventSource.instances[0].closed).toBe(true)
  })
})

describe('durable approval reconciliation', () => {
  const pending: PendingPermissionView = {
    id: 'approval_1', project_id: projectA.id, run_id: runA.id, tool_name: 'Bash', tool_input: { command: 'touch report.txt' },
    agent_id: null, created_at: '2026-09-05T00:00:00Z', timeout_seconds: 3600, destructive: false, persistable: false, scope: 'report.txt',
    durable: true, state: 'pending', expires_at: '2099-01-01T00:00:00Z', tool_use_id: 'tool_1',
  }

  it('restores durable approvals from the authoritative ledger, scoped to the selected run', async () => {
    vi.mocked(api.listPendingPermissions).mockResolvedValue([pending, { ...pending, id: 'other', run_id: runB.id, project_id: projectB.id }])
    const { result } = await setup()
    await waitFor(() => expect(result.current.permissions).toHaveLength(1))
    expect(result.current.permissions[0]).toMatchObject({ id: pending.id, durable: true, state: 'pending', toolUseId: 'tool_1', expiresAt: pending.expires_at })
    expect(api.resolvePermission).not.toHaveBeenCalled()
  })

  it('preserves a saved decision after 409 and only resumes it when explicitly requested', async () => {
    vi.mocked(api.listPendingPermissions).mockResolvedValue([pending])
    const { result } = await setup()
    await waitFor(() => expect(result.current.permissions).toHaveLength(1))
    vi.mocked(api.resolvePermission).mockRejectedValueOnce(Object.assign(new Error('Runtime still stopping'), { status: 409 }))
    vi.mocked(api.listPendingPermissions).mockResolvedValue([{ ...pending, state: 'resolved' }])
    await act(async () => { await result.current.decidePermission(pending.id, 'allow_once') })
    expect(result.current.error).toContain('Refresh approvals and retry')
    expect(result.current.error).toContain('Runtime still stopping')
    expect(result.current.permissions[0].state).toBe('resolved')
    expect(api.resumePermissionRun).not.toHaveBeenCalled()
    vi.mocked(api.resumePermissionRun).mockRejectedValueOnce(Object.assign(new Error('Resume not ready'), { status: 409 }))
    await act(async () => { await result.current.resumePermission(pending.id) })
    expect(result.current.permissions[0].state).toBe('resolved')
    expect(result.current.error).toContain('Resume not ready')
    vi.mocked(api.listPendingPermissions).mockResolvedValue([])
    vi.mocked(api.resumePermissionRun).mockResolvedValue({ ...runA, status: 'queued' })
    await act(async () => { await result.current.resumePermission(pending.id) })
    expect(api.resumePermissionRun).toHaveBeenCalledTimes(2)
    expect(api.resumePermissionRun).toHaveBeenCalledWith(runA.id)
    expect(result.current.run?.status).toBe('queued')
    expect(result.current.permissions).toEqual([])
    expect(api.retryRun).not.toHaveBeenCalled()
  })

  it('blocks decisions and continuation for expired or uncertain durable state', async () => {
    vi.mocked(api.listPendingPermissions).mockResolvedValue([{ ...pending, state: 'resolved', expires_at: '2020-01-01T00:00:00Z' }])
    const { result } = await setup()
    await waitFor(() => expect(result.current.permissions).toHaveLength(1))
    await act(async () => {
      await result.current.decidePermission(pending.id, 'allow_once')
      await result.current.resumePermission(pending.id)
    })
    vi.mocked(api.listPendingPermissions).mockResolvedValue([{ ...pending, state: 'uncertain' }])
    await act(async () => { await result.current.refreshPermissions(); await result.current.resumePermission(pending.id) })
    expect(api.resolvePermission).not.toHaveBeenCalled()
    expect(api.resumePermissionRun).not.toHaveBeenCalled()
  })

  it('does not overwrite a new decision with a delayed permission snapshot', async () => {
    vi.mocked(api.listPendingPermissions).mockResolvedValue([pending])
    const { result } = await setup()
    await waitFor(() => expect(result.current.permissions).toHaveLength(1))
    const old = deferred<PendingPermissionView[]>()
    vi.mocked(api.listPendingPermissions).mockReturnValueOnce(old.promise)
    let refresh!: Promise<void>
    act(() => { refresh = result.current.refreshPermissions() })
    vi.mocked(api.resolvePermission).mockResolvedValue(undefined)
    vi.mocked(api.listPendingPermissions).mockResolvedValue([])
    await act(async () => { await result.current.decidePermission(pending.id, 'allow_once') })
    await act(async () => { old.resolve([pending]); await refresh })
    expect(result.current.permissions).toEqual([])
  })

  it('never applies an old run continuation after selecting another project', async () => {
    vi.mocked(api.listPendingPermissions).mockResolvedValue([{ ...pending, state: 'resolved' }])
    const { result } = await setup()
    await waitFor(() => expect(result.current.permissions).toHaveLength(1))
    const continuation = deferred<RunRecord>()
    vi.mocked(api.resumePermissionRun).mockReturnValueOnce(continuation.promise)
    let operation!: Promise<void>
    act(() => { operation = result.current.resumePermission(pending.id) })
    await act(async () => { await result.current.selectProject(projectB) })
    await act(async () => { continuation.resolve({ ...runA, status: 'queued' }); await operation })
    expect(result.current.run?.id).toBe(runB.id)
    expect(result.current.project?.id).toBe(projectB.id)
  })

  it('does not regress a completed stream to queued when the continuation response arrives late', async () => {
    vi.mocked(api.listPendingPermissions).mockResolvedValue([{ ...pending, state: 'resolved' }])
    const { result } = await setup()
    await waitFor(() => expect(result.current.permissions).toHaveLength(1))
    const continuation = deferred<RunRecord>()
    vi.mocked(api.resumePermissionRun).mockReturnValueOnce(continuation.promise)
    let operation!: Promise<void>
    act(() => { operation = result.current.resumePermission(pending.id) })
    await act(async () => { MockEventSource.instances[0].emit('run.completed', { status: 'completed' }) })
    expect(result.current.run?.status).toBe('completed')
    await act(async () => { continuation.resolve({ ...runA, status: 'queued' }); await operation })
    expect(result.current.run?.status).toBe('completed')
    expect(result.current.isActive).toBe(false)
    expect(result.current.permissions).toEqual([])
  })

  it('reconciles durable resolved SSE events instead of removing an approval that still awaits continuation', async () => {
    vi.mocked(api.listPendingPermissions).mockResolvedValue([pending])
    const { result } = await setup()
    await waitFor(() => expect(result.current.permissions).toHaveLength(1))
    vi.mocked(api.listPendingPermissions).mockResolvedValue([{ ...pending, state: 'resolved' }])
    await act(async () => { MockEventSource.instances[0].emit('permission.resolved', { request_id: pending.id, durable: true, tool_use_id: 'tool_1' }) })
    expect(result.current.permissions[0].state).toBe('resolved')
    expect(api.resumePermissionRun).not.toHaveBeenCalled()
  })
})
