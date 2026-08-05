import { useEffect, useState, type FormEvent } from 'react'
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

type ScheduledTaskDraft = {
  id: string
  name: string
  prompt: string
  cadence: ScheduleCadence
  agent: 'opportunity' | 'demand' | 'supply' | 'capital' | 'sales'
  createdAt: string
}

const storageKey = 'ophira-scheduled-task-drafts'

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

function loadTaskDrafts(): ScheduledTaskDraft[] {
  try {
    return JSON.parse(window.localStorage.getItem(storageKey) ?? '[]') as ScheduledTaskDraft[]
  } catch {
    return []
  }
}

export default function ScheduledTasksPage() {
  const [view, setView] = useState<'templates' | 'tasks'>('templates')
  const [tasks, setTasks] = useState<ScheduledTaskDraft[]>(loadTaskDrafts)
  const [creatorOpen, setCreatorOpen] = useState(false)
  const [name, setName] = useState('')
  const [prompt, setPrompt] = useState('')
  const [cadence, setCadence] = useState<ScheduleCadence>('daily_morning')
  const [agent, setAgent] = useState<ScheduledTaskDraft['agent']>('opportunity')

  useEffect(() => {
    window.localStorage.setItem(storageKey, JSON.stringify(tasks))
  }, [tasks])

  useEffect(() => {
    if (!creatorOpen) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setCreatorOpen(false)
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [creatorOpen])

  const openCreator = (template?: ScheduleTemplate) => {
    setName(template?.name ?? '')
    setPrompt(template?.prompt ?? '')
    setCadence(template?.cadence ?? 'daily_morning')
    setAgent('opportunity')
    setCreatorOpen(true)
  }

  const createDraft = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!name.trim() || !prompt.trim()) return
    setTasks((current) => [{
      id: `schedule_${Date.now()}`,
      name: name.trim(),
      prompt: prompt.trim(),
      cadence,
      agent,
      createdAt: new Date().toISOString(),
    }, ...current])
    setCreatorOpen(false)
    setView('tasks')
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
          <p>Drafts stay on this device until the backend scheduler is connected.</p>
        </div>
      </aside>

      <main className="schedules-main">
        <header className="schedules-header">
          <div><p>OPHIRA automation</p><h1>Scheduled tasks</h1><span>Prepare recurring work for Opportunity Lead and the specialist Agent team.</span></div>
          <div className="schedules-actions">
            <div role="group" aria-label="Scheduled task view">
              <button type="button" data-active={view === 'templates'} onClick={() => setView('templates')}>Templates</button>
              <button type="button" data-active={view === 'tasks'} onClick={() => setView('tasks')}>My tasks <span>{tasks.length}</span></button>
            </div>
            <button type="button" onClick={() => openCreator()}><span aria-hidden="true">＋</span> New task</button>
          </div>
        </header>

        <section className="schedules-runtime-note" aria-label="Scheduler connection status">
          <div><span>Scheduler status</span><strong>Draft mode</strong></div>
          <p>Tasks can be prepared and saved locally. Automatic execution will remain off until a scheduler API, account identity, and delivery channel are connected.</p>
        </section>

        {view === 'tasks' ? (
          <section className="schedules-task-list" aria-labelledby="saved-tasks-heading">
            <div><h2 id="saved-tasks-heading">My task drafts</h2><span>{tasks.length} saved locally</span></div>
            {tasks.length ? tasks.map((task) => (
              <article key={task.id}>
                <span>{task.agent.slice(0, 2).toUpperCase()}</span>
                <div><strong>{task.name}</strong><p>{task.prompt}</p></div>
                <small>{cadenceLabels[task.cadence]} · Draft</small>
              </article>
            )) : (
              <div className="schedules-empty"><strong>No task drafts yet.</strong><p>Choose a template or create a task from your own recurring brief.</p><button type="button" onClick={() => setView('templates')}>Browse templates</button></div>
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
            <header><div><span>Local task draft</span><h2 id="schedule-creator-heading">Create scheduled task</h2></div><button type="button" aria-label="Close task creator" onClick={() => setCreatorOpen(false)}>×</button></header>
            <form onSubmit={createDraft}>
              <label>Task name<input value={name} onChange={(event) => setName(event.target.value)} placeholder="Weekly opportunity review" required /></label>
              <label>Recurring brief<textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={5} placeholder="Describe what the Agent should prepare..." required /></label>
              <div>
                <label>Cadence<select value={cadence} onChange={(event) => setCadence(event.target.value as ScheduleCadence)}>{Object.entries(cadenceLabels).map(([value, text]) => <option key={value} value={value}>{text}</option>)}</select></label>
                <label>Agent<select value={agent} onChange={(event) => setAgent(event.target.value as ScheduledTaskDraft['agent'])}><option value="opportunity">Opportunity Lead</option><option value="demand">Demand</option><option value="supply">Supply</option><option value="capital">Capital</option><option value="sales">Sales</option></select></label>
              </div>
              <p>This creates a local draft only. No task will run or contact an external service.</p>
              <footer><button type="button" onClick={() => setCreatorOpen(false)}>Cancel</button><button type="submit">Save draft <span aria-hidden="true">→</span></button></footer>
            </form>
          </section>
        </div>
      )}
    </div>
  )
}
