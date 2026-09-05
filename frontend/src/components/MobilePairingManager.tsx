import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { errorMessage, listProjects } from '../api/client'
import {
  createMobilePairing,
  decideMobilePairing,
  getMobileStatus,
  listMobilePairings,
  revokeMobilePairing,
  type MobileStatus,
  type PairingView,
} from '../api/companion'
import type { OpportunityProject } from '../api/types'
import { useT } from '../i18n'

export default function MobilePairingManager() {
  const t = useT()
  const [status, setStatus] = useState<MobileStatus | null>(null)
  const [projects, setProjects] = useState<OpportunityProject[]>([])
  const [pairings, setPairings] = useState<PairingView[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [allowRunStart, setAllowRunStart] = useState(false)
  const [offer, setOffer] = useState<{ id: string; url: string } | null>(null)
  const [codes, setCodes] = useState<Record<string, string>>({})
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [ready, setReady] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [busy, setBusy] = useState('')
  const mounted = useRef(false)
  const lifecycle = useRef(0)
  const revision = useRef(0)
  const polling = useRef<object | null>(null)
  const mutation = useRef(false)

  const refresh = useCallback(async () => {
    if (mutation.current || polling.current) return
    const token = {}
    const generation = lifecycle.current
    const version = revision.current
    polling.current = token
    setRefreshing(true)
    const current = () => mounted.current && lifecycle.current === generation && revision.current === version
    try {
      const nextStatus = await getMobileStatus()
      if (!current()) return
      setStatus(nextStatus)
      if (!nextStatus.enabled) {
        setProjects([])
        setPairings([])
        setOffer(null)
      } else {
        const [nextProjects, nextPairings] = await Promise.all([listProjects(), listMobilePairings()])
        if (!current()) return
        setProjects(nextProjects)
        setPairings(nextPairings)
        setSelected((ids) => ids.filter((id) => nextProjects.some((project) => project.id === id)))
        setOffer((value) => value && nextPairings.some((item) => item.id === value.id && item.status === 'offered') ? value : null)
      }
      if (current()) { setError(''); setReady(true) }
    } catch (cause) {
      if (current()) { setError(errorMessage(cause)); setReady(false) }
    } finally {
      if (polling.current === token) polling.current = null
      if (current()) setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    mounted.current = true
    lifecycle.current += 1
    polling.current = null
    void refresh()
    const timer = window.setInterval(() => void refresh(), 5_000)
    return () => {
      mounted.current = false
      lifecycle.current += 1
      window.clearInterval(timer)
    }
  }, [refresh])

  const act = async (key: string, action: () => Promise<void>) => {
    if (mutation.current || !ready || !status?.enabled) return
    mutation.current = true
    revision.current += 1
    polling.current = null
    const generation = lifecycle.current
    setBusy(key)
    setError('')
    try { await action() } catch (cause) {
      if (mounted.current && lifecycle.current === generation) setError(errorMessage(cause))
    } finally {
      mutation.current = false
      if (mounted.current && lifecycle.current === generation) {
        setBusy('')
        setRefreshing(false)
      }
    }
  }

  const create = (event: FormEvent) => {
    event.preventDefault()
    const projectIds = selected.filter((id) => projects.some((project) => project.id === id))
    if (!projectIds.length) return
    const generation = lifecycle.current
    void act('create', async () => {
      const result = await createMobilePairing({ project_ids: projectIds, allow_run_start: allowRunStart })
      if (!mounted.current || lifecycle.current !== generation) return
      setPairings((items) => [result.pairing, ...items])
      setOffer({ id: result.pairing.id, url: result.pairing_url })
      setNotice(t('Open this one-time link on your phone. Do not share it with anyone else.'))
    })
  }

  const decide = (item: PairingView, decision: 'approve' | 'reject') => {
    const code = codes[item.id]?.trim()
    if (decision === 'approve' && (!code || code !== item.verification_code)) return
    const generation = lifecycle.current
    void act(item.id, async () => {
      const result = await decideMobilePairing(item.id, decision, decision === 'approve' ? code : undefined)
      if (!mounted.current || lifecycle.current !== generation) return
      setPairings((items) => items.map((value) => value.id === result.id ? result : value))
      setCodes((values) => ({ ...values, [item.id]: '' }))
      setNotice(t(decision === 'approve' ? 'Device approved for the selected projects.' : 'Pairing rejected.'))
    })
  }

  const revoke = (item: PairingView) => {
    const generation = lifecycle.current
    void act(item.id, async () => {
      const result = await revokeMobilePairing(item.id)
      if (!mounted.current || lifecycle.current !== generation) return
      setPairings((items) => items.map((value) => value.id === result.id ? result : value))
      setOffer((value) => value?.id === result.id ? null : value)
      setNotice(t('Device access revoked.'))
    })
  }

  return <>
    {error && <p className="inbox-error" role="alert">{error}</p>}
    {notice && <p className="mobile-notice" role="status">{notice}</p>}
    <section className="mobile-settings" aria-labelledby="mobile-setup-heading">
      <div className="mobile-section-heading"><h2 id="mobile-setup-heading">{t('Secure pairing')}</h2><button type="button" disabled={refreshing || Boolean(busy)} onClick={() => void refresh()}>{t(refreshing ? 'Refreshing…' : 'Refresh')}</button></div>
      {!status && !error && <p role="status">{t('Checking pairing service…')}</p>}
      {status && !status.enabled && <div className="mobile-control-warning" role="status"><strong>{t('Pairing service is disabled')}</strong><p>{t('Configure an authenticated HTTPS public origin and enable the backend mobile pairing service. A local preference cannot enable remote access.')}</p></div>}
      {status?.enabled && <>
        <p className="mobile-service-note">{t('Service origin: {origin}', { origin: status.public_origin ?? '—' })}<br />{t('Pairing links expire after {minutes} minutes. Trusted devices expire after {days} days.', { minutes: Math.ceil(status.pairing_ttl_seconds / 60), days: Math.ceil(status.device_max_age_seconds / 86400) })}</p>
        <form className="mobile-run-form" onSubmit={create}>
          <fieldset disabled={!ready || Boolean(busy)}><legend>{t('Projects this device may access')}</legend>
            {projects.length ? projects.map((project) => <label className="mobile-check-row" key={project.id}><input type="checkbox" checked={selected.includes(project.id)} onChange={(event) => setSelected((ids) => event.target.checked ? [...ids, project.id] : ids.filter((id) => id !== project.id))} /><span>{project.name}</span></label>) : <p>{t('Create a project in the workspace before pairing a device.')}</p>}
          </fieldset>
          <label className="mobile-check-row"><input type="checkbox" checked={allowRunStart} disabled={!ready || Boolean(busy)} onChange={(event) => setAllowRunStart(event.target.checked)} /><span>{t('Allow this device to start new runs in these projects')}</span></label>
          <p className="mobile-service-note">{t('Paired devices may view these projects, cancel their runs and decide individual approvals. They cannot manage credentials, devices or persistent permission grants.')}</p>
          <button type="submit" disabled={!ready || !selected.length || Boolean(busy)}>{t(busy === 'create' ? 'Creating…' : 'Create pairing link')}</button>
        </form>
        {offer && <section className="mobile-offer" aria-label={t('One-time pairing link')}>
          <label>{t('Open privately on your phone')}<input aria-label={t('Pairing link')} readOnly value={offer.url} autoComplete="off" spellCheck={false} onFocus={(event) => event.target.select()} /></label>
          <p>{t('After requesting pairing on your phone, compare both verification codes below. Opening the link alone never approves a device.')}</p>
          <button type="button" onClick={() => { setOffer(null); setNotice('') }}>{t('Hide link')}</button>
        </section>}
      </>}
    </section>
    {status?.enabled && <section className="mobile-settings" aria-labelledby="mobile-devices-heading">
      <h2 id="mobile-devices-heading">{t('Pairing requests and trusted devices')}</h2>
      {!pairings.length && <p className="mobile-empty">{t('No paired devices or active invitations.')}</p>}
      {pairings.map((item) => <article className="mobile-device-card" key={item.id}>
        <div className="mobile-device-heading"><h3>{item.device_name || t('Unclaimed invitation')}</h3><span>{t(item.status)}</span></div>
        <p>{item.project_ids.map((id) => projects.find((project) => project.id === id)?.name ?? id).join(' · ')}</p>
        <p>{t(item.allow_run_start ? 'New run creation allowed' : 'New run creation not allowed')}</p>
        <p>{t('Expires: {date}', { date: new Date(item.expires_at).toLocaleString() })}</p>
        {item.status === 'pending' && <>
          <p>{t('Compare this code with the phone requesting access. Only approve if both codes match and you recognize the device.')}</p>
          <strong className="mobile-verification-code" aria-label={t('Desktop verification code')}>{item.verification_code}</strong>
          <label className="mobile-code-entry">{t('Type the code shown on your phone')}<input aria-label={t('Phone verification code')} value={codes[item.id] ?? ''} autoComplete="off" spellCheck={false} maxLength={32} disabled={!ready || Boolean(busy)} onChange={(event) => setCodes((values) => ({ ...values, [item.id]: event.target.value }))} /></label>
          <div className="mobile-actions"><button type="button" disabled={!ready || Boolean(busy) || !item.verification_code || codes[item.id]?.trim() !== item.verification_code} onClick={() => decide(item, 'approve')}>{t('Approve device')}</button><button type="button" disabled={!ready || Boolean(busy)} onClick={() => decide(item, 'reject')}>{t('Reject pairing')}</button></div>
        </>}
        {(item.status === 'approved' || item.status === 'offered') && <button type="button" disabled={!ready || Boolean(busy)} onClick={() => revoke(item)}>{t(item.status === 'approved' ? 'Revoke device' : 'Revoke invitation')}</button>}
      </article>)}
    </section>}
  </>
}
