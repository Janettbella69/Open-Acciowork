// @vitest-environment-options {"url":"https://trade.example/app/mobile"}
import { webcrypto } from 'node:crypto'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  CompanionRequestError, getCompanionPushStatus, subscribeCompanionPush, unsubscribeCompanionPush,
  type CompanionPushStatus,
} from '../api/companion'
import { companionWorkerPath, companionWorkerScope, subscriptionFingerprint } from '../companionPush'
import CompanionPushControls from './CompanionPushControls'

vi.mock('../api/companion', async (original) => ({
  ...await original<typeof import('../api/companion')>(),
  getCompanionPushStatus: vi.fn(), subscribeCompanionPush: vi.fn(), unsubscribeCompanionPush: vi.fn(),
}))

const key = new Uint8Array(65)
key[0] = 4
const publicKey = btoa(String.fromCharCode(...key))
const off: CompanionPushStatus = { enabled: true, public_key: publicKey, subscribed: false, subscription_fingerprint: null, reason: null }
let local: PushSubscription | null
let permission: NotificationPermission
let requestPermission: ReturnType<typeof vi.fn>
let subscribe: ReturnType<typeof vi.fn>
let registration: ServiceWorkerRegistration
let workers: ServiceWorkerContainer

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

function makeSubscription(endpoint = 'https://push.example/private-capability'): PushSubscription {
  const value = {
    endpoint, expirationTime: null, options: { applicationServerKey: key.buffer, userVisibleOnly: true },
    toJSON: () => ({ endpoint, keys: { p256dh: 'private-browser-key', auth: 'private-auth-key' } }),
    unsubscribe: vi.fn(async () => { if (local === value) local = null; return true }),
  } as unknown as PushSubscription
  return value
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('isSecureContext', true)
  vi.stubGlobal('crypto', webcrypto)
  vi.stubGlobal('PushManager', class {})
  permission = 'default'
  requestPermission = vi.fn(async () => { permission = 'granted'; return permission })
  vi.stubGlobal('Notification', { get permission() { return permission }, requestPermission })
  local = null
  subscribe = vi.fn(async () => { local = makeSubscription(); return local })
  registration = {
    scope: new URL(companionWorkerScope, window.location.origin).href,
    active: { state: 'activated', scriptURL: new URL(companionWorkerPath, window.location.origin).href },
    pushManager: { getSubscription: vi.fn(async () => local), subscribe },
  } as unknown as ServiceWorkerRegistration
  workers = Object.assign(new EventTarget(), {
    getRegistration: vi.fn(async () => registration), register: vi.fn(async () => registration),
  }) as unknown as ServiceWorkerContainer
  Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: workers })
  vi.mocked(getCompanionPushStatus).mockResolvedValue(off)
  vi.mocked(subscribeCompanionPush).mockImplementation(async () => ({ ...off, subscribed: true, subscription_fingerprint: await subscriptionFingerprint(local!) }))
  vi.mocked(unsubscribeCompanionPush).mockResolvedValue({ ...off, reason: 'unsubscribed' })
})

afterEach(() => {
  vi.unstubAllGlobals()
  Reflect.deleteProperty(navigator, 'serviceWorker')
})

const view = () => render(<CompanionPushControls sessionStatus="approved" deviceId="phone-1" />)
const enableButton = () => screen.getByRole('button', { name: 'Enable background push' })
const loaded = async () => { await screen.findByText(/Background push is off/); expect(enableButton()).toBeEnabled() }

