import { useEffect, useState, type FormEvent } from 'react'
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
import type { OpportunityProject, RunRecord, Schedule } from '../api/types'
import Brand from './Brand'
import './scheduled-tasks-page.css'

type ScheduleCategory = 'daily' | 'monitoring' | 'reports' | 'automation'
type ScheduleCadence = 'daily_morning' | 'daily_evening' | 'weekly_monday' | 'every_six_hours'

type ScheduleTemplate = {
  id: string
  code: string
  name: string
  description: string
  category: ScheduleCategory
  cadence: ScheduleCadence
  prompt: string
}

const templates: ScheduleTemplate[] = [
  { id: 'morning-brief', code: 'AM', name: 'Morning opportunity brief', description: 'Summarize new market signals, priority evidence, and today’s next actions.', category: 'daily', cadence: 'daily_morning', prompt: 'Prepare a morning opportunity brief with new signals, evidence gaps, and the three highest-priority actions.' },
  { id: 'end-of-day', code: 'PM', name: 'End-of-day synthesis', description: 'Summarize progress, unresolved questions, and tomorrow’s validation priorities.', category: 'daily', cadence: 'daily_evening', prompt: 'Summarize today’s opportunity work, unresolved questions, and recommended priorities for tomorrow.' },
  { id: 'competitor-watch', code: 'CW', name: 'Competitor watch', description: 'Review material product, pricing, positioning, and channel changes.', category: 'monitoring', cadence: 'every_six_hours', prompt: 'Monitor the defined competitors for material product, pricing, positioning, and channel changes.' },
  { id: 'market-radar', code: 'MR', name: 'Market signal radar', description: 'Track high-signal changes in buyer needs, policy, supply, and category behavior.', category: 'monitoring', cadence: 'daily_morning', prompt: 'Track high-signal changes in buyer needs, policy, supply conditions, and category behavior.' },
  { id: 'weekly-review', code: 'WR', name: 'Weekly opportunity review', description: 'Review validated evidence, weakest links, and next-stage readiness.', category: 'reports', cadence: 'weekly_monday', prompt: 'Create a weekly opportunity review covering evidence, weakest links, and stage-gate readiness.' },
  { id: 'quality-review', code: 'QA', name: 'Project quality review', description: 'Check unsupported claims, stale evidence, open permissions, and missing stop conditions.', category: 'reports', cadence: 'weekly_monday', prompt: 'Audit active opportunity projects for unsupported claims, stale evidence, open permissions, and missing stop conditions.' },
  { id: 'supplier-follow-up', code: 'SF', name: 'Supplier follow-up queue', description: 'Prepare the next approved supplier questions from unresolved project gaps.', category: 'automation', cadence: 'daily_morning', prompt: 'Prepare a supplier follow-up queue from unresolved supply gaps. Do not contact anyone without approval.' },
  { id: 'buyer-test-prep', code: 'BT', name: 'Buyer test preparation', description: 'Turn current sales assumptions into the smallest falsifiable buyer test.', category: 'automation', cadence: 'weekly_monday', prompt: 'Prepare the smallest falsifiable buyer test from current sales assumptions. Do not send outreach without approval.' },
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

function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

function fireTime(value: string | null): string {
  if (!value) return '—'
  return new Date(value).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
}

export default function ScheduledTasksPage() {
  const [view, setView] = useState<'templates' | 'tasks'>('templates')
  const [schedules, setSchedules] = useState<Schedule[]>([])
  const [projects, setProjects] = useState<OpportunityProject[]>([])
  const [lastRuns, setLastRuns] = useState<Record<string, RunRecord>>({})
  const [backendState, setBackendState] = useState<'loading' | 'online' | 'offline'>('loading')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState('')

  const [creatorOpen, setCreatorOpen] = useState(false)
  const [prompt, setPrompt] = useState('')
  const [cadence, setCadence] = useState<ScheduleCadence>('daily_morning')
  const [projectId, setProjectId] = useState('')

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
    setPrompt(template?.prompt ?? '')
    setCadence(template?.cadence ?? 'daily_morning')
    setProjectId(projects[0]?.id ?? '')
    setError('')
    setCreatorOpen(true)
  }

  const submitSchedule = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!prompt.trim() || !projectId) return
    setBusy('create')
    setError('')
    try {
      const created = await createSchedule({
        project_id: projectId,
        prompt: prompt.trim(),
        cron: cadenceCron[cadence],
        timezone: browserTimezone(),
        mode: 'standard',
      })
      setSchedules((current) => [created, ...current])
      setCreatorOpen(false)
      setView('tasks')
      setNotice('Schedule created. It runs on the backend scheduler — this device can go offline.')
    } catch (createError) {
      setError(errorMessage(createError))
    } finally {
      setBusy('')
    }
  }

  const toggleEnabled = async (schedule: Schedule) => {
    setBusy(schedule.id)
    setError('')
    try {
      const updated = schedule.enabled
        ? await pauseSchedule(schedule.id)
        : await resumeSchedule(schedule.id)
      setSchedules((current) => current.map((item) => (item.id === updated.id ? updated : item)))
    } catch (toggleError) {
      setError(errorMessage(toggleError))
    } finally {
      setBusy('')
    }
  }

  const fireNow = async (schedule: Schedule) => {
    setBusy(`run:${schedule.id}`)
    setError('')
    try {
      const run = await runSchedule(schedule.id)
      setNotice(`Manual run ${run.id} started (${run.status}). Watch it in the workspace.`)
    } catch (runError) {
      setError(errorMessage(runError))
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="schedules-shell">
      <aside className="schedules-sidebar">
        <a href="/" aria-label="OPHIRA website"><Brand /></a>
        <a className="schedules-back" href="/app"><span aria-hidden="true">←</span> Back to workspace</a>
        <nav aria-label="Scheduled task categories">
          <p>Task library</p>
          {(Object.keys(categoryLabels) as ScheduleCategory[]).map((category, index) => (
            <a key={category} href={`#${category}`}><span>{String(index + 1).padStart(2, '0')}</span>{categoryLabels[category]}</a>
          ))}
        </nav>
        <div className="schedules-sidebar-footer">
          <a href="/app/settings">Settings <span aria-hidden="true">→</span></a>
          <p>Schedules run on the OPHIRA backend — they do not depend on this device staying awake.</p>
        </div>
      </aside>

      <main className="schedules-main">
        <header className="schedules-header">
          <div><p>OPHIRA automation</p><h1>Scheduled tasks</h1><span>Recurring work executed by the backend scheduler with the same project ledger and permissions.</span></div>
          <div className="schedules-actions">
            <div role="group" aria-label="Scheduled task view">
              <button type="button" data-active={view === 'templates'} onClick={() => setView('templates')}>Templates</button>
              <button type="button" data-active={view === 'tasks'} onClick={() => setView('tasks')}>My tasks <span>{schedules.length}</span></button>
            </div>
            <button type="button" onClick={() => openCreator()}><span aria-hidden="true">＋</span> New task</button>
          </div>
        </header>

        <section className="schedules-runtime-note" aria-label="Scheduler connection status">
          <div>
            <span>Scheduler status</span>
            <strong>
              {backendState === 'online' ? 'Connected · server-side execution' : backendState === 'loading' ? 'Connecting…' : 'Backend unavailable'}
            </strong>
          </div>
          <p>
            {backendState === 'online'
              ? 'Tasks fire on the backend even when this browser is closed. Failures and expired credentials are shown on each task.'
              : backendState === 'loading'
                ? 'Loading schedules from the backend…'
                : `The scheduler API could not be reached. ${error}`}
          </p>
        </section>

        {notice && <p className="schedules-notice" role="status">{notice}</p>}
        {error && backendState === 'online' && <p className="schedules-error" role="alert">{error}</p>}

        {view === 'tasks' ? (
          <section className="schedules-task-list" aria-labelledby="saved-tasks-heading">
            <div><h2 id="saved-tasks-heading">My scheduled tasks</h2><span>{schedules.length} on the backend</span></div>
            {schedules.length ? schedules.map((schedule) => {
              const lastRun = lastRuns[schedule.id]
              const projectName = projects.find((item) => item.id === schedule.project_id)?.name ?? schedule.project_id
              return (
                <article key={schedule.id} data-enabled={schedule.enabled}>
                  <span>{schedule.enabled ? 'ON' : '⏸'}</span>
                  <div>
                    <strong>{schedule.prompt}</strong>
                    <p>
                      <code>{schedule.cron ?? schedule.run_once_at ?? ''}</code> · {schedule.timezone} · {projectName}
                    </p>
                    <p>
                      Next {fireTime(schedule.next_fire_at)} · Last {fireTime(schedule.last_fire_at)}
                      {!schedule.enabled && <em> · Paused</em>}
                    </p>
                    {lastRun && lastRun.status === 'failed' && (
                      <p className="schedules-run-error" role="alert">
                        Last run failed{lastRun.error_code ? <>: <code>{lastRun.error_code}</code></> : ''}
                        {lastRun.error ? <> — {lastRun.error}</> : ''}
                      </p>
                    )}
                    {lastRun && lastRun.status !== 'failed' && (
                      <p className="schedules-run-ok">Last run {lastRun.status}</p>
                    )}
                  </div>
                  <div className="schedules-row-actions">
                    <button type="button" disabled={busy === schedule.id} onClick={() => void toggleEnabled(schedule)}>
                      {schedule.enabled ? 'Pause' : 'Resume'}
                    </button>
                    <button type="button" disabled={busy === `run:${schedule.id}`} onClick={() => void fireNow(schedule)}>
                      Run now
                    </button>
                  </div>
                </article>
              )
            }) : (
              <div className="schedules-empty"><strong>No scheduled tasks yet.</strong><p>Choose a template or create a task from your own recurring brief.</p><button type="button" onClick={() => setView('templates')}>Browse templates</button></div>
            )}
          </section>
        ) : (
          <div className="schedules-library">
            {(Object.keys(categoryLabels) as ScheduleCategory[]).map((category) => (
              <section key={category} id={category} aria-labelledby={`${category}-heading`}>
                <div><h2 id={`${category}-heading`}>{categoryLabels[category]}</h2><span>{templates.filter((template) => template.category === category).length} templates</span></div>
                <div>
                  {templates.filter((template) => template.category === category).map((template) => (
                    <button key={template.id} type="button" onClick={() => openCreator(template)}>
                      <span>{template.code}</span>
                      <div><strong>{template.name}</strong><p>{template.description}</p></div>
                      <small>{cadenceLabels[template.cadence]}</small>
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
            <header><div><span>Backend schedule</span><h2 id="schedule-creator-heading">Create scheduled task</h2></div><button type="button" aria-label="Close task creator" onClick={() => setCreatorOpen(false)}>×</button></header>
            <form onSubmit={(event) => void submitSchedule(event)}>
              <label>Recurring brief<textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={5} placeholder="Describe what the Agent should prepare..." required /></label>
              <div>
                <label>Cadence<select value={cadence} onChange={(event) => setCadence(event.target.value as ScheduleCadence)}>{Object.entries(cadenceLabels).map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></label>
                <label>Project<select value={projectId} onChange={(event) => setProjectId(event.target.value)} required>
                  <option value="" disabled>Select a project</option>
                  {projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
                </select></label>
              </div>
              {!projects.length && <p>还没有项目——先在 workspace 发起一次对话创建项目，再回来挂定时任务。</p>}
              <p>Runs on the backend scheduler in timezone {browserTimezone()} ({cadenceCron[cadence]}). The Agent keeps the same ledger, permissions, and provider profile rules.</p>
              {error && <p className="schedules-error" role="alert">{error}</p>}
              <footer><button type="button" onClick={() => setCreatorOpen(false)}>Cancel</button><button type="submit" disabled={busy === 'create' || !projects.length}>{busy === 'create' ? 'Creating…' : 'Create schedule'} <span aria-hidden="true">→</span></button></footer>
            </form>
          </section>
        </div>
      )}
    </div>
  )
}
