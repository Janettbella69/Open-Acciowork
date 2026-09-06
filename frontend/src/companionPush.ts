import { apiBaseUrl } from './api/client'

export const companionWorkerPath = '/ophira-companion-sw.js'
export const companionWorkerScope = '/app/mobile'
export const companionSubscriptionChanged = 'ophira-companion-subscription-changed'

export function pushSupportIssue(): string | null {
  if (window.location.protocol !== 'https:' || !window.isSecureContext) return 'Background push requires HTTPS on this device.'
  if (new URL(apiBaseUrl || '/', window.location.href).origin !== window.location.origin) return 'Background push requires the companion API on the same origin.'
  if (window.top !== window.self || !('Notification' in window) || !('PushManager' in window) || !navigator.serviceWorker || !crypto.subtle) return 'This browser does not support background Web Push here. On iPhone or iPad, add this page to the Home Screen and open it there.'
  return null
}

// Never overwrite, unregister or unsubscribe a different application's worker.
export async function companionRegistration(): Promise<ServiceWorkerRegistration | null> {
  if (!navigator.serviceWorker) return null
  const registration = await navigator.serviceWorker.getRegistration(companionWorkerScope)
  if (!registration || registration.scope !== new URL(companionWorkerScope, window.location.origin).href) return null
  const workers = [registration.active, registration.waiting, registration.installing].filter(Boolean)
  if (!workers.length || workers.some((worker) => worker!.scriptURL !== new URL(companionWorkerPath, window.location.origin).href)) {
    throw new Error('Another service worker already uses the companion scope.')
  }
  return registration
}

export async function removeCompanionSubscription(subscription?: PushSubscription | null): Promise<void> {
  const current = subscription ?? await (await companionRegistration())?.pushManager.getSubscription()
  if (current && !await current.unsubscribe()) {
    // A false result may mean it was already removed, but must not hide failure.
    const remaining = await (await companionRegistration())?.pushManager.getSubscription()
    if (remaining?.endpoint === current.endpoint) throw new Error('The browser could not remove its push subscription.')
  }
}

export function pushApplicationKey(value: string): Uint8Array<ArrayBuffer> {
  const decoded = atob(value.replace(/-/g, '+').replace(/_/g, '/'))
  const key = Uint8Array.from(decoded, (character) => character.charCodeAt(0))
  if (key.length !== 65 || key[0] !== 4) throw new Error('Invalid push application key.')
  return key
}

export function subscriptionUsesKey(subscription: PushSubscription, key: Uint8Array): boolean {
  const existing = subscription.options.applicationServerKey
  return Boolean(existing && new Uint8Array(existing).length === key.length && new Uint8Array(existing).every((byte, index) => byte === key[index]))
}

export async function subscriptionFingerprint(subscription: PushSubscription): Promise<string> {
  const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(subscription.endpoint))
  return Array.from(new Uint8Array(hash), (value) => value.toString(16).padStart(2, '0')).join('')
}

export async function activateCompanionWorker(signal: AbortSignal): Promise<ServiceWorkerRegistration> {
  await companionRegistration() // Detect an exact-scope collision before register.
  if (signal.aborted) throw new Error('Push setup was cancelled.')
  const registration = await navigator.serviceWorker.register(companionWorkerPath, { scope: companionWorkerScope, updateViaCache: 'none' })
  if (signal.aborted) throw new Error('Push setup was cancelled.')
  if (registration.active?.state === 'activated') return registration
  await new Promise<void>((resolve, reject) => {
    const worker = registration.installing ?? registration.waiting ?? registration.active
    const finish = (error?: Error) => {
      window.clearTimeout(timer)
      worker?.removeEventListener('statechange', check)
      signal.removeEventListener('abort', abort)
      if (error) reject(error)
      else resolve()
    }
    const check = () => {
      if (worker?.state === 'activated') finish()
      else if (!worker || worker.state === 'redundant') finish(new Error('The push service worker could not activate.'))
    }
    const abort = () => finish(new Error('Push setup was cancelled.'))
    const timer = window.setTimeout(() => finish(new Error('The push service worker did not activate in time.')), 10_000)
    worker?.addEventListener('statechange', check)
    signal.addEventListener('abort', abort, { once: true })
    check()
  })
  return registration
}
