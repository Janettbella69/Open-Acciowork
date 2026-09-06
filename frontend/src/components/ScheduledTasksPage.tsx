import { useEffect, useRef, useState, type FormEvent } from 'react'
import {
  createSchedule,
  errorMessage,
  getRun,
  listProjects,
  listSchedules,
  pauseSchedule,
  resumeSchedule,
  runSchedule,
} from '../api/client'
import type {
  OpportunityProject,
  RunRecord,
  Schedule,
  ScheduleKind,
  SchedulePermissionRule,
} from '../api/types'
import Brand from './Brand'
import TaskExperiencePanel from './TaskExperiencePanel'
import { useLanguage, useT } from '../i18n'
import './scheduled-tasks-page.css'

type ScheduleCategory = 'daily' | 'monitoring' | 'reports' | 'automation'
type ScheduleCadence = 'daily_morning' | 'daily_evening' | 'weekly_monday' | 'every_six_hours'
type SchedulePermissionPreset = 'read_only' | 'workspace_files'

type ScheduleTemplate = {
  id: string
  code: string
  name: string
  description: string
  category: ScheduleCategory
  cadence: ScheduleCadence
  kind: ScheduleKind
  prompt: string
}

const templates: ScheduleTemplate[] = [
  { id: 'morning-brief', code: 'AM', name: 'Morning opportunity brief', description: 'Summarize new market signals, priority evidence, and today’s next actions.', category: 'daily', cadence: 'daily_morning', kind: 'task', prompt: 'Prepare a morning opportunity brief with new signals, evidence gaps, and the three highest-priority actions.' },
  { id: 'end-of-day', code: 'PM', name: 'End-of-day synthesis', description: 'Summarize progress, unresolved questions, and tomorrow’s validation priorities.', category: 'daily', cadence: 'daily_evening', kind: 'task', prompt: 'Summarize today’s opportunity work, unresolved questions, and recommended priorities for tomorrow.' },
  { id: 'competitor-watch', code: 'CW', name: 'Competitor watch', description: 'Review material product, pricing, positioning, and channel changes.', category: 'monitoring', cadence: 'every_six_hours', kind: 'heartbeat', prompt: 'Monitor the defined competitors for material product, pricing, positioning, and channel changes.' },
  { id: 'market-radar', code: 'MR', name: 'Market signal radar', description: 'Track high-signal changes in buyer needs, policy, supply, and category behavior.', category: 'monitoring', cadence: 'daily_morning', kind: 'heartbeat', prompt: 'Track high-signal changes in buyer needs, policy, supply conditions, and category behavior.' },
  { id: 'weekly-review', code: 'WR', name: 'Weekly opportunity review', description: 'Review validated evidence, weakest links, and next-stage readiness.', category: 'reports', cadence: 'weekly_monday', kind: 'task', prompt: 'Create a weekly opportunity review covering evidence, weakest links, and stage-gate readiness.' },
  { id: 'quality-review', code: 'QA', name: 'Project quality review', description: 'Check unsupported claims, stale evidence, open permissions, and missing stop conditions.', category: 'reports', cadence: 'weekly_monday', kind: 'task', prompt: 'Audit active opportunity projects for unsupported claims, stale evidence, open permissions, and missing stop conditions.' },
  { id: 'supplier-follow-up', code: 'SF', name: 'Supplier follow-up queue', description: 'Prepare the next approved supplier questions from unresolved project gaps.', category: 'automation', cadence: 'daily_morning', kind: 'task', prompt: 'Prepare a supplier follow-up queue from unresolved supply gaps. Do not contact anyone without approval.' },
  { id: 'buyer-test-prep', code: 'BT', name: 'Buyer test preparation', description: 'Turn current sales assumptions into the smallest falsifiable buyer test.', category: 'automation', cadence: 'weekly_monday', kind: 'task', prompt: 'Prepare the smallest falsifiable buyer test from current sales assumptions. Do not send outreach without approval.' },
]

const categoryLabels: Record<ScheduleCategory, string> = {
  daily: 'Daily',
  monitoring: 'Monitoring',
  reports: 'Reports',
  automation: 'Automation',
}

