import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import {
  errorMessage,
  listPendingPermissions,
  listProjects,
  resolvePermission,
  startRun,
} from '../api/client'
import type { OpportunityProject, PendingPermissionView, PermissionAnswers, PermissionDecision } from '../api/types'
import { useT } from '../i18n'
import { loadLocalSettings, saveLocalSettings, type LocalSettings } from '../localSettings'
import Brand from './Brand'
import { PermissionDetails, PermissionQuestionCard } from './PermissionRequest'
import WorkspaceNav from './WorkspaceNav'
import './workspace-composer.css'
import './mobile-page.css'

function MobileToggle({
  title,
  description,
  checked,
  onChange,
}: {
  title: string
  description: string
  checked: boolean
  onChange: (checked: boolean) => void
}) {
  const t = useT()
  return (
    <label className="mobile-setting-row">
      <span><strong>{t(title)}</strong><small>{t(description)}</small></span>
      <input type="checkbox" role="switch" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    </label>
  )
}

export default function MobilePage() {
  const t = useT()
  const [settings, setSettings] = useState<LocalSettings>(loadLocalSettings)
  const [pending, setPending] = useState<PendingPermissionView[]>([])
  const [projects, setProjects] = useState<OpportunityProject[]>([])
  const [projectId, setProjectId] = useState('')
  const [prompt, setPrompt] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [deciding, setDeciding] = useState('')
  const [starting, setStarting] = useState(false)
  const mountedRef = useRef(false)
  const refreshGenerationRef = useRef(0)
  const refreshInFlightRef = useRef<number | null>(null)
  const permissionInFlightRef = useRef(false)
  const runStartInFlightRef = useRef(false)

  useEffect(() => {
    saveLocalSettings(settings)
  }, [settings])

  const refresh = useCallback(async (silent = false, generation = refreshGenerationRef.current) => {
    if (refreshInFlightRef.current === generation) return
    refreshInFlightRef.current = generation
    if (!silent && mountedRef.current) setRefreshing(true)
    const results = await Promise.allSettled([
      settings.computerDiscoverable ? listPendingPermissions() : Promise.resolve([]),
      listProjects(),
    ])
    if (mountedRef.current && refreshGenerationRef.current === generation) {
      const [permissionResult, projectResult] = results
      if (permissionResult.status === 'fulfilled') setPending(permissionResult.value)
      if (projectResult.status === 'fulfilled') {
        const projectList = projectResult.value
        setProjects(projectList)
        setProjectId((current) => (
          current && projectList.some((project) => project.id === current)
            ? current
            : projectList[0]?.id ?? ''
        ))
      }
      const failures = results
        .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
        .map((result) => errorMessage(result.reason))
      setError(failures.join(' · '))
      setRefreshing(false)
    }
    if (refreshInFlightRef.current === generation) refreshInFlightRef.current = null
  }, [settings.computerDiscoverable])

  useEffect(() => {
    const generation = refreshGenerationRef.current + 1
    refreshGenerationRef.current = generation
    mountedRef.current = true
    void refresh(false, generation)
    const timer = window.setInterval(() => void refresh(true, generation), 10_000)
    return () => {
      mountedRef.current = false
      if (refreshGenerationRef.current === generation) refreshGenerationRef.current += 1
      window.clearInterval(timer)
    }
  }, [refresh])

  const update = <Key extends keyof LocalSettings>(key: Key, value: LocalSettings[Key]) => {
    setSettings((current) => ({ ...current, [key]: value }))
  }

  const decide = async (
    item: PendingPermissionView,
    decision: PermissionDecision,
    answers?: PermissionAnswers,
  ) => {
    if (!settings.computerDiscoverable || permissionInFlightRef.current) return
    if (item.tool_name === 'AskUserQuestion' && decision !== 'deny' && !answers) {
      setError(t('Answer every question before allowing this request.'))
      return
    }
    permissionInFlightRef.current = true
    setDeciding(item.id)
    setError('')
    try {
      await resolvePermission(item.run_id, item.id, decision, answers)
      setPending((current) => current.filter((entry) => entry.id !== item.id))
      setNotice(t('Decision sent'))
    } catch (decideError) {
      setError(errorMessage(decideError))
    } finally {
      permissionInFlightRef.current = false
      setDeciding('')
    }
  }

  const launch = async (event: FormEvent) => {
    event.preventDefault()
    if (!settings.computerDiscoverable || !projectId || !prompt.trim() || runStartInFlightRef.current) return
    runStartInFlightRef.current = true
    setStarting(true)
    setError('')
    try {
      const run = await startRun(projectId, prompt.trim())
      setPrompt('')
      setNotice(t('Run started: {id}', { id: run.id }))
    } catch (startError) {
      setError(errorMessage(startError))
    } finally {
      runStartInFlightRef.current = false
      setStarting(false)
    }
  }

  return (
    <div className="mobile-shell">
      <aside className="conversation-sidebar mobile-sidebar">
        <a className="conversation-sidebar-brand" href="/" aria-label={t('Back to TradeMind website')}><Brand /></a>
        <a className="conversation-new-research" href="/app"><span aria-hidden="true">＋</span> {t('New task')}</a>
        <WorkspaceNav active="mobile" />
        <div className="mobile-sidebar-note"><strong>{t('Companion access')}</strong><p>{t('Remote controls stay off until this device is explicitly enabled. Do not expose this local build to a network without authentication and secure pairing.')}</p></div>
      </aside>

      <main className="mobile-main">
        <header className="mobile-hero">
          <span>{pending.length ? t('{n} waiting', { n: pending.length }) : settings.computerDiscoverable ? t('Remote control') : t('Remote control off')}</span>
          <h1>{t('Mobile approval')}</h1>
          <p>{t('Review pending tool requests and start a new run without sitting at the desktop session.')}</p>
        </header>
        {error && <p className="inbox-error" role="alert">{error}</p>}
        {notice && <p className="mobile-notice">{notice}</p>}
        {!settings.computerDiscoverable && <p className="mobile-control-warning" role="status">{t('Enable trusted companion control below before loading requests or starting runs.')}</p>}

        <section className="mobile-settings" aria-labelledby="mobile-pending-heading">
          <div className="mobile-section-heading">
            <h2 id="mobile-pending-heading">{t('Pending approvals')}</h2>
            <button type="button" disabled={refreshing || !settings.computerDiscoverable} onClick={() => void refresh()}>{refreshing ? t('Refreshing…') : t('Refresh')}</button>
          </div>
          {pending.length ? pending.map((item) => (
            item.tool_name === 'AskUserQuestion' ? (
              <PermissionQuestionCard
                key={item.id}
                toolInput={item.tool_input}
                scope={item.scope}
                timeoutSeconds={item.timeout_seconds}
                busy={Boolean(deciding)}
                onSubmit={(answers) => void decide(item, 'allow_once', answers)}
                onDeny={() => void decide(item, 'deny')}
              />
            ) : (
              <article className="mobile-permission" key={item.id}>
                <strong>{item.tool_name}</strong>
                <p>{item.run_id}</p>
                <PermissionDetails
                  toolName={item.tool_name}
                  toolInput={item.tool_input}
                  scope={item.scope}
                  destructive={item.destructive}
                  persistable={item.persistable}
                  timeoutSeconds={item.timeout_seconds}
                />
                <div>
                  <button type="button" disabled={Boolean(deciding)} onClick={() => void decide(item, 'allow_once')}>{t('Allow once')}</button>
                  {item.persistable && !item.destructive && <button type="button" disabled={Boolean(deciding)} onClick={() => void decide(item, 'allow_run')}>{t('Allow for run')}</button>}
                  {item.persistable && !item.destructive && <button type="button" disabled={Boolean(deciding)} onClick={() => void decide(item, 'allow_persistent')}>{t('Allow persistent')}</button>}
                  <button type="button" disabled={Boolean(deciding)} onClick={() => void decide(item, 'deny')}>{t('Deny')}</button>
                </div>
              </article>
            )
          )) : <p className="mobile-empty">{t('No pending permissions.')}</p>}
        </section>

        <section className="mobile-settings" aria-labelledby="mobile-run-heading">
          <h2 id="mobile-run-heading">{t('Start a remote run')}</h2>
          <form className="mobile-run-form" onSubmit={(event) => void launch(event)}>
            <label>
              <span>{t('Project')}</span>
              <select aria-label={t('Project')} value={projectId} onChange={(event) => setProjectId(event.target.value)}>
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>{project.name}</option>
                ))}
              </select>
            </label>
            <label>
              <span>{t('Prompt')}</span>
              <textarea aria-label={t('Prompt')} value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={4} />
            </label>
            <button type="submit" disabled={!settings.computerDiscoverable || !projectId || !prompt.trim() || starting}>{starting ? t('Starting…') : t('Start run')}</button>
          </form>
        </section>

        <section className="mobile-settings" aria-labelledby="mobile-settings-heading">
          <h2 id="mobile-settings-heading">{t('Keep these settings enabled')}</h2>
          <div>
            <MobileToggle
              title="Allow this computer to be discovered and controlled"
              description="Authorized devices may request control only after the backend pairing service verifies the session."
              checked={settings.computerDiscoverable}
              onChange={(checked) => {
                update('computerDiscoverable', checked)
                if (!checked) setPending([])
              }}
            />
            <MobileToggle
              title="Keep this computer awake"
              description="Prevent sleep while a verified remote-control or Agent run is active."
              checked={settings.keepAwake}
              onChange={(checked) => update('keepAwake', checked)}
            />
          </div>
        </section>
      </main>
    </div>
  )
}
