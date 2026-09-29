import { useCallback, useEffect, useRef, useState } from 'react'
import {
  cancelWeixinPairing, createWeixinPairing, errorMessage, getWeixinOverview,
  pollWeixinPairing, revokeWeixinAccount, saveWeixinBinding,
} from '../api/client'
import { weixinQrImage, type WeixinAccount, type WeixinOverview, type WeixinPairing } from '../api/weixin'
import { useLanguage, useT } from '../i18n'

const pairingLabels: Record<WeixinPairing['status'], string> = {
  wait: 'Scan this QR code with WeChat.',
  scaned: 'Scanned. Confirm the connection in WeChat.',
  need_verifycode: 'Enter the verification code requested by WeChat.',
  verify_code_blocked: 'Verification is blocked. Start a new connection when WeChat allows it.',
  confirmed: 'WeChat connected. Choose a project, then enable direct messages.',
  expired: 'This QR code expired. Start a new connection.',
  cancelled: 'WeChat connection cancelled.',
  unsupported_redirect: 'This WeChat authorization redirect is not supported. Start a new connection.',
  binded_redirect: 'WeChat reported an existing binding. Start a new connection.',
}
const activePairing = (pairing: WeixinPairing | null) => !!pairing && ['wait', 'scaned', 'need_verifycode'].includes(pairing.status)

