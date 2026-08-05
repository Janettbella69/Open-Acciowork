import { useEffect, useRef, useState, type FormEvent } from 'react'
import {
  dimensions,
  type OpportunityProject,
  type OphiraEvent,
  type PendingPermission,
  type RunRecord,
} from '../api/types'
import Brand from './Brand'
import './workspace-composer.css'

type WorkspaceComposerProps = {
  value: string
  busy: boolean
  notice: string
  error: string
  projects: OpportunityProject[]
  project: OpportunityProject | null
  run: RunRecord | null
  events: OphiraEvent[]
  assistantText: string
  reasoningSummary: string
  permissions: PendingPermission[]
  connection: string
  isActive: boolean
  onChange: (value: string) => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
  onNewResearch: () => void
  onSelectProject: (projectId: string) => void
  onCancel: () => void
  onDecision: (requestId: string, decision: 'allow_once' | 'allow_run' | 'deny') => void
}

const quickStarts = [
  {
    dimension: 'Demand',
    title: 'Find a real unmet need',
    prompt: 'Find a specific unmet customer need in a market where a small cross-border team can compete.',
  },
  {
    dimension: 'Supply',
    title: 'Test whether our capability can travel',
    prompt: 'Assess which global market best fits our current product and supply capability.',
  },
  {
    dimension: 'Capital',
    title: 'Bound the downside before we move',
    prompt: 'Map the first-order cash exposure and the assumptions we must validate before entering a new market.',
  },
  {
    dimension: 'Sales',
    title: 'Design the smallest buyer test',
    prompt: 'Design a small, falsifiable sales test for a new cross-border product opportunity.',
  },
]

function label(value: string): string {
  return value.replace(/[._]/g, ' ').replace(/\b\w/g, (character) => character.toUpperCase())
}

function activityLabel(event: OphiraEvent): string {
  const toolName = typeof event.data.tool_name === 'string' ? event.data.tool_name : ''
  if (event.type.startsWith('tool.') && toolName) return `${label(event.type)} · ${toolName}`
  return label(event.type)
}

function WorkspaceAccountMenu({ backendState }: { backendState: string }) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return

    const closeOnOutsidePress = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }

    document.addEventListener('pointerdown', closeOnOutsidePress)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePress)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [open])

  return (
    <div className="conversation-account" ref={rootRef}>
      {open && (
        <section className="conversation-account-menu" role="dialog" aria-label="Workspace account menu">
          <header>
            <span>OP</span>
            <div><strong>Ophira workspace</strong><small>Local development workspace</small></div>
          </header>

          <div className="conversation-account-plan">
            <div><strong>Developer preview</strong><span>Local</span></div>
            <p><span>Runtime</span><strong>{backendState}</strong></p>
            <small>Usage follows the AI provider configured on this backend.</small>
          </div>

          <nav aria-label="Account destinations">
            <a href="/app/settings"><span><strong>Settings</strong><small>Permissions, runtime, and language</small></span><i aria-hidden="true">→</i></a>
            <a href="/"><span><strong>Homepage</strong><small>Return to the OPHIRA website</small></span><i aria-hidden="true">↗</i></a>
            <a href="https://github.com/Janettbella69/Ophira-ai" target="_blank" rel="noreferrer"><span><strong>GitHub</strong><small>Open the source repository</small></span><i aria-hidden="true">↗</i></a>
          </nav>

          <footer>Account authentication and billing are not enabled in this local build.</footer>
        </section>
      )}

      <button
        className="conversation-sidebar-profile"
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label="Open account menu for Ophira workspace"
        onClick={() => setOpen((current) => !current)}
      >
        <span>OP</span>
        <div><strong>Ophira workspace</strong><small>{backendState}</small></div>
        <i aria-hidden="true">⌃</i>
      </button>
    </div>
  )
}

