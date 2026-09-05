import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { errorMessage } from '../api/client'
import {
  cancelCompanionRun,
  getCompanionOverview,
  getCompanionSession,
  logoutCompanion,
  pairCompanion,
  resolveCompanionPermission,
  startCompanionRun,
  type CompanionOverview,
  type CompanionSession,
  type PairingFragment,
} from '../api/companion'
import type { PendingPermissionView, PermissionAnswers, RunRecord } from '../api/types'
import { useT } from '../i18n'
import { PermissionDetails, PermissionQuestionCard } from './PermissionRequest'

function canCancel(run: RunRecord): boolean {
  return ['queued', 'running', 'waiting_permission', 'paused'].includes(run.status)
}

export default function MobileCompanion({ initialSession, entry }: {
  initialSession: CompanionSession | null
  entry: PairingFragment
}) {
  const t = useT()
  const invitation = useRef(entry.kind === 'pairing' ? entry.input : null)
  const [needsPairing, setNeedsPairing] = useState(entry.kind === 'pairing')
  const [deviceName, setDeviceName] = useState('')
  const [session, setSession] = useState(initialSession)
  const [overview, setOverview] = useState<CompanionOverview | null>(null)
  const [projectId, setProjectId] = useState('')
  const [prompt, setPrompt] = useState('')
  const [confirmed, setConfirmed] = useState<Record<string, boolean>>({})
  const [busy, setBusy] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState(entry.kind === 'invalid' ? t('This pairing link is invalid. Request a new link from your desktop.') : '')
  const [notice, setNotice] = useState('')
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
      const nextSession = await getCompanionSession()
      if (!current()) return
      setSession(nextSession)
      if (nextSession.device) {
        invitation.current = null
        setNeedsPairing(false)
      }
      if (nextSession.status === 'approved') {
        const nextOverview = await getCompanionOverview()
        if (!current()) return
        setOverview(nextOverview)
        setProjectId((id) => nextOverview.projects.some((project) => project.id === id) ? id : nextOverview.projects[0]?.id ?? '')
        setConfirmed((values) => Object.fromEntries(Object.entries(values).filter(([id]) => nextOverview.pending_permissions.some((item) => item.id === id))))
      } else {
        setOverview(null)
        setConfirmed({})
      }
      if (current()) setError('')
    } catch (cause) {
      if (current()) {
        setError(errorMessage(cause))
        // Fail closed if the credential was revoked between session and overview.
        setOverview(null)
        setConfirmed({})
      }
    } finally {
      if (polling.current === token) polling.current = null
      if (current()) setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    mounted.current = true
    lifecycle.current += 1
    polling.current = null
    if (!needsPairing && entry.kind !== 'invalid') void refresh()
    const timer = window.setInterval(() => {
      if (!invitation.current && entry.kind !== 'invalid') void refresh()
    }, 5_000)
    return () => {
      mounted.current = false
      lifecycle.current += 1
      window.clearInterval(timer)
    }
  }, [entry.kind, needsPairing, refresh])

  const perform = async <T,>(key: string, action: () => Promise<T>, apply: (result: T) => void) => {
    if (mutation.current) return
    mutation.current = true
    revision.current += 1
    polling.current = null
    const generation = lifecycle.current
    setBusy(key)
    setError('')
    try {
      const result = await action()
      if (mounted.current && lifecycle.current === generation) apply(result)
    } catch (cause) {
      if (mounted.current && lifecycle.current === generation) setError(errorMessage(cause))
    } finally {
      mutation.current = false
      if (mounted.current && lifecycle.current === generation) { setBusy(''); setRefreshing(false) }
    }
  }

  const pair = (event: FormEvent) => {
    event.preventDefault()
    const input = invitation.current
    if (!input || !deviceName.trim()) return
    void perform('pair', () => pairCompanion(input, deviceName.trim()), (result) => {
      invitation.current = null
      setSession(result)
      setNeedsPairing(false)
      setNotice(t('Pairing requested. Confirm the matching code on your desktop.'))
    })
  }

  const decide = (item: PendingPermissionView, decision: 'allow_once' | 'deny', answers?: PermissionAnswers) => {
    if (!overview || session?.status !== 'approved' || !overview.pending_permissions.some((value) => value.id === item.id)) return
    if (decision !== 'deny' && (item.tool_name === 'AskUserQuestion' ? !answers : item.destructive && !confirmed[item.id])) return
    void perform(item.id, () => resolveCompanionPermission(item.run_id, item.id, decision, answers), () => {
      setOverview((value) => value ? { ...value, pending_permissions: value.pending_permissions.filter((permission) => permission.id !== item.id) } : null)
      setConfirmed((values) => ({ ...values, [item.id]: false }))
      setNotice(t('Decision sent'))
    })
  }

  const launch = (event: FormEvent) => {
    event.preventDefault()
    if (!overview?.device.allow_run_start || session?.status !== 'approved' || !prompt.trim() || !overview.projects.some((project) => project.id === projectId)) return
    void perform('start', () => startCompanionRun(projectId, prompt.trim()), (run) => {
      setOverview((value) => value ? { ...value, runs: [run, ...value.runs.filter((item) => item.id !== run.id)] } : null)
      setPrompt('')
      setNotice(t('Run started: {id}', { id: run.id }))
    })
  }

  const cancel = (run: RunRecord) => {
    if (!overview || session?.status !== 'approved' || !canCancel(run)) return
    void perform(run.id, () => cancelCompanionRun(run.id), (result) => {
      setOverview((value) => value ? { ...value, runs: value.runs.map((item) => item.id === result.id ? result : item) } : null)
      setNotice(t('Cancellation requested. Completed side effects are not undone.'))
    })
  }

  const disconnect = () => {
    void perform('logout', logoutCompanion, () => {
      setOverview(null)
      setConfirmed({})
      setSession({ status: 'unpaired', device: null })
      setPrompt('')
      setNotice(t('This device has been disconnected. Pair again to regain access.'))
    })
  }

  return <>
    {error && <p className="inbox-error" role="alert">{error}</p>}
    {notice && <p className="mobile-notice" role="status">{notice}</p>}
    <section className="mobile-settings" aria-labelledby="mobile-session-heading">
      <div className="mobile-section-heading"><h2 id="mobile-session-heading">{t('This device')}</h2><button type="button" disabled={refreshing || Boolean(busy)} onClick={() => void refresh()}>{t(refreshing ? 'Refreshing…' : 'Refresh')}</button></div>
      {needsPairing ? <form className="mobile-run-form" onSubmit={pair}>
        <p>{t('Name this device, then compare the verification code with your trusted desktop. The link does not grant access by itself.')}</p>
        <label>{t('Device name')}<input aria-label={t('Device name')} value={deviceName} maxLength={80} autoComplete="off" disabled={Boolean(busy)} onChange={(event) => setDeviceName(event.target.value)} placeholder={t('For example, my phone')} /></label>
        <button type="submit" disabled={!deviceName.trim() || Boolean(busy)}>{t(busy === 'pair' ? 'Requesting…' : 'Request pairing')}</button>
      </form> : <>
        {!session && !error && <p role="status">{t('Checking device session…')}</p>}
        {session?.status === 'pending' && <div className="mobile-pending-device">
          <h3>{t('Waiting for desktop approval')}</h3><p>{session.device?.device_name}</p>
          <p>{t('Compare this code on your trusted desktop. Never approve a different code.')}</p>
          <strong className="mobile-verification-code" aria-label={t('Phone verification code')}>{session.device?.verification_code}</strong>
          <p>{t('No project data is available until the desktop approves this device.')}</p>
        </div>}
        {session?.status === 'approved' && <p className="mobile-notice">{t('Trusted device: {name}', { name: session.device?.device_name ?? '—' })}</p>}
        {session?.status === 'rejected' && <p className="mobile-control-warning" role="status">{t('The desktop rejected this pairing request. Ask the owner for a new invitation if this was your device.')}</p>}
        {session && ['unpaired', 'revoked', 'expired'].includes(session.status) && <p className="mobile-control-warning" role="status">{t('This device is not authorized. Create a new pairing link from the desktop and open it on this phone.')}</p>}
        {session?.status === 'disabled' && <p className="mobile-control-warning" role="status">{t('The companion service is disabled. Ask the desktop owner to configure secure remote access.')}</p>}
        {session?.device && <button className="mobile-disconnect" type="button" disabled={Boolean(busy)} onClick={disconnect}>{t('Disconnect this device')}</button>}
      </>}
    </section>
    {overview && session?.status === 'approved' && <>
      <section className="mobile-settings" aria-labelledby="mobile-projects-heading"><h2 id="mobile-projects-heading">{t('Authorized projects')}</h2>
        {overview.projects.map((project) => <article className="mobile-device-card" key={project.id}><h3>{project.name}</h3><p>{project.objective}</p></article>)}
      </section>
      <section className="mobile-settings" aria-labelledby="mobile-pending-heading">
        <h2 id="mobile-pending-heading">{t('Pending approvals')}</h2>
        <p className="mobile-service-note">{t('Mobile approvals apply once only. Persistent grants are managed from the desktop.')}</p>
        {overview.pending_permissions.length ? overview.pending_permissions.map((item) => item.tool_name === 'AskUserQuestion' ? <PermissionQuestionCard
          key={item.id} toolInput={item.tool_input} scope={item.scope} timeoutSeconds={item.timeout_seconds} busy={Boolean(busy)}
          onSubmit={(answers) => decide(item, 'allow_once', answers)} onDeny={() => decide(item, 'deny')}
        /> : <article className="mobile-permission" key={item.id}>
          <strong>{item.tool_name}</strong><p>{item.run_id}</p>
          <PermissionDetails toolName={item.tool_name} toolInput={item.tool_input} scope={item.scope} destructive={item.destructive} persistable={false} timeoutSeconds={item.timeout_seconds} />
          {item.destructive && <label className="mobile-check-row mobile-destructive-confirm"><input type="checkbox" checked={confirmed[item.id] ?? false} disabled={Boolean(busy)} onChange={(event) => setConfirmed((values) => ({ ...values, [item.id]: event.target.checked }))} /><span>{t('I reviewed the exact target and confirm this destructive action.')}</span></label>}
          <div className="mobile-actions"><button type="button" disabled={Boolean(busy) || (item.destructive && !confirmed[item.id])} onClick={() => decide(item, 'allow_once')}>{t('Allow once')}</button><button type="button" disabled={Boolean(busy)} onClick={() => decide(item, 'deny')}>{t('Deny')}</button></div>
        </article>) : <p className="mobile-empty">{t('No pending permissions.')}</p>}
      </section>
      <section className="mobile-settings" aria-labelledby="mobile-run-heading"><h2 id="mobile-run-heading">{t('Start a remote run')}</h2>
        {overview.device.allow_run_start ? <form className="mobile-run-form" onSubmit={launch}>
          <label><span>{t('Project')}</span><select aria-label={t('Project')} value={projectId} disabled={Boolean(busy)} onChange={(event) => setProjectId(event.target.value)}>{overview.projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}</select></label>
          <label><span>{t('Prompt')}</span><textarea aria-label={t('Prompt')} value={prompt} disabled={Boolean(busy)} onChange={(event) => setPrompt(event.target.value)} rows={4} /></label>
          <button type="submit" disabled={!projectId || !prompt.trim() || Boolean(busy)}>{t(busy === 'start' ? 'Starting…' : 'Start run')}</button>
        </form> : <p className="mobile-empty">{t('The desktop owner has not allowed this device to start runs.')}</p>}
      </section>
      <section className="mobile-settings" aria-labelledby="mobile-runs-heading"><h2 id="mobile-runs-heading">{t('Recent runs')}</h2>
        {!overview.runs.length && <p className="mobile-empty">{t('No runs in the authorized projects.')}</p>}
        {overview.runs.map((run) => <article className="mobile-device-card" key={run.id}><div className="mobile-device-heading"><h3>{run.prompt}</h3><span>{t(run.status)}</span></div><p>{run.id}</p>{run.error && <p>{run.error}</p>}{canCancel(run) && <button type="button" disabled={Boolean(busy)} onClick={() => cancel(run)}>{t('Cancel run')}</button>}</article>)}
      </section>
      <section className="mobile-settings" aria-labelledby="mobile-notifications-heading"><h2 id="mobile-notifications-heading">{t('Project notifications')}</h2>
        {!overview.notifications.length && <p className="mobile-empty">{t('No notifications for these projects.')}</p>}
        {overview.notifications.map((item) => <article className="mobile-device-card" key={item.id}><h3>{item.title}</h3><p>{item.body}</p><small>{new Date(item.created_at).toLocaleString()}</small></article>)}
      </section>
    </>}
  </>
}
