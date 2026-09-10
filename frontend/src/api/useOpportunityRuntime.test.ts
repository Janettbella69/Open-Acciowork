import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from './client'
import type { OpportunityProject, OphiraEvent, PendingPermissionView, ProtocolMetadata, RunRecord, RunStreamEnd } from './types'
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
  event_types: [
    'run.created', 'run.started', 'run.status', 'run.completed', 'run.failed', 'run.cancelled',
    'assistant.text.delta', 'tool.completed', 'permission.requested', 'permission.resolved',
    'evidence.added', 'decision.recorded', 'task.completed', 'provider.preflight',
  ],
  stream_end_event: 'stream.completed',
  run_statuses: ['queued', 'running', 'waiting_permission', 'paused', 'cancelling', 'completed', 'failed', 'cancelled'],
} as ProtocolMetadata

class MockEventSource {
  static instances: MockEventSource[] = []
  listeners = new Map<string, EventListener>()
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  closed = false
  sequence = 0

  constructor(public url: string) { MockEventSource.instances.push(this) }
  addEventListener(type: string, listener: EventListener) { this.listeners.set(type, listener) }
  close() { this.closed = true }
  emit(type: string, data: Record<string, unknown>, override: Partial<OphiraEvent> = {}) {
    const active = this.url.includes(runB.id) ? runB : runA
    const sequence = override.sequence ?? this.sequence + 1
    this.sequence = Math.max(this.sequence, sequence)
    const event = {
      type, run_id: active.id, project_id: active.project_id, data,
      sequence, id: `${active.id}:${sequence}`, ...override,
    } as OphiraEvent
    this.listeners.get(type)?.({ data: JSON.stringify(event) } as MessageEvent<string>)
  }
  end(override: Partial<RunStreamEnd> = {}) {
    const active = this.url.includes(runB.id) ? runB : runA
    const end = { run_id: active.id, project_id: active.project_id, last_sequence: this.sequence, ...override }
    this.listeners.get(protocol.stream_end_event)?.({ data: JSON.stringify(end) } as MessageEvent<string>)
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
  it('keeps a newly created template project when the initial project list arrives late and starts no run', async () => {
    const initial = deferred<OpportunityProject[]>()
    vi.mocked(api.listProjects).mockReturnValueOnce(initial.promise)
    vi.mocked(api.listProjectRuns).mockResolvedValue([])
    const created = { ...projectA, id: 'project_from_template' }
    const { result } = renderHook(() => useOpportunityRuntime())
    await act(async () => { await result.current.selectProject(created) })
    await act(async () => { initial.resolve([projectA, projectB]) })
    expect(result.current.project).toEqual(created)
    expect(result.current.projects.map((row) => row.id)).toEqual([created.id, projectA.id, projectB.id])
    expect(result.current.run).toBeNull()
    expect(api.startRun).not.toHaveBeenCalled()
    expect(MockEventSource.instances).toHaveLength(0)
  })
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
    act(() => {
      MockEventSource.instances[0].emit('run.completed', { status: 'completed' })
      MockEventSource.instances[0].end()
    })
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

describe('project record events and complete durable tails', () => {
  it.each(['evidence.added', 'decision.recorded', 'task.completed', 'provider.preflight', 'tool.completed'])(
    'does not apply %s status to the Run', async (type) => {
      const { result } = await setup()
      await act(async () => { MockEventSource.instances[0].emit(type, { status: 'failed', resource_id: 'record_1' }) })
      expect(result.current.run?.status).toBe('running')
      expect(result.current.isActive).toBe(true)
    },
  )

  it('accepts only valid lifecycle statuses and retains an authoritative terminal history status', async () => {
    const { result } = await setup()
    const source = MockEventSource.instances[0]
    act(() => { source.emit('run.status', { status: 'validated' }) })
    expect(result.current.run?.status).toBe('running')
    act(() => { source.emit('run.status', { status: 'waiting_permission' }) })
    expect(result.current.run?.status).toBe('waiting_permission')

    vi.mocked(api.listProjectRuns).mockResolvedValueOnce([{ ...runA, status: 'completed' }])
    await act(async () => { await result.current.selectProject(projectA) })
    act(() => { MockEventSource.instances.at(-1)!.emit('run.started', { status: 'running' }) })
    expect(result.current.run?.status).toBe('completed')
    expect(result.current.isActive).toBe(false)
    expect(result.current.historyLoading).toBe(true)
  })

  it('replays historical approval events without restoring actionable approvals on a terminal Run', async () => {
    vi.mocked(api.listProjectRuns).mockResolvedValueOnce([{ ...runA, status: 'completed' }])
    const { result } = await setup()
    const source = MockEventSource.instances[0]
    await act(async () => {
      source.emit('run.started', { status: 'running' })
      source.emit('permission.requested', { request_id: 'old_approval', tool_name: 'Bash', durable: true, state: 'pending' })
      source.emit('permission.resolved', { request_id: 'old_approval', durable: true })
      source.emit('run.completed', { status: 'completed' })
      source.end()
    })
    expect(result.current.run?.status).toBe('completed')
    expect(result.current.permissions).toEqual([])
    expect(result.current.events.some((event) => event.type === 'permission.requested')).toBe(true)
    expect(api.resolvePermission).not.toHaveBeenCalled()
  })

  it('ignores envelopes for another run or project before requesting any project ledger', async () => {
    const { result } = await setup()
    const source = MockEventSource.instances[0]
    act(() => {
      source.emit('evidence.added', { resource_id: 'foreign' }, { project_id: projectB.id })
      source.emit('decision.recorded', { resource_id: 'foreign' }, { run_id: runB.id })
    })
    expect(api.getProject).not.toHaveBeenCalled()
    expect(result.current.events).toEqual([])
    expect(result.current.project).toEqual(projectA)
  })

  it('coalesces record notifications and discards the response captured before a newer event', async () => {
    const { result } = await setup()
    const older = deferred<OpportunityProject>()
    const latest = deferred<OpportunityProject>()
    vi.mocked(api.getProject).mockReturnValueOnce(older.promise).mockReturnValueOnce(latest.promise)
    const source = MockEventSource.instances[0]
    act(() => {
      source.emit('evidence.added', { schema_version: 1, resource_id: 'evidence_1', status: 'validated' })
      source.emit('decision.recorded', { schema_version: 1, resource_id: 'decision_1' })
      source.emit('evidence.added', { schema_version: 1, resource_id: 'evidence_2' })
    })
    expect(api.getProject).toHaveBeenCalledTimes(1)
    expect(result.current.project).toEqual(projectA)
    await act(async () => { older.resolve({ ...projectA, name: 'Stale response' }) })
    expect(api.getProject).toHaveBeenCalledTimes(2)
    expect(result.current.project?.name).toBe('A')
    await act(async () => { latest.resolve({ ...projectA, name: 'Authoritative ledger', revision: 3 }) })
    expect(result.current.project?.name).toBe('Authoritative ledger')
    expect(result.current.project?.revision).toBe(3)
    expect(result.current.project?.evidence).toBeUndefined()
    expect(result.current.run?.status).toBe('running')
  })

  it('aborts the old request and ignores it even after returning to the same project', async () => {
    const { result } = await setup()
    const older = deferred<OpportunityProject>()
    vi.mocked(api.getProject).mockReturnValueOnce(older.promise)
    act(() => { MockEventSource.instances[0].emit('evidence.added', { resource_id: 'old' }) })
    const signal = vi.mocked(api.getProject).mock.calls[0][1]!
    await act(async () => { await result.current.selectProject(projectB) })
    expect(signal.aborted).toBe(true)
    await act(async () => { await result.current.selectProject(projectA) })
    vi.mocked(api.getProject).mockResolvedValueOnce({ ...projectA, name: 'Fresh A' })
    await act(async () => { MockEventSource.instances.at(-1)!.emit('decision.recorded', { resource_id: 'new' }) })
    await act(async () => { older.resolve({ ...projectA, name: 'Old A' }) })
    expect(result.current.project?.name).toBe('Fresh A')
    expect(result.current.projects.find((item) => item.id === projectA.id)?.name).toBe('Fresh A')
  })

  it('continues past an old terminal event and finishes history only after the complete tail is refreshed', async () => {
    vi.mocked(api.listProjectRuns).mockResolvedValueOnce([{ ...runA, status: 'completed' }])
    const { result } = await setup()
    const source = MockEventSource.instances[0]
    const first = deferred<OpportunityProject>()
    const final = deferred<OpportunityProject>()
    vi.mocked(api.getProject).mockReturnValueOnce(first.promise).mockReturnValueOnce(final.promise)
    act(() => {
      source.emit('run.completed', { status: 'completed' })
      source.emit('evidence.added', { schema_version: 1, resource_id: 'evidence_1' })
      source.emit('decision.recorded', { schema_version: 1, resource_id: 'decision_1' })
    })
    expect(source.closed).toBe(false)
    expect(result.current.historyLoading).toBe(true)
    expect(result.current.events.map((event) => event.type)).toEqual(['run.completed', 'evidence.added', 'decision.recorded'])
    act(() => { source.end() })
    expect(source.closed).toBe(true)
    expect(result.current.historyLoading).toBe(true)
    expect(result.current.events).toHaveLength(3)
    await act(async () => { first.resolve(projectA) })
    await act(async () => { final.resolve({ ...projectA, name: 'Complete business ledger' }) })
    expect(result.current.historyLoading).toBe(false)
    expect(result.current.connection).toBe('closed')
    expect(result.current.project?.name).toBe('Complete business ledger')
  })

  it('keeps reconnecting after execution completion and consumes resumed notifications only once', async () => {
    const { result } = await setup()
    const source = MockEventSource.instances[0]
    act(() => {
      source.emit('assistant.text.delta', { text: 'Saved answer.' })
      source.emit('run.completed', { status: 'completed' })
      source.onerror?.()
    })
    expect(result.current.connection).toBe('reconnecting')
    expect(source.closed).toBe(false)
    await act(async () => {
      source.onopen?.()
      source.emit('assistant.text.delta', { text: 'Saved answer.' }, { sequence: 1 })
      source.emit('run.completed', { status: 'completed' }, { sequence: 2 })
      source.emit('evidence.added', { resource_id: 'after_resume' })
    })
    expect(result.current.assistantText).toBe('Saved answer.')
    expect(result.current.events.map((event) => event.type)).toEqual(['run.completed', 'evidence.added'])
    expect(api.getProject).toHaveBeenCalledTimes(1)
    await act(async () => { source.end() })
    expect(source.closed).toBe(true)
  })

  it('rejects mismatched or unconsumed completion boundaries without closing the source', async () => {
    const { result } = await setup()
    const source = MockEventSource.instances[0]
    act(() => {
      source.emit('run.completed', { status: 'completed' })
      source.end({ run_id: runB.id })
      source.end({ project_id: projectB.id })
      source.end({ last_sequence: 2 })
      source.end({ last_sequence: -1 })
      source.end({ last_sequence: 1.5 })
    })
    expect(source.closed).toBe(false)
    expect(api.getRun).not.toHaveBeenCalled()
    expect(result.current.error).toContain('stream has a gap')
    await act(async () => { source.end() })
    expect(source.closed).toBe(true)
  })

  it('does not consider a higher sequence proof that missing events were consumed', async () => {
    const { result } = await setup()
    const source = MockEventSource.instances[0]
    act(() => {
      source.emit('assistant.text.delta', { text: 'First.' })
      source.emit('evidence.added', { resource_id: 'gap' }, { sequence: 3 })
      source.end()
    })
    expect(source.closed).toBe(false)
    expect(api.getProject).not.toHaveBeenCalled()
    expect(result.current.assistantText).toBe('First.')
  })

  it('finishes empty historical streams without inventing a completion event', async () => {
    vi.mocked(api.listProjectRuns).mockResolvedValueOnce([{ ...runA, status: 'failed' }])
    vi.mocked(api.getRun).mockResolvedValueOnce({ ...runA, status: 'failed' })
    const { result } = await setup()
    await act(async () => { MockEventSource.instances[0].end() })
    expect(result.current.run?.status).toBe('failed')
    expect(result.current.events).toEqual([])
    expect(result.current.historyLoading).toBe(false)
  })

  it('preserves the last ledger view and reports a denied authoritative refresh', async () => {
    const { result } = await setup()
    vi.mocked(api.getProject).mockRejectedValueOnce(Object.assign(new Error('Denied'), { status: 403 }))
    await act(async () => { MockEventSource.instances[0].emit('evidence.added', { resource_id: 'never_fabricate', text: 'untrusted' }) })
    expect(result.current.project).toEqual(projectA)
    expect(result.current.project?.evidence).toBeUndefined()
    expect(result.current.error).toContain('Could not refresh the saved project')
    expect(result.current.run?.status).toBe('running')
  })

  it('ignores stale end callbacks and final project/run responses after a selection change', async () => {
    const { result } = await setup()
    const oldProject = deferred<OpportunityProject>()
    const oldRun = deferred<RunRecord>()
    vi.mocked(api.getProject).mockReturnValueOnce(oldProject.promise)
    vi.mocked(api.getRun).mockReturnValueOnce(oldRun.promise)
    const source = MockEventSource.instances[0]
    act(() => { source.end() })
    await act(async () => { await result.current.selectProject(projectB) })
    await act(async () => {
      source.end()
      oldProject.resolve({ ...projectA, name: 'Late A' })
      oldRun.resolve({ ...runA, status: 'completed' })
    })
    expect(result.current.project).toEqual(projectB)
    expect(result.current.run).toEqual(runB)
    expect(MockEventSource.instances.at(-1)?.closed).toBe(false)
  })
})