function AccountBinding({ account, projects, disabled, onSave, onRevoke }: {
  account: WeixinAccount
  projects: WeixinOverview['projects']
  disabled: boolean
  onSave: (account: WeixinAccount, projectId: string | null, enabled: boolean) => void
  onRevoke: (account: WeixinAccount) => void
}) {
  const t = useT()
  const [projectId, setProjectId] = useState(account.project_id ?? '')
  const [confirming, setConfirming] = useState(false)
  const changed = projectId !== (account.project_id ?? '')
  const canEnable = !!account.project_id && projects.some((project) => project.id === account.project_id)
  return <article className="marketplace-connector" data-enabled={account.enabled ? 'true' : 'false'}>
    <div className="connector-copy">
      <header><h3>{t('WeChat account')} <code>{account.bot_id}</code></h3>
        <span>{t(account.status === 'connected' ? account.enabled ? 'Enabled' : 'Connected · disabled' : account.status === 'revoked' ? 'Revoked' : 'Expired')}</span>
      </header>
      <p>{t('Connected WeChat user')}: {account.owner_user_id}</p>
      {account.last_error_code && <p role="status">{t('Connection issue')}: <code>{account.last_error_code}</code></p>}
      {account.status === 'connected' && <>
        <p>{t('Only direct messages sent after this account is enabled will start tasks. Earlier messages are not replayed automatically.')}</p>
        <fieldset disabled={disabled}>
          <label>{t('Project for WeChat messages')}<select value={projectId} onChange={(event) => setProjectId(event.target.value)}>
            <option value="">{t('Select a project')}</option>
            {account.project_id && !projects.some((project) => project.id === account.project_id) && <option value={account.project_id} disabled>{t('Previous project unavailable')}</option>}
            {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select></label>
          <button type="button" disabled={!changed} onClick={() => onSave(account, projectId || null, false)}>{t('Save WeChat project')}</button>
        </fieldset>
        {changed && <p>{t('Saving a different project pauses this account. Enable it again after reviewing the new project.')}</p>}
        {!projects.length && <p>{t('Create a saved project you can run before enabling WeChat messages.')}</p>}
        <div className="marketplace-toolbar">
          <button type="button" disabled={disabled || changed || (!account.enabled && !canEnable)}
            onClick={() => onSave(account, account.project_id, !account.enabled)}>
            {t(account.enabled ? 'Pause WeChat messages' : 'Enable WeChat direct messages')}
          </button>
          <button type="button" disabled={disabled} onClick={() => setConfirming(true)}>{t('Revoke WeChat account')}</button>
        </div>
      </>}
      {account.status === 'expired' && <button type="button" disabled={disabled} onClick={() => setConfirming(true)}>{t('Revoke WeChat account')}</button>}
      {confirming && <div>
        <p>{t('Revoke this WeChat connection? Messages stop and reconnecting requires a new scan.')}</p>
        <p>{t('Revoking deletes the credentials saved by this app. Manage the WeChat-side binding in WeChat.')}</p>
        <button type="button" disabled={disabled} onClick={() => onRevoke(account)}>{t('Confirm WeChat revocation')}</button>{' '}
        <button type="button" disabled={disabled} onClick={() => setConfirming(false)}>{t('Cancel')}</button>
      </div>}
    </div>
  </article>
}

export default function WeixinConnector() {
  const t = useT()
  const language = useLanguage()
  const [overview, setOverview] = useState<WeixinOverview | null>(null)
  const [pairing, setPairing] = useState<WeixinPairing | null>(null)
  const [verifyCode, setVerifyCode] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const mounted = useRef(false)
  const revision = useRef(0)
  const busyRef = useRef('')
  const controller = useRef<AbortController | null>(null)
  const pairingRef = useRef<WeixinPairing | null>(null)
  const confirmedRefresh = useRef('')

  const replacePairing = useCallback((next: WeixinPairing | null) => {
    pairingRef.current = next
    setPairing(next)
    setVerifyCode('')
  }, [])

  const acceptOverview = useCallback((next: WeixinOverview) => {
    if (!next.can_manage) {
      setOverview({ ...next, accounts: [], projects: [] })
      replacePairing(null)
    } else setOverview(next)
  }, [replacePairing])

  const run = useCallback(async <T,>(key: string, operation: (signal: AbortSignal) => Promise<T>,
    accept: (value: T) => void, replace = false) => {
    if (busyRef.current && !replace) return
    controller.current?.abort()
    const abort = new AbortController()
    controller.current = abort
    const version = ++revision.current
    const current = () => mounted.current && revision.current === version && !abort.signal.aborted
    busyRef.current = key
    setBusy(key)
    setError('')
    try {
      const value = await operation(abort.signal)
      if (current()) accept(value)
    } catch (cause) {
      if (!current()) return
      const status = cause && typeof cause === 'object' && 'status' in cause ? cause.status : null
      if (status === 401 || status === 403) {
        replacePairing(null)
        setOverview(null)
        setNotice('')
        setError('WeChat management access is no longer available. Refresh after signing in.')
      } else if (status === 409) {
        replacePairing(null)
        setNotice('')
        setError('WeChat settings changed. Review the refreshed account before trying again.')
        try {
          const latest = await getWeixinOverview(abort.signal)
          if (current()) acceptOverview(latest)
        } catch {
          if (current()) setOverview(null)
        }
      } else setError(errorMessage(cause))
    } finally {
      if (current()) { busyRef.current = ''; setBusy('') }
    }
  }, [acceptOverview, replacePairing])

  const refresh = useCallback(() => run('refresh', getWeixinOverview, acceptOverview), [acceptOverview, run])

  useEffect(() => {
    mounted.current = true
    void refresh()
    return () => {
      mounted.current = false
      revision.current += 1
      controller.current?.abort()
      busyRef.current = ''
      pairingRef.current = null
    }
  }, [refresh])

  const acceptPairing = useCallback((next: WeixinPairing, expectedId?: string) => {
    if ((expectedId && next.id !== expectedId) || !Number.isFinite(Date.parse(next.expires_at)) || !Object.hasOwn(pairingLabels, next.status)) {
      replacePairing(null)
      throw new Error('The WeChat pairing response could not be verified. Start a new connection.')
    }
    const image = activePairing(next)
      ? next.qr_svg_data_url ?? (pairingRef.current?.id === next.id ? pairingRef.current.qr_svg_data_url : undefined)
      : undefined
    replacePairing({ ...next, qr_svg_data_url: image })
    if (next.status === 'confirmed') setNotice('WeChat connected. Choose a project, then enable direct messages.')
  }, [replacePairing])

  useEffect(() => {
    if (!pairing || !activePairing(pairing)) return
    const id = pairing.id
    const timer = window.setTimeout(() => {
      if (pairingRef.current?.id !== id || !activePairing(pairingRef.current)) return
      revision.current += 1
      controller.current?.abort()
      busyRef.current = ''
      setBusy('')
      replacePairing({ ...pairingRef.current, status: 'expired', qr_svg_data_url: undefined })
    }, Math.max(0, Date.parse(pairing.expires_at) - Date.now()))
    return () => window.clearTimeout(timer)
  }, [pairing, replacePairing])

  useEffect(() => {
    if (busy || pairing?.status !== 'confirmed' || confirmedRefresh.current === pairing.id) return
    confirmedRefresh.current = pairing.id
    void refresh()
  }, [busy, pairing, refresh])

  useEffect(() => {
    if (busy || error || !overview?.can_manage || !pairing || !['wait', 'scaned'].includes(pairing.status)) return
    const id = pairing.id
    const timer = window.setTimeout(() => {
      if (pairingRef.current?.id !== id || !['wait', 'scaned'].includes(pairingRef.current.status)) return
      void run('poll', (signal) => pollWeixinPairing(id, undefined, signal), (next) => acceptPairing(next, id))
    }, Math.min(10_000, Math.max(1_000, pairing.poll_after_ms || 1_000)))
    return () => window.clearTimeout(timer)
  }, [acceptPairing, busy, error, overview?.can_manage, pairing, run])

  const connect = () => {
    if (!overview?.can_manage || busyRef.current) return
    replacePairing(null)
    setNotice('')
    void run('connect', createWeixinPairing, (next) => acceptPairing(next))
  }

  const cancel = () => {
    const current = pairingRef.current
    if (!current) return
    replacePairing({ ...current, status: 'cancelled', qr_svg_data_url: undefined })
    void run('cancel', (signal) => cancelWeixinPairing(current.id, signal), (next) => acceptPairing(next, current.id), true)
  }

  const verify = () => {
    if (!pairing || pairing.status !== 'need_verifycode' || !/^[0-9]{1,12}$/.test(verifyCode.trim()) || busyRef.current) return
    const id = pairing.id
    const code = verifyCode.trim()
    setVerifyCode('')
    void run('verify', (signal) => pollWeixinPairing(id, code, signal), (next) => acceptPairing(next, id))
  }

  const updateAccount = (account: WeixinAccount, projectId: string | null, enabled: boolean) => {
    if (!overview?.can_manage || activePairing(pairingRef.current)) return
    setNotice('')
    void run(account.id, (signal) => saveWeixinBinding(account.id, {
      project_id: projectId, enabled, expected_revision: account.revision,
    }, signal), (updated) => {
      if (updated.id !== account.id) throw new Error('The WeChat account response did not match this account.')
      setOverview((current) => current && ({ ...current, accounts: current.accounts.map((item) => item.id === updated.id ? updated : item) }))
      setNotice(enabled ? 'WeChat direct messages enabled for the selected project.' : 'WeChat project saved. Direct messages are paused.')
    })
  }

  const revoke = (account: WeixinAccount) => {
    if (!overview?.can_manage || activePairing(pairingRef.current)) return
    setNotice('')
    void run(account.id, (signal) => revokeWeixinAccount(account.id, account.revision, signal), (updated) => {
      if (updated.id !== account.id) throw new Error('The WeChat account response did not match this account.')
      setOverview((current) => current && ({ ...current, accounts: current.accounts.map((item) => item.id === updated.id ? updated : item) }))
      setNotice('WeChat account revoked.')
    })
  }

  const qrImage = weixinQrImage(pairing?.qr_svg_data_url)
  const pairingActive = activePairing(pairing)
  return <section className="connectors-registry weixin-connector" aria-labelledby="weixin-connector-heading">
    <div><span>{t('Messaging')}</span><h2 id="weixin-connector-heading">{t('WeChat direct messages')}</h2></div>
    <p>{t('Connect WeChat one-to-one text messages to a saved project. Group chats and group mentions are not supported.')}</p>
    <div className="marketplace-toolbar marketplace-connector">
      <button type="button" disabled={!!busy || pairingActive} onClick={() => void refresh()}>{t(busy === 'refresh' ? 'Refreshing…' : 'Refresh WeChat status')}</button>
      {overview?.can_manage && <button type="button" disabled={!!busy || pairingActive || !overview.available} onClick={connect}>{t(busy === 'connect' ? 'Connecting…' : 'Connect WeChat with QR code')}</button>}
    </div>
    {error && <p className="connectors-error" role="alert">{t(error)}</p>}
    {notice && <p role="status">{t(notice)}</p>}
    {overview && !overview.can_manage && <p>{t('An administrator manages WeChat connections for this organization.')}</p>}
    {overview?.can_manage && <>
      <p>{t(overview.identity_mode === 'local_only'
        ? 'Personal use on this computer. Select a local project for your WeChat messages.'
        : 'This connection uses the current administrator identity. Project membership is checked again for each run.')}</p>
      {!overview.delivery_enabled && <p>{t('Reply delivery is disabled in Settings. Connecting or enabling an account does not turn it on.')} <a href="/app/settings#permissions">{t('Review integration permissions')}</a></p>}
      {pairing && <div className="weixin-pairing marketplace-connector" aria-label={t('WeChat authorization')}>
        <p role="status">{t(pairingLabels[pairing.status])}</p>
        {pairingActive && <>
          {qrImage && <img src={qrImage} alt={t('WeChat authorization QR code')} width={240} height={240} referrerPolicy="no-referrer" />}
          {!qrImage && pairing.status !== 'need_verifycode' && <p role="alert">{t('The QR image is unavailable. Cancel and start a new connection.')}</p>}
          <p>{t('Expires: {date}', { date: new Date(pairing.expires_at).toLocaleString(language === 'zh-CN' ? 'zh-CN' : 'en') })}</p>
          {pairing.status === 'need_verifycode' && <form onSubmit={(event) => { event.preventDefault(); verify() }}>
            <label>{t('WeChat verification code')}<input type="password" inputMode="numeric" pattern="[0-9]{1,12}" autoComplete="off" maxLength={12} value={verifyCode} disabled={!!busy} onChange={(event) => setVerifyCode(event.target.value)} /></label>
            <button type="submit" disabled={!!busy || !/^[0-9]{1,12}$/.test(verifyCode.trim())}>{t('Submit verification code')}</button>
          </form>}
          <button type="button" disabled={busy === 'cancel'} onClick={cancel}>{t('Cancel WeChat connection')}</button>
        </>}
      </div>}
      <div className="connectors-list">
        {overview.accounts.map((account) => <AccountBinding key={`${account.id}:${account.revision}`} account={account} projects={overview.projects}
          disabled={!!busy || pairingActive} onSave={updateAccount} onRevoke={revoke} />)}
      </div>
    </>}
  </section>
}