const cadenceLabels: Record<ScheduleCadence, string> = {
  daily_morning: 'Every day · 08:00',
  daily_evening: 'Every day · 18:00',
  weekly_monday: 'Every Monday · 09:00',
  every_six_hours: 'Every 6 hours',
}

const cadenceCron: Record<ScheduleCadence, string> = {
  daily_morning: '0 8 * * *',
  daily_evening: '0 18 * * *',
  weekly_monday: '0 9 * * 1',
  every_six_hours: '0 */6 * * *',
}

const workspaceFilePermissionCeiling: SchedulePermissionRule[] = [
  { tool_name: 'Write', scope: 'path:.' },
  { tool_name: 'Edit', scope: 'path:.' },
]

function isWorkspaceFileCeiling(rules: SchedulePermissionRule[]): boolean {
  return rules.length === workspaceFilePermissionCeiling.length
    && workspaceFilePermissionCeiling.every((expected) => rules.some(
      (rule) => rule.tool_name === expected.tool_name && rule.scope === expected.scope,
    ))
}

function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

function fireTime(value: string | null, language: 'en' | 'zh-CN'): string {
  if (!value) return '—'
  return new Date(value).toLocaleString(language === 'zh-CN' ? 'zh-CN' : 'en', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export default function ScheduledTasksPage() {
  const t = useT()
  const language = useLanguage()
  const [view, setView] = useState<'templates' | 'tasks'>('templates')
  const [schedules, setSchedules] = useState<Schedule[]>([])
  const [projects, setProjects] = useState<OpportunityProject[]>([])
  const [lastRuns, setLastRuns] = useState<Record<string, RunRecord>>({})
  const [backendState, setBackendState] = useState<'loading' | 'online' | 'offline'>('loading')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState<Set<string>>(() => new Set())
  const busyRef = useRef(new Set<string>())
  const feedbackRevision = useRef(0)

  const [creatorOpen, setCreatorOpen] = useState(false)
  const [prompt, setPrompt] = useState('')
  const [cadence, setCadence] = useState<ScheduleCadence>('daily_morning')
  const [scheduleKind, setScheduleKind] = useState<ScheduleKind>('task')
  const [projectId, setProjectId] = useState('')
  const [permissionPreset, setPermissionPreset] = useState<SchedulePermissionPreset>('read_only')

  const beginAction = (key: string): number | null => {
    if (busyRef.current.has(key)) return null
    busyRef.current.add(key)
    setBusy(new Set(busyRef.current))
    setError('')
    setNotice('')
    return ++feedbackRevision.current
  }

  const finishAction = (key: string) => {
    busyRef.current.delete(key)
    setBusy(new Set(busyRef.current))
  }

  useEffect(() => {
    let active = true
    void Promise.all([listSchedules(), listProjects()])
      .then(async ([loadedSchedules, loadedProjects]) => {
        if (!active) return
        setSchedules(loadedSchedules)
        setProjects(loadedProjects)
        setBackendState('online')
        const withRuns = loadedSchedules.filter((item) => item.last_run_id)
        const runs = await Promise.all(
          withRuns.map((item) => getRun(item.last_run_id!).catch(() => null)),
        )
        if (!active) return
        const map: Record<string, RunRecord> = {}
        withRuns.forEach((item, index) => {
          const run = runs[index]
          if (run) map[item.id] = run
        })
        setLastRuns(map)
      })
      .catch((loadError) => {
        if (!active) return
        setBackendState('offline')
        setError(errorMessage(loadError))
      })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (!creatorOpen) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setCreatorOpen(false)
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [creatorOpen])

  const openCreator = (template?: ScheduleTemplate) => {
    setPrompt(template ? t(template.prompt) : '')
    setCadence(template?.cadence ?? 'daily_morning')
    setScheduleKind(template?.kind ?? 'task')
    setProjectId(projects[0]?.id ?? '')
    setPermissionPreset('read_only')
    setError('')
    setCreatorOpen(true)
  }

  const submitSchedule = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!prompt.trim() || !projectId) return
    const revision = beginAction('create')
    if (revision === null) return
    try {
      const created = await createSchedule({
        project_id: projectId,
        prompt: prompt.trim(),
        cron: cadenceCron[cadence],
        timezone: browserTimezone(),
        mode: 'standard',
        kind: scheduleKind,
        permission_ceiling: permissionPreset === 'workspace_files'
          ? workspaceFilePermissionCeiling.map((rule) => ({ ...rule }))
          : [],
      })
      setSchedules((current) => [created, ...current])
      setCreatorOpen(false)
      setView('tasks')
      if (revision === feedbackRevision.current) {
        setNotice(t('Schedule created. It runs on the backend scheduler — this device can go offline.'))
      }
    } catch (createError) {
      if (revision === feedbackRevision.current) setError(t(errorMessage(createError)))
    } finally {
      finishAction('create')
    }
  }

  const toggleEnabled = async (schedule: Schedule) => {
    const revision = beginAction(schedule.id)
    if (revision === null) return
    try {
      const updated = schedule.enabled
        ? await pauseSchedule(schedule.id)
        : await resumeSchedule(schedule.id)
      setSchedules((current) => current.map((item) => (item.id === updated.id ? updated : item)))
    } catch (toggleError) {
      if (revision === feedbackRevision.current) setError(t(errorMessage(toggleError)))
    } finally {
      finishAction(schedule.id)
    }
  }

  const fireNow = async (schedule: Schedule) => {
    const revision = beginAction(schedule.id)
    if (revision === null) return
    try {
      const run = await runSchedule(schedule.id)
      if (revision === feedbackRevision.current) {
        setNotice(t('Manual run {id} started ({status}). Watch it in the workspace.', {
          id: run.id,
          status: t(run.status),
        }))
      }
    } catch (runError) {
      if (revision === feedbackRevision.current) setError(t(errorMessage(runError)))
    } finally {
      finishAction(schedule.id)
    }
  }

  return (
    <div className="schedules-shell">
      <aside className="schedules-sidebar">
        <a href="/" aria-label={t('TradeMind website')}><Brand /></a>
        <a className="schedules-back" href="/app"><span aria-hidden="true">←</span> {t('Back to workspace')}</a>
        <nav aria-label={t('Scheduled task categories')}>
          <p>{t('Task library')}</p>
          {(Object.keys(categoryLabels) as ScheduleCategory[]).map((category, index) => (
            <a key={category} href={`#${category}`}><span>{String(index + 1).padStart(2, '0')}</span>{t(categoryLabels[category])}</a>
          ))}
        </nav>
        <div className="schedules-sidebar-footer">
          <a href="/app/settings">{t('Settings')} <span aria-hidden="true">→</span></a>
          <p>{t('Schedules run on the TradeMind backend — they do not depend on this device staying awake.')}</p>
        </div>
      </aside>

      <main className="schedules-main">
        <header className="schedules-header">
          <div><p>{t('TradeMind automation')}</p><h1>{t('Scheduled tasks')}</h1><span>{t('Recurring work executed by the backend scheduler with the same project ledger and permissions.')}</span></div>
          <div className="schedules-actions">
            <div role="group" aria-label={t('Scheduled task view')}>
              <button type="button" data-active={view === 'templates'} onClick={() => setView('templates')}>{t('Templates')}</button>
              <button type="button" data-active={view === 'tasks'} onClick={() => setView('tasks')}>{t('My tasks')} <span>{schedules.length}</span></button>
            </div>
            <button type="button" onClick={() => openCreator()}><span aria-hidden="true">＋</span> {t('New task')}</button>
          </div>
        </header>

        <section className="schedules-runtime-note" aria-label={t('Scheduler connection status')}>
          <div>
            <span>{t('Scheduler status')}</span>
            <strong>
              {backendState === 'online' ? t('Connected · server-side execution') : backendState === 'loading' ? t('Connecting…') : t('Backend unavailable')}
            </strong>
          </div>
          <p>
            {backendState === 'online'
              ? t('Tasks fire on the backend even when this browser is closed. Failures and expired credentials are shown on each task.')
              : backendState === 'loading'
                ? t('Loading schedules from the backend…')
                : `${t('The scheduler API could not be reached.')} ${error}`}
          </p>
        </section>

        {notice && <p className="schedules-notice" role="status">{notice}</p>}
        {error && backendState === 'online' && <p className="schedules-error" role="alert">{error}</p>}

        {view === 'tasks' ? (
          <section className="schedules-task-list" aria-labelledby="saved-tasks-heading">
            <div><h2 id="saved-tasks-heading">{t('My scheduled tasks')}</h2><span>{t('{n} on the backend', { n: schedules.length })}</span></div>
            {schedules.length ? schedules.map((schedule) => {
              const lastRun = lastRuns[schedule.id]
              const projectName = projects.find((item) => item.id === schedule.project_id)?.name ?? schedule.project_id
              return (
                <article key={schedule.id} data-enabled={schedule.enabled}>
                  <span>{schedule.enabled ? 'ON' : '⏸'}</span>
                  <div>
                    <strong>{schedule.prompt}</strong>
                    <p>
                      <code>{schedule.cron ?? schedule.run_once_at ?? ''}</code> · {schedule.timezone} · {projectName} ·{' '}
                      <b className="schedule-kind" data-kind={schedule.kind}>{t(schedule.kind === 'heartbeat' ? 'Heartbeat' : 'Task')}</b>
                    </p>
                    <p>
                      {t('Next')} {fireTime(schedule.next_fire_at, language)} · {t('Last')} {fireTime(schedule.last_fire_at, language)}
                      {!schedule.enabled && <em> · {t('Paused')}</em>}
                    </p>
                    <section
                      className="schedule-permission-ceiling"
                      aria-label={t('Permission ceiling')}
                    >
                      <span>{t('Permission ceiling')}</span>
                      {schedule.permission_ceiling.length === 0 ? (
                        <div>
                          <strong>{t('Read-only analysis · no added action grants')}</strong>
                          <small>{t('No workspace writes or new external actions. Delivery already authorized by this schedule or channel policy may still occur.')}</small>
                        </div>
                      ) : (
                        <div>
                          <strong>{isWorkspaceFileCeiling(schedule.permission_ceiling)
                            ? t('Workspace file changes only')
                            : t('Bounded unattended permissions')}</strong>
                          <ul>
                            {schedule.permission_ceiling.map((rule, index) => (
                              <li key={`${rule.tool_name}:${rule.scope}:${index}`}>
                                <code>{rule.tool_name}</code>
                                <small>{rule.scope === 'path:.'
                                  ? t('Project workspace (path:.)')
                                  : rule.scope.startsWith('url:https://')
                                    ? `${t('Exact HTTPS origin')} · ${rule.scope.slice(4)}`
                                    : rule.scope}</small>
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}
                    </section>
                    {lastRun && lastRun.status === 'failed' && (
                      <p className="schedules-run-error" role="alert">
                        {t('Last run failed')}{lastRun.error_code ? <>: <code>{lastRun.error_code}</code></> : ''}
                        {lastRun.error ? <> — {lastRun.error}</> : ''}
                      </p>
                    )}
                    {lastRun && lastRun.status !== 'failed' && (
                      <p className="schedules-run-ok">{t('Last run')} {t(lastRun.status)}</p>
                    )}
                    <TaskExperiencePanel scheduleId={schedule.id} />
                  </div>
                  <div className="schedules-row-actions">
                    <button type="button" disabled={busy.has(schedule.id)} onClick={() => void toggleEnabled(schedule)}>
                      {schedule.enabled ? t('Pause') : t('Resume')}
                    </button>
                    <button type="button" disabled={busy.has(schedule.id)} onClick={() => void fireNow(schedule)}>
                      {t('Run now')}
                    </button>
                  </div>
                </article>
              )
            }) : (
              <div className="schedules-empty"><strong>{t('No scheduled tasks yet.')}</strong><p>{t('Choose a template or create a task from your own recurring brief.')}</p><button type="button" onClick={() => setView('templates')}>{t('Browse templates')}</button></div>
            )}
          </section>
        ) : (
          <div className="schedules-library">
            {(Object.keys(categoryLabels) as ScheduleCategory[]).map((category) => (
              <section key={category} id={category} aria-labelledby={`${category}-heading`}>
                <div><h2 id={`${category}-heading`}>{t(categoryLabels[category])}</h2><span>{t('{n} templates', { n: templates.filter((template) => template.category === category).length })}</span></div>
                <div>
                  {templates.filter((template) => template.category === category).map((template) => (
                    <button key={template.id} type="button" onClick={() => openCreator(template)}>
                      <span>{template.code}</span>
                      <div><strong>{t(template.name)}</strong><p>{t(template.description)}</p></div>
                      <small>{t(cadenceLabels[template.cadence])} · {t(template.kind === 'heartbeat' ? 'Heartbeat' : 'Task')}</small>
                    </button>
                  ))}
                </div>
              </section>
            ))}
          </div>
        )}
      </main>

      {creatorOpen && (
        <div className="schedule-creator-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setCreatorOpen(false)
        }}>
          <section className="schedule-creator" role="dialog" aria-modal="true" aria-labelledby="schedule-creator-heading">
            <header><div><span>{t('Backend schedule')}</span><h2 id="schedule-creator-heading">{t('Create scheduled task')}</h2></div><button type="button" aria-label={t('Close task creator')} onClick={() => setCreatorOpen(false)}>×</button></header>
            <form onSubmit={(event) => void submitSchedule(event)}>
              <label>{t('Recurring brief')}<textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={5} placeholder={t('Describe what the Agent should prepare...')} required /></label>
              <div>
                <label>{t('Cadence')}<select value={cadence} onChange={(event) => setCadence(event.target.value as ScheduleCadence)}>{Object.entries(cadenceLabels).map(([value, text]) => <option key={value} value={value}>{t(text)}</option>)}</select></label>
                <label>{t('Project')}<select value={projectId} onChange={(event) => setProjectId(event.target.value)} required>
                  <option value="" disabled>{t('Select a project')}</option>
                  {projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                </select></label>
              </div>
              {!projects.length && <p>{t('No projects yet — start a conversation in the workspace to create one, then come back to schedule it.')}</p>}
              <p className="schedule-kind-note">
                <strong>{t(scheduleKind === 'heartbeat' ? 'Heartbeat monitor' : 'Scheduled task')}</strong>{' · '}
                {scheduleKind === 'heartbeat'
                  ? t('Unchanged checks stay silent; changes and failures go to Inbox.')
                  : t('Each scheduled run is recorded in this project.')}
              </p>
              <fieldset className="schedule-permission-picker">
                <legend>{t('Permission ceiling')}</legend>
                <p>{t('Choose the maximum access this task may use without someone present.')}</p>
                <label data-selected={permissionPreset === 'read_only' || undefined}>
                  <input
                    type="radio"
                    name="schedule-permission-preset"
                    value="read_only"
                    checked={permissionPreset === 'read_only'}
                    onChange={() => setPermissionPreset('read_only')}
                  />
                  <span>
                    <strong>{t('Read-only analysis · no added action grants')}</strong>
                    <small>{t('Default. No workspace writes or new external actions are granted. Delivery already authorized by this schedule or channel policy may still occur.')}</small>
                  </span>
                </label>
                <label data-selected={permissionPreset === 'workspace_files' || undefined}>
                  <input
                    type="radio"
                    name="schedule-permission-preset"
                    value="workspace_files"
                    checked={permissionPreset === 'workspace_files'}
                    onChange={() => setPermissionPreset('workspace_files')}
                  />
                  <span>
                    <strong>{t('Workspace files · Write and Edit only')}</strong>
                    <small>{t("Explicit opt-in. Allows only Write and Edit inside this project's workspace (path:.).")}</small>
                  </span>
                </label>
                <p>{t('Shell commands, deletion, browser actions, and new external actions remain blocked. Previously authorized schedule or channel delivery may still occur.')}</p>
              </fieldset>
              <p>{t('Runs on the backend scheduler in timezone {tz} ({cron}). The Agent keeps the same ledger, permissions, and provider profile rules.', { tz: browserTimezone(), cron: cadenceCron[cadence] })}</p>
              {error && <p className="schedules-error" role="alert">{error}</p>}
              <footer><button type="button" onClick={() => setCreatorOpen(false)}>{t('Cancel')}</button><button type="submit" disabled={busy.has('create') || !projects.length}>{busy.has('create') ? t('Creating…') : t('Create schedule')} <span aria-hidden="true">→</span></button></footer>
            </form>
          </section>
        </div>
      )}
    </div>
  )
}