export default function WorkspaceComposer({
  value,
  busy,
  notice,
  error,
  projects,
  project,
  run,
  events,
  assistantText,
  reasoningSummary,
  permissions,
  connection,
  isActive,
  onChange,
  onSubmit,
  onNewResearch,
  onSelectProject,
  onCancel,
  onDecision,
}: WorkspaceComposerProps) {
  const sessionOpen = Boolean(project || run)
  const recentActivity = events.filter((event) => !event.type.endsWith('.delta')).slice(-6).reverse()
  const latestActor = recentActivity[0]?.actor.name.toLowerCase() ?? ''
  const composer = (
    <div className="conversation-composer-dock" data-inline={!sessionOpen}>
      <div className="conversation-context-bar">
        <span>{project?.name ?? 'New opportunity'}</span>
        <small>{run ? `${label(run.status)} · ${connection}` : 'One shared project truth'}</small>
      </div>
      <form className="conversation-composer" onSubmit={onSubmit}>
        <label htmlFor="workspace-conversation-brief">Ask Ophira</label>
        <textarea
          id="workspace-conversation-brief"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="Describe a product, market, capability, or customer..."
          rows={3}
          disabled={busy}
          autoFocus={!sessionOpen}
        />
        <div className="conversation-composer-footer">
          <span><b>Opportunity Lead</b> coordinates Demand · Supply · Capital · Sales</span>
          <button type="submit" disabled={busy || !value.trim()}>
            {busy ? 'Researching…' : sessionOpen ? 'Start new research' : 'Start research'} <span aria-hidden="true">→</span>
          </button>
        </div>
      </form>
      {notice && <p className="conversation-notice" role="status">{notice}</p>}
      {error && <p className="conversation-error" role="alert">{error}</p>}
    </div>
  )

  return (
    <div className="conversation-entry">
      <aside className="conversation-sidebar">
        <a className="conversation-sidebar-brand" href="/" aria-label="Back to OPHIRA website"><Brand /></a>

        <button className="conversation-new-research" type="button" onClick={onNewResearch}>
          <span aria-hidden="true">＋</span> New research
        </button>

        <nav className="conversation-sidebar-nav" aria-label="Workspace navigation">
          <p>Workspace</p>
          <a href="/app" aria-current="page"><span>01</span>Opportunity studio</a>
          <a href="/app/schedules"><span>02</span>Scheduled tasks</a>
          <a href="/app/skills"><span>03</span>Skill Studio</a>
        </nav>

        <section className="conversation-sidebar-projects" aria-labelledby="sidebar-projects-title">
          <div><h2 id="sidebar-projects-title">Recent projects</h2><span>{projects.length}</span></div>
          {projects.length > 0 ? projects.slice(0, 6).map((savedProject) => (
            <button
              key={savedProject.id}
              type="button"
              aria-pressed={project?.id === savedProject.id}
              onClick={() => onSelectProject(savedProject.id)}
            >
              <span>{savedProject.name}</span>
              <small>{savedProject.objective}</small>
            </button>
          )) : <p>Your saved opportunity studies will appear here.</p>}
        </section>

        <section className="conversation-sidebar-agents" aria-labelledby="sidebar-agents-title">
          <h2 id="sidebar-agents-title">Agent team</h2>
          <ul>
            {['Demand', 'Supply', 'Capital', 'Sales'].map((dimension) => (
              <li key={dimension} data-active={latestActor.includes(dimension.toLowerCase())}><i />{dimension}</li>
            ))}
          </ul>
        </section>

        <WorkspaceAccountMenu backendState={error ? 'Backend needs attention' : run ? `Backend ${connection}` : 'Backend ready'} />
      </aside>

      <main className="conversation-entry-main" data-session={sessionOpen}>
        <a href="/" className="conversation-site-link">Website <span aria-hidden="true">↗</span></a>

        {!sessionOpen ? (
          <div className="conversation-entry-center">
            <div className="conversation-entry-mark" aria-hidden="true">
              <img src="/ophira-mark.svg" alt="" width="48" height="48" />
            </div>
            <p className="conversation-entry-kicker">Opportunity Lead</p>
            <h1>What opportunity should we explore?</h1>

            {composer}

            <section className="conversation-quick-starts" aria-labelledby="quick-starts-title">
              <h2 id="quick-starts-title">Start with a validation task</h2>
              <div className="conversation-quick-grid">
                {quickStarts.map((item, index) => (
                  <button key={item.dimension} type="button" data-dimension={item.dimension.toLowerCase()} onClick={() => onChange(item.prompt)}>
                    <span>{String(index + 1).padStart(2, '0')} · {item.dimension}</span>
                    <strong>{item.title}</strong>
                    <i aria-hidden="true">↗</i>
                  </button>
                ))}
              </div>
            </section>
          </div>
        ) : (
          <section className="conversation-session" aria-labelledby="conversation-session-title">
            <header className="conversation-session-header">
              <div>
                <p>Opportunity Lead · One project truth</p>
                <h1 id="conversation-session-title">{project?.name ?? value}</h1>
              </div>
              <div className="conversation-run-state">
                <span data-status={run?.status ?? 'saved'}>{label(run?.status ?? 'Saved')}</span>
                {run && <small>{connection} · Trace {run.trace_id}</small>}
                {isActive && <button type="button" onClick={onCancel}>Cancel</button>}
              </div>
            </header>

            <div className="conversation-thread">
              <article className="conversation-user-message">
                <span>You</span>
                <p>{project?.objective ?? value}</p>
              </article>

              <article className="conversation-agent-message">
                <header>
                  <span><img src="/ophira-mark.svg" alt="" width="24" height="24" /></span>
                  <div><strong>Opportunity Lead</strong><small>{run ? `${label(run.status)} · ${connection}` : 'Saved project'}</small></div>
                </header>

                {assistantText ? (
                  <p className="conversation-agent-output">{assistantText}</p>
                ) : (
                  <div className="conversation-agent-waiting" role="status">
                    {isActive && <i aria-hidden="true" />}
                    <p>{isActive
                      ? 'The backend is connected. Opportunity Lead is coordinating the specialist Agents…'
                      : run?.error || 'This project truth is loaded from the backend. Start a new research brief to run the team again.'}</p>
                  </div>
                )}

                {reasoningSummary && (
                  <details className="conversation-reasoning">
                    <summary>Visible reasoning summary</summary>
                    <p>{reasoningSummary}</p>
                  </details>
                )}

                {permissions.map((permission) => (
                  <section className="conversation-permission" key={permission.id} aria-label={`Approval for ${permission.toolName}`}>
                    <div><span>Approval required</span><strong>{permission.actor.name} wants to use {permission.toolName}</strong></div>
                    <div>
                      <button type="button" onClick={() => onDecision(permission.id, 'allow_once')}>Allow once</button>
                      <button type="button" onClick={() => onDecision(permission.id, 'allow_run')}>Allow for run</button>
                      <button type="button" onClick={() => onDecision(permission.id, 'deny')}>Deny</button>
                    </div>
                  </section>
                ))}

                {project && (
                  <section className="conversation-project-truth" aria-labelledby="project-truth-heading">
                    <div><span>Live project truth</span><h2 id="project-truth-heading">Four dimensions, one decision.</h2></div>
                    <div className="conversation-assessment-grid">
                      {dimensions.map((dimension) => {
                        const assessment = project.assessments[dimension]
                        return (
                          <article key={dimension}>
                            <span>{label(dimension)}</span>
                            <strong>{assessment.score}</strong>
                            <small data-status={assessment.status}>{label(assessment.status)}</small>
                            <p>{assessment.summary || assessment.gaps[0] || 'Waiting for the specialist Agent.'}</p>
                          </article>
                        )
                      })}
                    </div>
                    {project.evidence.length > 0 && (
                      <div className="conversation-evidence-ledger">
                        <div><strong>Evidence ledger</strong><span>{project.evidence.length} records</span></div>
                        {project.evidence.slice(-4).reverse().map((item) => (
                          <article key={item.id}>
                            <small>{label(item.kind)} · {label(item.dimension)}</small>
                            <p>{item.claim}</p>
                            <span>{item.source_title}</span>
                          </article>
                        ))}
                      </div>
                    )}
                  </section>
                )}

                {recentActivity.length > 0 && (
                  <section className="conversation-activity" aria-labelledby="conversation-activity-heading">
                    <div><h2 id="conversation-activity-heading">Live backend activity</h2><span>{events.length} events</span></div>
                    <ol>
                      {recentActivity.map((event) => (
                        <li key={event.id}><i data-event={event.type} /><span>{activityLabel(event)}</span><small>{event.actor.name}</small></li>
                      ))}
                    </ol>
                  </section>
                )}
              </article>
            </div>
          </section>
        )}

        {sessionOpen && composer}
      </main>
    </div>
  )
}
