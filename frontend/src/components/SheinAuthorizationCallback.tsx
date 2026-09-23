import { useEffect, useRef, useState } from 'react'
import { cancelSheinAuthorization, errorMessage, finishSheinAuthorization, getSheinAuthorization } from '../api/client'
import type { SheinAuthorizationView } from '../api/marketplaces'
import { useT } from '../i18n'
import type { SheinCallback } from '../sheinAuthorizationCallback'
import './plugins-page.css'

export default function SheinAuthorizationCallback({ callback }: { callback: SheinCallback | null }) {
  const t = useT()
  const [view, setView] = useState<SheinAuthorizationView | null>(null)
  const [appSecret, setAppSecret] = useState('')
  const [busy, setBusy] = useState(true)
  const [needsRefresh, setNeedsRefresh] = useState(false)
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState('')
  const mounted = useRef(true)
  const pending = useRef(false)

  useEffect(() => {
    let active = true
    mounted.current = true
    if (callback) {
      void getSheinAuthorization(callback.connectorId, callback.state).then((result) => {
        if (!active) return
        if (result.app_id !== callback.appId) throw new Error('The callback does not match this app. Start authorization again.')
        setView(result); setSaved(result.status === 'complete')
      }).catch((reason) => { if (active) setError(errorMessage(reason)) })
        .finally(() => { if (active) setBusy(false) })
    } else setBusy(false)
    return () => { active = false; mounted.current = false }
  }, [callback])

  const act = async (action: () => Promise<void>) => {
    if (pending.current || !callback) return
    pending.current = true
    setBusy(true); setError('')
    try { await action() } catch (reason) {
      if (mounted.current) setError(errorMessage(reason))
    } finally {
      pending.current = false
      if (mounted.current) setBusy(false)
    }
  }
  const refresh = () => act(async () => {
    const result = await getSheinAuthorization(callback!.connectorId, callback!.state)
    if (result.app_id !== callback!.appId) throw new Error(t('The callback does not match this app. Start authorization again.'))
    if (mounted.current) {
      setView(result); setSaved(result.status === 'complete'); setNeedsRefresh(false)
    }
  })
  const finish = () => act(async () => {
    if (!view || needsRefresh || !['pending', 'installing'].includes(view.status)) return
    setNeedsRefresh(true)
    try {
      const result = await finishSheinAuthorization(callback!.connectorId, {
        state: callback!.state,
        ...(view.status === 'pending' ? {
          app_id: callback!.appId, temp_token: callback!.tempToken, app_secret: appSecret.trim(),
        } : {}),
      })
      if (result.id !== callback!.connectorId || result.type !== 'shein' || !result.enabled || result.endpoint !== view.endpoint || result.scope !== view.scope) throw new Error(t('Refresh the connector status before continuing.'))
      if (mounted.current) { setSaved(true); setNeedsRefresh(false) }
    } finally {
      if (mounted.current) setAppSecret('')
    }
  })
  const cancel = () => act(async () => {
    setNeedsRefresh(true)
    await cancelSheinAuthorization(callback!.connectorId, callback!.state)
    if (mounted.current) {
      setView((value) => value ? { ...value, status: 'cancelled' } : value)
      setAppSecret(''); setNeedsRefresh(false)
    }
  })

  return <main className="shein-authorization-callback plugins-hub-main">
    <a href="/app/plugins/connectors">{t('Back to Connectors')}</a>
    <article className="marketplace-connector"><div className="connector-copy">
      <h1>{t('Complete SHEIN authorization')}</h1>
      {!callback && <p role="alert">{t('The callback is incomplete or expired. Start authorization again from Connectors.')}</p>}
      {busy && <p role="status">{t('Working…')}</p>}
      {error && <p className="connectors-error" role="alert">{t(error)}</p>}
      {saved ? <p role="status">{t('SHEIN authorization saved. Return to Connectors to read the store and grant project access.')}</p> : callback && <>
        {view && <>
          <p>{t('SHEIN app ID')}: <strong>{view.app_id}</strong></p>
          <p>{t('Authorization API region')}: {view.endpoint}</p>
          <p>{t(view.scope.split(' ').includes('shein:products:read') ? 'Store and product reads' : 'Store reads only')}</p>
          {view.status === 'pending' && <form onSubmit={(event) => { event.preventDefault(); void finish() }}>
            <p>{t('Enter the app secret for this app. The temporary token expires ten minutes after SHEIN approval. The seller key is decrypted and the store identity is verified before saving.')}</p>
            <fieldset disabled={busy || needsRefresh}>
              <label>{t('SHEIN app secret')}<input type="password" required minLength={16} maxLength={512} autoComplete="new-password" value={appSecret} onChange={(event) => setAppSecret(event.target.value)} /></label>
              <button type="submit">{t('Verify and save SHEIN connection')}</button>
            </fieldset>
          </form>}
          {view.status === 'installing' && <button disabled={busy || needsRefresh} type="button" onClick={() => void finish()}>{t('Recover saved connection')}</button>}
          {view.status === 'exchanging' && <p>{t('An exchange has already been attempted. Check its status or start a new authorization; the same token will not be exchanged again.')}</p>}
          {['cancelled', 'expired', 'failed'].includes(view.status) && <p>{t('This authorization is no longer usable. Start again from Connectors.')}</p>}
          {['pending', 'exchanging'].includes(view.status) && <button disabled={busy || needsRefresh} type="button" onClick={() => void cancel()}>{t('Cancel authorization')}</button>}
        </>}
        {needsRefresh && <p>{t('The change may have reached the server. Refresh its status before continuing.')}</p>}
        <button disabled={busy} type="button" onClick={() => void refresh()}>{t('Check authorization status')}</button>
      </>}
    </div></article>
  </main>
}
