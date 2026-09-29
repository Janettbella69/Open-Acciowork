import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getScopedEventHistory } from './client'
import { useScopedEvents } from './useScopedEvents'
import type { ScopedEventHistory } from './scopedEvents'
import { scopedEvent, scopedHistory, ScopedEventSource } from '../test/scopedEventFixtures'

vi.mock('./client', () => ({
  getScopedEventHistory: vi.fn(),
  scopedEventUrl: (scope: string, id: string, after: number) => `/${scope}/${id}/events?after=${after}`,
  errorMessage: (cause: unknown) => cause instanceof Error ? cause.message : 'Connection failed',
}))

beforeEach(() => {
  vi.clearAllMocks()
  ScopedEventSource.instances = []
  vi.stubGlobal('EventSource', ScopedEventSource)
  vi.mocked(getScopedEventHistory).mockResolvedValue(scopedHistory())
})
afterEach(() => vi.unstubAllGlobals())

describe('independent scoped event stream', () => {
  it('loads bounded history and starts at the tail, ignoring duplicate and wrong-scope events', async () => {
    const onEvent = vi.fn()
    const onReady = vi.fn()
    vi.mocked(getScopedEventHistory).mockResolvedValue(scopedHistory([scopedEvent(199), scopedEvent(200)]))
    const { result } = renderHook(() => useScopedEvents({ scope: 'project', id: 'project_1', onEvent, onReady }))
    await waitFor(() => expect(ScopedEventSource.instances).toHaveLength(1))
    const source = ScopedEventSource.instances[0]
    expect(source.url).toBe('/project/project_1/events?after=200')
    expect(getScopedEventHistory).toHaveBeenCalledWith('project', 'project_1', expect.objectContaining({ tail: true, limit: 200, signal: expect.any(AbortSignal) }))
    expect(onReady).toHaveBeenCalledOnce()
    expect(onEvent).not.toHaveBeenCalled()
    act(() => {
      source.emit(scopedEvent(200))
      source.emit(scopedEvent(201, { project_id: 'other' }))
      source.emit(scopedEvent(201, { stream_id: 'other' }))
      source.emit(scopedEvent(201))
    })
    expect(result.current.events.map((event) => event.sequence)).toEqual([199, 200, 201])
    expect(onEvent).toHaveBeenCalledOnce()
  })

  it('closes on project switch and ignores late history and old-source updates', async () => {
    let finishOld!: (value: ScopedEventHistory) => void
    vi.mocked(getScopedEventHistory).mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve }))
    const onEvent = vi.fn()
    const { result, rerender } = renderHook(({ id }) => useScopedEvents({ scope: 'project', id, onEvent }), { initialProps: { id: 'old' } })
    rerender({ id: 'project_1' })
    await waitFor(() => expect(ScopedEventSource.instances).toHaveLength(1))
    const source = ScopedEventSource.instances[0]
    await act(async () => { finishOld(scopedHistory([scopedEvent(1, { project_id: 'old', stream_id: 'old-stream' })], 'old-stream')) })
    expect(result.current.events).toEqual([])
    expect(ScopedEventSource.instances).toHaveLength(1)
    await act(async () => rerender({ id: 'new' }))
    expect(source.closed).toBe(true)
    act(() => source.emit(scopedEvent(1)))
    expect(onEvent).not.toHaveBeenCalled()
    expect(result.current.events).toEqual([])
  })

  it.each([401, 403, 404])('checks access on disconnect, clears unavailable data (%s) and rejects late events', async (status) => {
    const onUnavailable = vi.fn()
    const onEvent = vi.fn()
    vi.mocked(getScopedEventHistory).mockResolvedValueOnce(scopedHistory([scopedEvent(1)]))
    const { result } = renderHook(() => useScopedEvents({ scope: 'project', id: 'project_1', onUnavailable, onEvent }))
    await waitFor(() => expect(ScopedEventSource.instances).toHaveLength(1))
    vi.mocked(getScopedEventHistory).mockRejectedValueOnce({ status })
    await act(async () => ScopedEventSource.instances[0].onerror?.())
    expect(result.current.status).toBe('unavailable')
    expect(result.current.events).toEqual([])
    expect(ScopedEventSource.instances[0].closed).toBe(true)
    expect(onUnavailable).toHaveBeenCalledOnce()
    act(() => ScopedEventSource.instances[0].emit(scopedEvent(2)))
    expect(onEvent).not.toHaveBeenCalled()
  })

  it('keeps native reconnection and its cursor on a transient connection error', async () => {
    const { result } = renderHook(() => useScopedEvents({ scope: 'project', id: 'project_1' }))
    await waitFor(() => expect(ScopedEventSource.instances).toHaveLength(1))
    const source = ScopedEventSource.instances[0]
    await act(async () => source.onerror?.())
    expect(result.current.status).toBe('reconnecting')
    expect(source.closed).toBe(false)
    expect(ScopedEventSource.instances).toHaveLength(1)
    act(() => source.onopen?.())
    expect(result.current.status).toBe('live')
  })

  it('reconciles a sequence gap before resuming live updates', async () => {
    const onEvent = vi.fn()
    const { result } = renderHook(() => useScopedEvents({ scope: 'project', id: 'project_1', onEvent }))
    await waitFor(() => expect(ScopedEventSource.instances).toHaveLength(1))
    vi.mocked(getScopedEventHistory).mockResolvedValue(scopedHistory([scopedEvent(1), scopedEvent(2)]))
    await act(async () => ScopedEventSource.instances[0].emit(scopedEvent(2)))
    expect(ScopedEventSource.instances[0].closed).toBe(true)
    expect(ScopedEventSource.instances[1].url).toContain('after=2')
    expect(result.current.events).toHaveLength(2)
    expect(onEvent).not.toHaveBeenCalled()
  })

  it('loads paused history without a socket and reconnects from a refreshed tail', async () => {
    const { result, rerender } = renderHook(({ live }) => useScopedEvents({ scope: 'project', id: 'project_1', live }), { initialProps: { live: false } })
    await waitFor(() => expect(result.current.status).toBe('paused'))
    expect(ScopedEventSource.instances).toHaveLength(0)
    vi.mocked(getScopedEventHistory).mockResolvedValue(scopedHistory([scopedEvent(4)]))
    rerender({ live: true })
    await waitFor(() => expect(ScopedEventSource.instances).toHaveLength(1))
    expect(ScopedEventSource.instances[0].url).toContain('after=4')
    await act(async () => rerender({ live: false }))
    expect(ScopedEventSource.instances[0].closed).toBe(true)
  })

  it('treats provider stream ids as opaque and rejects malformed history', async () => {
    const event = scopedEvent(8, { stream_id: 'tenant:opaque:hash:profile', project_id: null, provider_profile_id: 'deepseek', type: 'provider.preflight.failed', data: { status: 'failed' } })
    vi.mocked(getScopedEventHistory).mockResolvedValue(scopedHistory([event], event.stream_id))
    const { result, rerender } = renderHook(({ id }) => useScopedEvents({ scope: 'provider', id }), { initialProps: { id: 'deepseek' } })
    await waitFor(() => expect(result.current.events).toEqual([event]))
    expect(ScopedEventSource.instances[0].url).toContain('/provider/deepseek/')
    vi.mocked(getScopedEventHistory).mockResolvedValue(scopedHistory([event], 'wrong-stream'))
    rerender({ id: 'other-provider' })
    await waitFor(() => expect(result.current.status).toBe('error'))
    expect(result.current.events).toEqual([])
    expect(ScopedEventSource.instances).toHaveLength(1)
  })

  it('discards the old provider history when access checking finds a different stream', async () => {
    const oldEvent = scopedEvent(20, { stream_id: 'tenant-old', project_id: null, provider_profile_id: 'deepseek', type: 'provider.preflight.passed', data: { status: 'passed' } })
    vi.mocked(getScopedEventHistory).mockResolvedValueOnce(scopedHistory([oldEvent], 'tenant-old'))
    const { result } = renderHook(() => useScopedEvents({ scope: 'provider', id: 'deepseek' }))
    await waitFor(() => expect(ScopedEventSource.instances).toHaveLength(1))
    vi.mocked(getScopedEventHistory).mockResolvedValue(scopedHistory([], 'tenant-new'))
    await act(async () => ScopedEventSource.instances[0].onerror?.())
    expect(ScopedEventSource.instances[0].closed).toBe(true)
    expect(ScopedEventSource.instances[1].url).toContain('after=0')
    expect(result.current.events).toEqual([])
    act(() => ScopedEventSource.instances[0].emit(scopedEvent(21, { ...oldEvent, sequence: 21, id: 'tenant-old:21' })))
    expect(result.current.events).toEqual([])
  })
})
