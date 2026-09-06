import { describe, expect, it, vi } from 'vitest'
import workerSource from '../public/ophira-companion-sw.js?raw'

function worker() {
  const events = new Map<string, (event: unknown) => void>()
  const scope = {
    location: { origin: 'https://trade.example' },
    addEventListener: (name: string, handler: (event: unknown) => void) => events.set(name, handler),
    skipWaiting: vi.fn(async () => undefined),
    registration: { showNotification: vi.fn(async () => undefined) },
    clients: { matchAll: vi.fn(async (): Promise<unknown[]> => []), openWindow: vi.fn(async () => undefined) },
  }
  // Execute the actual shipped SW script, not a duplicate of its handlers.
  new Function('self', workerSource)(scope)
  const dispatch = async (type: string, extra: object = {}) => {
    let completed: Promise<unknown> | undefined
    events.get(type)!({ ...extra, waitUntil: (promise: Promise<unknown>) => { completed = promise } })
    await completed
  }
  return { scope, events, dispatch }
}

describe('companion service worker', () => {
  it('shows only a generic visible notification and never parses sensitive or malicious payloads', async () => {
    const { scope, dispatch, events } = worker()
    const json = vi.fn(() => ({ url: 'https://evil.example', title: 'Confidential project', command: 'approve', secret: 'private' }))
    await dispatch('push', { data: { json } })
    expect(json).not.toHaveBeenCalled()
    expect(scope.registration.showNotification).toHaveBeenCalledWith('TradeMind', {
      body: 'There is an update. Open your trusted companion to review it.', tag: 'ophira-companion-update', icon: '/trademind-mark.svg',
    })
    expect(events.has('fetch')).toBe(false)
    expect(scope.clients.openWindow).not.toHaveBeenCalled()
  })

  it.each(['https://evil.example', '/api/companion/runs/start', 'javascript:alert(1)', '/app/mobile#pairing=secret', '/app/mobile?approve=true'])('ignores notification URL/action %s and opens only the fixed same-origin page', async (url) => {
    const { scope, dispatch } = worker()
    const close = vi.fn()
    await dispatch('notificationclick', { notification: { close, data: { url, navigate: url } }, action: url })
    expect(close).toHaveBeenCalled()
    expect(scope.clients.openWindow).toHaveBeenCalledWith('https://trade.example/app/mobile')
  })

  it('focuses an existing exact companion page, not another origin, path or old pairing fragment', async () => {
    const { scope, dispatch } = worker()
    const other = vi.fn()
    const focus = vi.fn()
    scope.clients.matchAll.mockResolvedValue([
      { url: 'https://evil.example/app/mobile', focus: other },
      { url: 'https://trade.example/app', focus: other },
      { url: 'https://trade.example/app/mobile#pairing=secret', focus: other },
      { url: 'https://trade.example/app/mobile?approve=true', focus: other },
      { url: 'https://trade.example/app/mobile', focus },
    ])
    await dispatch('notificationclick', { notification: { close: vi.fn() } })
    expect(focus).toHaveBeenCalledOnce()
    expect(other).not.toHaveBeenCalled()
    expect(scope.clients.openWindow).not.toHaveBeenCalled()
  })

  it('signals subscription change to open companion pages without silently renewing or writing to an API', async () => {
    const { scope, dispatch } = worker()
    const postMessage = vi.fn()
    const other = vi.fn()
    scope.clients.matchAll.mockResolvedValue([
      { url: 'https://trade.example/app/mobile', postMessage },
      { url: 'https://evil.example/app/mobile', postMessage: other },
    ])
    await dispatch('pushsubscriptionchange', { oldSubscription: { endpoint: 'secret' } })
    expect(postMessage).toHaveBeenCalledWith({ type: 'ophira-companion-subscription-changed' })
    expect(other).not.toHaveBeenCalled()
    expect(scope.registration.showNotification).not.toHaveBeenCalled()
  })
})
