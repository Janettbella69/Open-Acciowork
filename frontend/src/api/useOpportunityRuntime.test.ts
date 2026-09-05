import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from './client'
import type { OpportunityProject, OphiraEvent, ProtocolMetadata, RunRecord } from './types'
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
  event_types: ['run.completed', 'assistant.text.delta', 'tool.completed'],
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
  vi.mocked(api.listProjects).mockResolvedValue([projectA, projectB])
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
