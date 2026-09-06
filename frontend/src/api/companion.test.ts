import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  companionModeStorageKey, consumePairingFragment, getCompanionOverview, pairCompanion,
  resolveCompanionPermission, startCompanionRun,
  getCompanionPushStatus, subscribeCompanionPush, unsubscribeCompanionPush,
  resumeCompanionPermissionRun,
} from './companion'

beforeEach(() => {
  window.history.replaceState(null, '', '/app/mobile')
  const storage = new Map<string, string>()
  Object.defineProperty(window, 'localStorage', { configurable: true, value: {
    get length() { return storage.size },
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => { storage.set(key, value) },
    removeItem: (key: string) => { storage.delete(key) },
    clear: () => storage.clear(),
  } })
})

afterEach(() => vi.unstubAllGlobals())

describe('companion transport', () => {
  it('resumes only the saved permission for the exact run via a protected bodyless POST', async () => {
    const fetchMock = vi.fn(async () => new Response('{}', { status: 202 }))
    vi.stubGlobal('fetch', fetchMock)
    await resumeCompanionPermissionRun('run/1')
    expect(fetchMock).toHaveBeenCalledWith('/api/companion/runs/run%2F1/resume-permission', expect.objectContaining({
      method: 'POST', credentials: 'include', headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-Ophira-Companion': '1' },
    }))
    expect((fetchMock.mock.calls[0] as unknown as [string, RequestInit])[1].body).toBeUndefined()
  })
  it('uses scoped cookie authentication for push status and subscription writes, without JavaScript bearer storage', async () => {
    const fetchMock = vi.fn(async () => new Response('{}', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    const subscription = { endpoint: 'https://push.example/private-capability', keys: { p256dh: 'private-key', auth: 'private-auth' } }
    await getCompanionPushStatus()
    await subscribeCompanionPush(subscription)
    await unsubscribeCompanionPush()
    expect(fetchMock.mock.calls).toEqual([
      ['/api/companion/push/status', expect.objectContaining({ method: 'GET', credentials: 'include', cache: 'no-store' })],
      ['/api/companion/push/subscription', expect.objectContaining({ method: 'POST', credentials: 'include', body: JSON.stringify(subscription), headers: { Accept: 'application/json', 'Content-Type': 'application/json', 'X-Ophira-Companion': '1' } })],
      ['/api/companion/push/unsubscribe', expect.objectContaining({ method: 'POST', credentials: 'include', body: '{}' })],
    ])
    expect(window.localStorage.length).toBe(0)
  })
  it('uses the HttpOnly credential via include, never a JavaScript bearer, and protects JSON writes', async () => {
    const fetchMock = vi.fn(async () => new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    await getCompanionOverview()
    await pairCompanion({ pairing_id: 'pair_1', secret: 'one_time_secret_123' }, 'My phone')
    expect(fetchMock.mock.calls[0]).toEqual(['/api/companion/overview', expect.objectContaining({
      method: 'GET', credentials: 'include', cache: 'no-store', headers: { Accept: 'application/json', 'X-Ophira-Companion': '1' },
    })])
    expect(fetchMock.mock.calls[1]).toEqual(['/api/companion/pair', expect.objectContaining({
      method: 'POST', credentials: 'include', headers: { Accept: 'application/json', 'X-Ophira-Companion': '1', 'Content-Type': 'application/json' },
      body: JSON.stringify({ pairing_id: 'pair_1', secret: 'one_time_secret_123', device_name: 'My phone' }),
    })])
    expect(window.localStorage.length).toBe(0)
  })

  it('encodes identifiers and carries structured answers unchanged', async () => {
    const fetchMock = vi.fn(async () => new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)
    await resolveCompanionPermission('run/1', 'perm/1', 'allow_once', { Market: ['Japan', 'Germany'] })
    await startCompanionRun('project/1', 'Analyze this opportunity')
    expect(fetchMock.mock.calls[0]).toEqual(['/api/companion/runs/run%2F1/permissions/perm%2F1', expect.objectContaining({
      body: JSON.stringify({ decision: 'allow_once', reason: '', answers: { Market: ['Japan', 'Germany'] } }),
    })])
    expect(fetchMock.mock.calls[1]).toEqual(['/api/companion/projects/project%2F1/runs', expect.any(Object)])
  })

  it('does not reflect rejected pairing validation inputs into error messages', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: [{ input: { secret: 'private-secret' }, msg: 'invalid value' }] }), { status: 422 })))
    await expect(pairCompanion({ pairing_id: 'x', secret: 'private-secret' }, 'Phone')).rejects.toThrow('Request failed (422)')
  })
})

describe('pairing fragments', () => {
  it('clears the fragment synchronously and persists only the mode flag', () => {
    window.history.replaceState({ keep: true }, '', '/app/mobile?view=mobile#pairing=pair_1&secret=one_time_secret_123')
    expect(consumePairingFragment()).toEqual({ kind: 'pairing', input: { pairing_id: 'pair_1', secret: 'one_time_secret_123' } })
    expect(window.location.hash).toBe('')
    expect(window.location.search).toBe('?view=mobile')
    expect(window.history.state).toEqual({ keep: true })
    expect(window.localStorage.length).toBe(1)
    expect(window.localStorage.getItem(companionModeStorageKey)).toBe('true')
    expect(consumePairingFragment()).toEqual({ kind: 'none' })
  })

  it.each([
    '#pairing=pair_1', '#secret=one_time_secret_123',
    '#pairing=pair_1&secret=one_time_secret_123&secret=another_secret_123',
    '#pairing=pair_1&pairing=pair_2&secret=one_time_secret_123',
    '#pairing=../outside&secret=one_time_secret_123',
  ])('clears and rejects malformed fragment %s', (hash) => {
    window.history.replaceState(null, '', `/app/mobile${hash}`)
    expect(consumePairingFragment()).toEqual({ kind: 'invalid' })
    expect(window.location.hash).toBe('')
    expect(window.localStorage.getItem(companionModeStorageKey)).toBe('true')
  })
})
