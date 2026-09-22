import { useEffect, useRef, useState } from 'react'
import {
  confirmMaintenance, discardMaintenance, getMaintenanceStatus, maintenanceError, prepareMaintenance,
  type MaintenanceOperation, type MaintenancePrepare, type MaintenanceStatus,
} from '../api/maintenanceConsole'
import { I18nContext, readLanguagePreference, resolveLanguage, useT, type ResolvedLanguage } from '../i18n'
import Brand from './Brand'
import './maintenance-page.css'

const activeStates = new Set(['preparing', 'reviewed', 'publishing'])

function MaintenanceSession({ token }: { token: string }) {
  const t = useT()
  const [status, setStatus] = useState<MaintenanceStatus | null>(null)
  const [operation, setOperation] = useState<MaintenanceOperation | null>(null)
  const [backupId, setBackupId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [uncertain, setUncertain] = useState(false)
  const [pending, setPending] = useState<MaintenancePrepare | null>(null)
  const active = useRef(true)
  const inflight = useRef(false)

  const load = async () => {
    const result = await getMaintenanceStatus(token)
    if (!active.current) return
    setStatus(result)
    setOperation((current) => result.operations.find((row) => row.request_id === current?.request_id) ?? result.operations.find((row) => activeStates.has(row.state)) ?? null)
    setPending((current) => current && !result.operations.some((row) => row.request_id === current.request_id) ? current : null)
    setBackupId((current) => result.backups.some((row) => row.id === current) ? current : result.backups[0]?.id ?? '')
  }
  useEffect(() => {
    let mounted = true
    active.current = true
    void getMaintenanceStatus(token).then((result) => {
      if (!mounted) return
      setStatus(result); setBackupId(result.backups[0]?.id ?? '')
      setOperation(result.operations.find((row) => activeStates.has(row.state)) ?? null)
    }).catch((failure: unknown) => { if (mounted) setError(failure instanceof Error ? failure.message : 'maintenance_request_failed') })
    return () => { mounted = false; active.current = false }
  }, [token])

  const run = async (action: () => Promise<void>) => {
    if (inflight.current) return
    inflight.current = true
    setBusy(true); setError('')
    try { await action() }
    catch (failure) {
      if (active.current) {
        const code = failure instanceof Error ? failure.message : 'maintenance_request_failed'
        setError(code)
        if (code === 'maintenance_access_denied') {
          setStatus(null); setOperation(null); setPending(null); setConfirmed(false); setUncertain(false)
        }
      }
    }
    finally { inflight.current = false; if (active.current) setBusy(false) }
  }
  const prepare = (body: MaintenancePrepare) => void run(async () => {
    setPending(body); setOperation(null); setConfirmed(false); setUncertain(false)
    const result = await prepareMaintenance(token, body)
    if (result.request_id !== body.request_id || result.kind !== body.kind || result.backup_id !== (body.backup_id ?? null)) throw new Error('maintenance_response_mismatch')
    if (!active.current) return
    setOperation(result); setPending(null)
    await load()
  })
  const apply = () => {
    if (!operation || !confirmed || !operation.fingerprint) return
    const reviewed = operation
    void run(async () => {
      let result: MaintenanceOperation
      try {
        result = await confirmMaintenance(token, reviewed)
        if (result.request_id !== reviewed.request_id || result.fingerprint !== reviewed.fingerprint || result.kind !== reviewed.kind || result.destination !== reviewed.destination || result.state !== 'completed') throw new Error('maintenance_response_mismatch')
      } catch (failure) {
        if (active.current) setUncertain(true)
        throw failure
      }
      if (!active.current) return
      setOperation(result); setConfirmed(false); setUncertain(false)
      await load()
    })
  }
  const discard = () => {
    if (!operation || !['preparing', 'reviewed'].includes(operation.state)) return
    const selected = operation
    void run(async () => {
      const result = await discardMaintenance(token, selected)
      if (result.request_id !== selected.request_id || result.state !== 'discarded') throw new Error('maintenance_response_mismatch')
      if (!active.current) return
      setOperation(result); setPending(null); setConfirmed(false); setUncertain(false)
      await load()
    })
  }
  const hasReview = !!pending || !!status?.operations.some((row) => activeStates.has(row.state)) || !!operation && activeStates.has(operation.state)
  return <>
    <div className="maintenance-toolbar"><button disabled={busy} onClick={() => { setConfirmed(false); void run(load) }}>{t('Refresh maintenance state')}</button>{busy && <p role="status">{t('Verifying and preparing files. Keep this page open; a disconnected request may still finish.')}</p>}</div>
    {error && <p className="maintenance-error" role="alert">{t(maintenanceError(error))}</p>}
    {uncertain && <p role="status">{t('Publication may already be committed. Retry this same review to check its result; closing this page does not cancel it.')}</p>}
    {pending && <button disabled={busy} onClick={() => prepare(pending)}>{t('Retry the same preparation')}</button>}
    {status && <>
      <div className="maintenance-grid">
        <section className="maintenance-card" aria-labelledby="backup-title">
          <p className="maintenance-step">01</p><h2 id="backup-title">{t('Create a complete backup')}</h2>
          <p>{t('Stop all application workers and shop browsers first. All conversations must be finished or cancelled.')}</p>
          <dl><dt>{t('Application data')}</dt><dd><code>{status.data_directory}</code></dd><dt>{t('Application Skills')}</dt><dd><code>{status.plugin_directory}</code></dd></dl>
          <button disabled={busy || hasReview} onClick={() => prepare({ request_id: crypto.randomUUID().replaceAll('-', ''), kind: 'backup' })}>{t('Prepare backup review')}</button>
        </section>
        <section className="maintenance-card" aria-labelledby="restore-title">
          <p className="maintenance-step">02</p><h2 id="restore-title">{t('Restore an independent copy')}</h2>
          <p>{t('Choose a trusted full backup from the private backup folder. Validation and interrupted Skill review recovery run before you confirm.')}</p>
          <code>{status.backup_directory}</code>
          <label>{t('Available backup')}<select value={backupId} disabled={busy || hasReview || !status.backups.length} onChange={(event) => setBackupId(event.target.value)}><option value="">{t('Select a backup')}</option>{status.backups.map((row) => <option key={row.id} value={row.id}>{row.name}</option>)}</select></label>
          {!status.backups.length && <p>{t('Create a backup above, or place an existing compatible cold-backup directory in this folder and refresh.')}</p>}
          <button disabled={busy || hasReview || !backupId} onClick={() => prepare({ request_id: crypto.randomUUID().replaceAll('-', ''), kind: 'restore', backup_id: backupId })}>{t('Prepare restore review')}</button>
        </section>
      </div>
      {operation && <section className="maintenance-card maintenance-review" aria-labelledby="review-title">
        <p className="maintenance-step">{t(operation.state)}</p><h2 id="review-title">{t(operation.kind === 'backup' ? 'Backup review' : 'Restore review')}</h2>
        <p>{t('Reviewed at')}: {operation.reviewed_at ?? operation.created_at}</p>
        {operation.kind === 'restore' && <p>{t('Source backup')}: {status.backups.find((row) => row.id === operation.backup_id)?.name ?? t('Source removed; the prepared copy remains available for verification.')}</p>}
        <dl className="maintenance-counts"><div><dt>{t('Projects')}</dt><dd>{operation.summary.projects ?? '—'}</dd></div><div><dt>{t('Conversations')}</dt><dd>{operation.summary.runs ?? '—'}</dd></div><div><dt>{t('Stored files')}</dt><dd>{operation.summary.files ?? '—'}</dd></div></dl>
        <p>{t('New output directory')}<code>{operation.destination}</code></p>
        {operation.summary.proposal_states && <p>{t('Recovered Skill review states')}: {Object.entries(operation.summary.proposal_states).map(([state, count]) => `${t(state)}: ${count}`).join(' · ')}</p>}
        {operation.state === 'preparing' && <p>{t('Preparation was interrupted or is still running. Retry the same request to recover, or discard its private staging copy.')}</p>}
        {['reviewed', 'publishing'].includes(operation.state) && <>
          <p>{t('This copy includes credentials, browser logins, conversations and saved automation. Existing backups and source directories remain unchanged. Store it privately; the directory is not encrypted.')}</p>
          <p>{t('Restoring writes a new directory and starts no jobs. Activating it later can resume saved automation and requires a separate deployment decision. Native sessions still depend on their original working directory.')}</p>
          <label className="maintenance-confirm"><input type="checkbox" disabled={busy} checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />{t('I reviewed the destination and understand what this complete copy contains.')}</label>
          <button disabled={busy || !confirmed} onClick={apply}>{t(uncertain || operation.state === 'publishing' ? 'Retry reviewed publication' : 'Confirm reviewed copy')}</button>
        </>}
        {operation.state === 'preparing' && <button disabled={busy} onClick={() => prepare({ request_id: operation.request_id, kind: operation.kind, backup_id: operation.backup_id })}>{t('Retry the same preparation')}</button>}
        {['preparing', 'reviewed'].includes(operation.state) && <button disabled={busy} onClick={discard}>{t('Discard uncommitted review')}</button>}
        {operation.state === 'completed' && <div role="status"><strong>{t('Copy published successfully')}</strong>{operation.kind === 'restore' && <><p>{t('After stopping the original deployment, configure these restored directories. Review restored schedules and external connections before starting the application.')}</p><dl><dt>OPHIRA_DATA_DIR</dt><dd><code>{operation.destination}/data</code></dd><dt>OPHIRA_CAPABILITY_PLUGIN_DIR</dt><dd><code>{operation.destination}/application-plugin</code></dd></dl></>}</div>}
      </section>}
      {!!status.operations.length && <section className="maintenance-card"><h2>{t('Maintenance history')}</h2><ul className="maintenance-history">{status.operations.map((row) => <li key={row.request_id}><button disabled={busy} onClick={() => { setOperation(row); setConfirmed(false); setUncertain(false) }}>{t(row.kind === 'backup' ? 'Backup review' : 'Restore review')} · {row.created_at} · {t(row.state)}</button></li>)}</ul></section>}
    </>}
  </>
}

export default function MaintenancePage({ initialToken = '' }: { initialToken?: string }) {
  const t = useT()
  const [token, setToken] = useState(initialToken)
  const [entered, setEntered] = useState('')
  return <main className="maintenance-page">
    <header><Brand /><p className="maintenance-step">{t('Local operator console')}</p><h1>{t('Full application backup and recovery')}</h1><p>{t('Review a complete copy of this installation. Maintenance stays on this computer and never starts an Agent, schedule or external delivery.')}</p></header>
    {token ? <MaintenanceSession key={token} token={token} /> : <section className="maintenance-card">
      <h2>{t('Open your private maintenance link')}</h2><p>{t('Use the link printed by the local maintenance command, or enter its session key. The key expires after eight hours and is not saved in this browser.')}</p>
      <label>{t('Maintenance session key')}<input type="password" autoComplete="off" value={entered} onChange={(event) => setEntered(event.target.value)} /></label>
      <button disabled={!/^[A-Za-z0-9_-]{40,128}$/.test(entered)} onClick={() => { setToken(entered); setEntered('') }}>{t('Connect to maintenance')}</button>
    </section>}
  </main>
}

export function MaintenanceApp({ initialToken }: { initialToken: string }) {
  const [language, setLanguage] = useState<ResolvedLanguage>(() => resolveLanguage(readLanguagePreference()))
  return <I18nContext.Provider value={language}>
    <div className="maintenance-language"><label>Language / 语言 <select value={language} onChange={(event) => setLanguage(event.target.value as ResolvedLanguage)}><option value="en">English</option><option value="zh-CN">简体中文</option></select></label></div>
    <MaintenancePage initialToken={initialToken} />
  </I18nContext.Provider>
}