describe('companion Web Push', () => {
  it('only requests permission from a click and sends a standard subscription, never its secrets to the DOM', async () => {
    view()
    await loaded()
    fireEvent.focus(window)
    expect(requestPermission).not.toHaveBeenCalled()
    expect(workers.register).not.toHaveBeenCalled()
    fireEvent.click(enableButton())
    expect(requestPermission).toHaveBeenCalledTimes(1)
    await screen.findByText(/Background push is enabled for this browser/)
    expect(workers.register).toHaveBeenCalledWith(companionWorkerPath, { scope: companionWorkerScope, updateViaCache: 'none' })
    expect(subscribe).toHaveBeenCalledWith({ userVisibleOnly: true, applicationServerKey: key })
    expect(subscribeCompanionPush).toHaveBeenCalledWith(local!.toJSON())
    expect(document.body.textContent).not.toMatch(/private-capability|private-auth-key|private-browser-key/)
    expect(enableButton()).toBeDisabled()
  })

  it('explains denied permission, does not prompt again, and can retry after browser settings change', async () => {
    requestPermission.mockImplementation(async () => { permission = 'denied'; return permission })
    view()
    await loaded()
    fireEvent.click(enableButton())
    await screen.findByText(/Notifications are blocked/)
    expect(enableButton()).toBeDisabled()
    expect(subscribe).not.toHaveBeenCalled()
    permission = 'granted'
    fireEvent.click(screen.getByRole('button', { name: 'Refresh push status' }))
    await loaded()
    fireEvent.click(enableButton())
    await screen.findByText(/Background push is enabled for this browser/)
    expect(requestPermission).toHaveBeenCalledTimes(1)
  })

  it('allows another deliberate attempt after permission is dismissed', async () => {
    requestPermission.mockResolvedValueOnce('default')
    view()
    await loaded()
    fireEvent.click(enableButton())
    await screen.findByText(/Notification permission was not granted/)
    expect(subscribe).not.toHaveBeenCalled()
    fireEvent.click(enableButton())
    await screen.findByText(/Background push is enabled for this browser/)
    expect(requestPermission).toHaveBeenCalledTimes(2)
  })

  it('rolls back a new browser subscription when the server rejects it and supports a fresh retry', async () => {
    vi.mocked(subscribeCompanionPush).mockRejectedValueOnce(new Error('network failure'))
    view()
    await loaded()
    fireEvent.click(enableButton())
    await screen.findByText(/Could not confirm background push/)
    await waitFor(() => expect(local).toBeNull())
    fireEvent.click(screen.getByRole('button', { name: 'Refresh push status' }))
    await loaded()
    fireEvent.click(enableButton())
    await screen.findByText(/Background push is enabled for this browser/)
    expect(subscribe).toHaveBeenCalledTimes(2)
  })

  it('shows cleanup failure instead of claiming a failed subscription was rolled back', async () => {
    subscribe.mockImplementation(async () => {
      local = makeSubscription()
      vi.mocked(local.unsubscribe).mockResolvedValue(false)
      return local
    })
    vi.mocked(subscribeCompanionPush).mockRejectedValue(new Error('network failure'))
    view()
    await loaded()
    fireEvent.click(enableButton())
    await screen.findByText(/Push registration failed and browser cleanup also failed/)
    expect(local).not.toBeNull()
  })

  it('removes local push even if server unsubscribe fails and lets the user retry server cleanup', async () => {
    permission = 'granted'
    local = makeSubscription()
    vi.mocked(getCompanionPushStatus).mockResolvedValue({ ...off, subscribed: true, subscription_fingerprint: await subscriptionFingerprint(local) })
    vi.mocked(unsubscribeCompanionPush).mockRejectedValueOnce(new Error('offline'))
    view()
    await screen.findByText(/Background push is enabled for this browser/)
    fireEvent.click(screen.getByRole('button', { name: 'Disable background push' }))
    await screen.findByText(/Browser push was removed, but server cleanup is unconfirmed/)
    expect(local).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Disable background push' }))
    await screen.findByText(/Background push is disabled on this browser/)
    expect(unsubscribeCompanionPush).toHaveBeenCalledTimes(2)
    expect(screen.getByText(/Background push is off/)).toBeInTheDocument()
    expect(screen.queryByText(/The push subscription is missing, expired or out of sync/)).not.toBeInTheDocument()
    vi.mocked(getCompanionPushStatus).mockResolvedValue({ ...off, reason: 'unsubscribed' })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh push status' }))
    await waitFor(() => expect(enableButton()).toBeEnabled())
    expect(screen.getByText(/Background push is off/)).toBeInTheDocument()
  })

  it('does not claim enabled when the browser endpoint rotates or the server has expired its subscription', async () => {
    permission = 'granted'
    local = makeSubscription('https://push.example/new-endpoint')
    vi.mocked(getCompanionPushStatus).mockResolvedValue({ ...off, subscribed: true, subscription_fingerprint: 'outdated' })
    view()
    await screen.findByText(/The push subscription is missing, expired or out of sync/)
    expect(enableButton()).toBeEnabled()
    expect(subscribeCompanionPush).not.toHaveBeenCalled()
    fireEvent.click(enableButton())
    await screen.findByText(/Background push is enabled for this browser/)
    expect(subscribe).not.toHaveBeenCalled()
  })

  it('reports disabled server configuration without asking for notification permission', async () => {
    vi.mocked(getCompanionPushStatus).mockResolvedValue({ ...off, enabled: false, public_key: null, reason: 'Web Push is not configured' })
    view()
    await screen.findByText(/Background push is not configured on the server/)
    expect(enableButton()).toBeDisabled()
    expect(requestPermission).not.toHaveBeenCalled()
  })

  it('requires HTTPS and browser support without calling authenticated push APIs', async () => {
    vi.stubGlobal('isSecureContext', false)
    const result = view()
    expect(screen.getByText('Background push requires HTTPS on this device.')).toBeInTheDocument()
    expect(getCompanionPushStatus).not.toHaveBeenCalled()
    result.unmount()
    vi.stubGlobal('isSecureContext', true)
    Reflect.deleteProperty(window, 'PushManager')
    view()
    expect(screen.getByText(/This browser does not support background Web Push here/)).toBeInTheDocument()
    expect(getCompanionPushStatus).not.toHaveBeenCalled()
  })

  it('cleans the local subscription on device revocation without using the revoked API credential', async () => {
    local = makeSubscription()
    const unsubscribe = local.unsubscribe
    const result = view()
    await screen.findByText(/The push subscription is missing/)
    result.rerender(<CompanionPushControls sessionStatus="revoked" deviceId="phone-1" />)
    await waitFor(() => expect(unsubscribe).toHaveBeenCalled())
    expect(screen.getByText(/Background push requires an approved device/)).toBeInTheDocument()
    expect(unsubscribeCompanionPush).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Enable background push' })).not.toBeInTheDocument()
  })

  it('treats authenticated 403 as revocation and removes local push', async () => {
    local = makeSubscription()
    const unsubscribe = local.unsubscribe
    vi.mocked(getCompanionPushStatus).mockRejectedValue(new CompanionRequestError('Device is revoked', 403))
    view()
    await screen.findByText(/Background push requires an approved device/)
    await waitFor(() => expect(unsubscribe).toHaveBeenCalled())
    expect(subscribeCompanionPush).not.toHaveBeenCalled()
  })

  it('does not register or subscribe if the device is revoked during the permission prompt', async () => {
    const pending = deferred<NotificationPermission>()
    requestPermission.mockReturnValue(pending.promise)
    const result = view()
    await loaded()
    fireEvent.click(enableButton())
    result.rerender(<CompanionPushControls sessionStatus="revoked" deviceId="phone-1" />)
    await act(async () => { pending.resolve('granted') })
    expect(workers.register).not.toHaveBeenCalled()
    expect(subscribeCompanionPush).not.toHaveBeenCalled()
  })

  it('rolls back a subscription that completes after the component unmounts', async () => {
    const pending = deferred<PushSubscription>()
    subscribe.mockReturnValue(pending.promise)
    const result = view()
    await loaded()
    fireEvent.click(enableButton())
    await waitFor(() => expect(subscribe).toHaveBeenCalled())
    result.unmount()
    const abandoned = makeSubscription()
    await act(async () => { local = abandoned; pending.resolve(abandoned) })
    expect(abandoned.unsubscribe).toHaveBeenCalled()
    expect(subscribeCompanionPush).not.toHaveBeenCalled()
  })

  it('never restores enabled UI when registration succeeds after a device is revoked', async () => {
    const pending = deferred<CompanionPushStatus>()
    vi.mocked(subscribeCompanionPush).mockReturnValue(pending.promise)
    const result = view()
    await loaded()
    fireEvent.click(enableButton())
    await waitFor(() => expect(subscribeCompanionPush).toHaveBeenCalled())
    const stale = { ...off, subscribed: true, subscription_fingerprint: await subscriptionFingerprint(local!) }
    result.rerender(<CompanionPushControls sessionStatus="revoked" deviceId="phone-1" />)
    await act(async () => { pending.resolve(stale) })
    await waitFor(() => expect(local).toBeNull())
    expect(screen.getByText(/Background push requires an approved device/)).toBeInTheDocument()
    expect(screen.queryByText(/Background push is enabled for this browser/)).not.toBeInTheDocument()
  })

  it('shows local unsubscribe failure and allows retry without claiming push is disabled', async () => {
    permission = 'granted'
    local = makeSubscription()
    const unsubscribe = vi.mocked(local.unsubscribe)
    unsubscribe.mockResolvedValueOnce(false)
    vi.mocked(getCompanionPushStatus).mockResolvedValue({ ...off, subscribed: true, subscription_fingerprint: await subscriptionFingerprint(local) })
    view()
    await screen.findByText(/Background push is enabled for this browser/)
    fireEvent.click(screen.getByRole('button', { name: 'Disable background push' }))
    await screen.findByText(/The local push subscription could not be removed/)
    expect(screen.queryByText('Background push is disabled on this browser.')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Disable background push' }))
    await screen.findByText('Background push is disabled on this browser.')
    expect(local).toBeNull()
  })

  it('does not let an old status response overwrite a newly enabled subscription', async () => {
    const old = deferred<CompanionPushStatus>()
    view()
    await loaded()
    vi.mocked(getCompanionPushStatus).mockReturnValueOnce(old.promise)
    fireEvent.focus(window)
    fireEvent.click(enableButton())
    await screen.findByText(/Background push is enabled for this browser/)
    await act(async () => { old.resolve(off) })
    expect(screen.getByText(/Background push is enabled for this browser/)).toBeInTheDocument()
  })

  it('never overwrites or unsubscribes a different worker at the same scope', async () => {
    registration = { ...registration, active: { scriptURL: 'https://trade.example/other-worker.js' } } as ServiceWorkerRegistration
    view()
    await screen.findByText(/Could not verify background push/)
    expect(workers.register).not.toHaveBeenCalled()
    expect(subscribe).not.toHaveBeenCalled()
  })
})
