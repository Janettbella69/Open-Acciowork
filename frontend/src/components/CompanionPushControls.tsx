import { useCallback, useEffect, useRef, useState } from 'react'
import {
  CompanionRequestError, getCompanionPushStatus, subscribeCompanionPush, unsubscribeCompanionPush,
  type CompanionPushStatus, type CompanionSession,
} from '../api/companion'
import {
  activateCompanionWorker, companionRegistration, companionSubscriptionChanged,
  pushApplicationKey, pushSupportIssue, removeCompanionSubscription, subscriptionFingerprint, subscriptionUsesKey,
} from '../companionPush'
import { useT } from '../i18n'

type Snapshot = { server: CompanionPushStatus; local: boolean; matched: boolean; permission: NotificationPermission }

export default function CompanionPushControls({ sessionStatus, deviceId }: {
  sessionStatus: CompanionSession['status'] | null
  deviceId: string | null
}) {
  const t = useT()
  const supported = pushSupportIssue()
  const approved = sessionStatus === 'approved' && Boolean(deviceId)
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [unauthorized, setUnauthorized] = useState(false)
  const context = useRef({ controller: new AbortController(), approved, supported })
  const mutation = useRef<object | null>(null)
  const revision = useRef(0)
  const reading = useRef<object | null>(null)

  const readSnapshot = async (server: CompanionPushStatus): Promise<Snapshot> => {
    const subscription = await (await companionRegistration())?.pushManager.getSubscription()
    const valid = subscription && (subscription.expirationTime === null || subscription.expirationTime > Date.now())
    const matched = Boolean(valid && server.public_key && subscriptionUsesKey(subscription, pushApplicationKey(server.public_key))
      && await subscriptionFingerprint(subscription) === server.subscription_fingerprint)
    return { server, local: Boolean(subscription), matched, permission: Notification.permission }
  }

  const refresh = useCallback(async () => {
    const currentContext = context.current
    if (currentContext.controller.signal.aborted || !currentContext.approved || currentContext.supported || mutation.current || reading.current) return
    const token = {}
    const version = revision.current
    reading.current = token
    const current = () => !currentContext.controller.signal.aborted && version === revision.current
    try {
      const next = await readSnapshot(await getCompanionPushStatus())
      if (current()) { setSnapshot(next); setError(''); setUnauthorized(false) }
    } catch (cause) {
      if (!current()) return
      setSnapshot(null)
      if (cause instanceof CompanionRequestError && [401, 403].includes(cause.status)) {
        setUnauthorized(true)
        try { await removeCompanionSubscription() } catch {
          if (current()) setError('The local push subscription could not be removed. Disable notifications in browser settings or retry cleanup.')
        }
      } else setError('Could not verify background push. Check your connection and retry; push is not confirmed enabled.')
    } finally {
      if (reading.current === token) reading.current = null
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    context.current = { controller, approved, supported }
    reading.current = null
    revision.current += 1
    setSnapshot(null)
    setError('')
    setNotice('')
    setUnauthorized(false)
    setBusy(false)
    if (approved) void refresh()
    else if (sessionStatus !== null) {
      // The server revokes delivery authority; also remove this browser's local
      // capability. No authenticated write is attempted with a revoked cookie.
      void removeCompanionSubscription().catch(() => {
        if (!controller.signal.aborted) setError('The local push subscription could not be removed. Disable notifications in browser settings or retry cleanup.')
      })
    }
    const onMessage = (event: MessageEvent) => {
      if (event.origin === window.location.origin && event.data?.type === companionSubscriptionChanged) void refresh()
    }
    window.addEventListener('focus', refresh)
    navigator.serviceWorker?.addEventListener('message', onMessage)
    const timer = window.setInterval(() => void refresh(), 30_000)
    return () => {
      controller.abort()
      window.clearInterval(timer)
      window.removeEventListener('focus', refresh)
      navigator.serviceWorker?.removeEventListener('message', onMessage)
    }
  }, [approved, deviceId, sessionStatus, supported, refresh])

  const enable = async () => {
    const currentContext = context.current
    if (!currentContext.approved || currentContext.supported || unauthorized || !snapshot?.server.enabled || !snapshot.server.public_key || mutation.current) return
    const token = {}
    mutation.current = token
    revision.current += 1
    setBusy(true)
    setError('')
    setNotice('')
    let created: PushSubscription | null = null
    let completed = false
    const current = () => !currentContext.controller.signal.aborted
    try {
      // Permission is requested directly from this click, never from an effect,
      // focus event, service worker, incoming notification, or server response.
      const permission = Notification.permission === 'default' ? await Notification.requestPermission() : Notification.permission
      if (!current()) return
      if (permission !== 'granted') {
        setSnapshot((value) => value ? { ...value, permission } : value)
        setNotice(permission === 'denied' ? 'Notifications are blocked. Allow them in browser or system settings, then refresh push status.' : 'Notification permission was not granted. You can try enabling push again.')
        return
      }
      const key = pushApplicationKey(snapshot.server.public_key)
      const registration = await activateCompanionWorker(currentContext.controller.signal)
      if (!current()) return
      let subscription = await registration.pushManager.getSubscription()
      if (!current()) return
      if (subscription && (!subscriptionUsesKey(subscription, key) || (subscription.expirationTime !== null && subscription.expirationTime <= Date.now()))) {
        await removeCompanionSubscription(subscription)
        subscription = null
      }
      if (!current()) return
      if (!subscription) {
        subscription = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key })
        created = subscription
      }
      if (!current()) return
      const nextServer = await subscribeCompanionPush(subscription.toJSON())
      if (!current()) return
      const next = await readSnapshot(nextServer)
      if (!current()) return
      if (!next.server.enabled || !next.server.subscribed || !next.matched || next.permission !== 'granted') throw new Error('Push registration was not confirmed.')
      completed = true
      setSnapshot(next)
      setNotice('Background push is enabled for this browser. Delivery still depends on browser, network and system settings.')
    } catch (cause) {
      if (current()) {
        setSnapshot(null)
        if (cause instanceof CompanionRequestError && [401, 403].includes(cause.status)) {
          setUnauthorized(true)
          // This can be a previously existing subscription, not just the one
          // created above, so a newly revoked device must clean it up as well.
          try { await removeCompanionSubscription() } catch { /* The explicit cleanup action remains available. */ }
        }
        setError('Could not confirm background push. Refresh its status before retrying.')
      }
    } finally {
      if (created && !completed) {
        try { await removeCompanionSubscription(created) } catch {
          if (current()) setError('Push registration failed and browser cleanup also failed. Disable notifications in browser settings or retry cleanup.')
        }
      }
      if (mutation.current === token) mutation.current = null
      if (current()) setBusy(false)
    }
  }

  const disable = async () => {
    if (mutation.current) return
    const token = {}
    const currentContext = context.current
    const current = () => !currentContext.controller.signal.aborted
    mutation.current = token
    revision.current += 1
    setBusy(true)
    setError('')
    setNotice('')
    let server: CompanionPushStatus | null = null
    let serverFailed = false
    let localFailed = false
    try {
      if (currentContext.approved && !unauthorized) {
        try { server = await unsubscribeCompanionPush(); serverFailed = server.subscribed } catch { serverFailed = true }
      }
      // Always attempt local removal, even if the network/server is unavailable.
      try { await removeCompanionSubscription() } catch { localFailed = true }
      if (!current()) return
      setSnapshot(server && !localFailed ? { server, local: false, matched: false, permission: Notification.permission } : null)
      if (localFailed) setError('The local push subscription could not be removed. Disable notifications in browser settings or retry cleanup.')
      else if (serverFailed) setError('Browser push was removed, but server cleanup is unconfirmed. Retry disabling push when connected.')
      else setNotice('Background push is disabled on this browser.')
    } finally {
      if (mutation.current === token) mutation.current = null
      if (current()) setBusy(false)
    }
  }

  let status = 'Checking background push…'
  if (!approved || unauthorized) status = 'Background push requires an approved device. Revoked or expired devices cannot receive new updates.'
  else if (supported) status = supported
  else if (error && !snapshot) status = 'Background push status is unconfirmed.'
  else if (snapshot?.permission === 'denied') status = 'Notifications are blocked. Allow them in browser or system settings, then refresh push status.'
  else if (snapshot && !snapshot.server.enabled) status = 'Background push is not configured on the server. Ask the desktop owner to configure Web Push.'
  else if (snapshot?.server.subscribed && snapshot.matched && snapshot.permission === 'granted') status = 'Background push is enabled for this browser. Delivery still depends on browser, network and system settings.'
  else if (snapshot && (snapshot.local || snapshot.server.subscribed || (snapshot.server.reason && snapshot.server.reason !== 'unsubscribed'))) status = 'The push subscription is missing, expired or out of sync. Enable push again to reconnect this browser.'
  else if (snapshot) status = 'Background push is off. Enable it to receive generic update notifications while this page is closed.'

  return <section className="mobile-settings" aria-labelledby="mobile-push-heading">
    <h2 id="mobile-push-heading">{t('Background notifications')}</h2>
    <p role="status">{t(status)}</p>
    {snapshot?.server.reason && <p className="mobile-service-note">{t('Server status: {reason}', { reason: snapshot.server.reason })}</p>}
    {error && <p className="inbox-error" role="alert">{t(error)}</p>}
    {notice && notice !== status && <p className="mobile-notice" role="status">{t(notice)}</p>}
    <p className="mobile-service-note">{t('Notifications contain no project content and never approve or start work. On iPhone or iPad, install this companion on the Home Screen, open it there, and pair that app if needed.')}</p>
    <div className="mobile-actions">
      {approved && !unauthorized && !supported && <>
        <button type="button" onClick={() => void enable()} disabled={busy || !snapshot?.server.enabled || !snapshot.server.public_key || snapshot.permission === 'denied' || (snapshot.server.subscribed && snapshot.matched && snapshot.permission === 'granted')}>{t(busy ? 'Updating push…' : 'Enable background push')}</button>
        <button type="button" disabled={busy} onClick={() => { setNotice(''); void refresh() }}>{t('Refresh push status')}</button>
      </>}
      {!supported && (snapshot?.local || snapshot?.server.subscribed || error) && <button type="button" disabled={busy} onClick={() => void disable()}>{t(approved && !unauthorized ? 'Disable background push' : 'Retry local push cleanup')}</button>}
    </div>
  </section>
}
