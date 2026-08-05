import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import Markdown from 'react-markdown'
import {
  dimensions,
  type OpportunityProject,
  type OphiraEvent,
  type PendingPermission,
  type RunRecord,
} from '../api/types'
import { builtInSkills } from '../data/skillCatalog'
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
  historyLoading: boolean
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

function eventDetail(event: OphiraEvent): string {
  try {
    return JSON.stringify(event.data, null, 2)
  } catch {
    return String(event.data)
  }
}

function eventKind(event: OphiraEvent): string {
  if (event.type.startsWith('tool.')) return 'tool'
  if (event.type.startsWith('assistant.reasoning')) return 'reasoning'
  if (event.type.startsWith('assistant.')) return 'assistant'
  if (event.type.startsWith('subagent.') || event.type.startsWith('task.')) return 'agent'
  if (event.type.startsWith('permission.')) return 'permission'
  return 'system'
}

function eventTitle(event: OphiraEvent): string {
  const toolName = typeof event.data.tool_name === 'string' ? label(event.data.tool_name.replace(/^mcp__ophira__/, '')) : 'Tool'
  const actorName = typeof event.data.agent_type === 'string' ? label(event.data.agent_type) : label(event.actor.name)
  const text = typeof event.data.text === 'string' ? event.data.text : ''
  const description = typeof event.data.description === 'string' ? event.data.description : ''
  const summary = typeof event.data.summary === 'string' ? event.data.summary : ''
  const decision = typeof event.data.decision === 'string' ? label(event.data.decision) : ''

  if (event.type === 'tool.planned') return `Preparing ${toolName}`
  if (event.type === 'tool.started') return `Using ${toolName}`
  if (event.type === 'tool.completed') return `${toolName} completed`
  if (event.type === 'tool.failed') return `${toolName} failed`
  if (event.type === 'subagent.started') return `${actorName} joined the work`
  if (event.type === 'subagent.stopped') return `${actorName} finished`
  if (event.type === 'task.started') return description || 'Agent task started'
  if (event.type === 'task.progress') return description || 'Agent task updated'
  if (event.type === 'task.completed') return summary || 'Agent task completed'
  if (event.type === 'assistant.message.completed' && text) return text
  if (event.type === 'permission.requested') return `Approval requested for ${toolName}`
  if (event.type === 'permission.resolved') return decision ? `Confirmed: ${decision}` : 'Approval confirmed'
  if (event.type === 'run.result') return event.data.is_error ? 'Research ended with an error' : 'Research response prepared'
  return activityLabel(event)
}

function formatDuration(milliseconds: number): string {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return `${minutes}m ${remainder}s`
}

function processIcon(kind: string): string {
  if (kind === 'reasoning') return '◇'
  if (kind === 'tool') return '▣'
  if (kind === 'agent') return '●'
  if (kind === 'permission') return '?'
  if (kind === 'assistant') return '•'
  return '·'
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
  historyLoading,
  isActive,
  onChange,
  onSubmit,
  onNewResearch,
  onSelectProject,
  onCancel,
  onDecision,
}: WorkspaceComposerProps) {
  const [skillsOpen, setSkillsOpen] = useState(false)
  const [attachedFiles, setAttachedFiles] = useState<File[]>([])
  const [clock, setClock] = useState(() => Date.now())
  const composerToolsRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const sessionOpen = Boolean(project || run)
  const hasProjectTruth = Boolean(project && (
    project.evidence.length
    || project.hypotheses.length
    || Object.values(project.assessments).some((assessment) => (
      assessment.status !== 'unverified'
      || assessment.score > 0
      || assessment.summary
      || assessment.gaps.length
    ))
  ))
  const traceEvents = events.filter((event) => !event.type.endsWith('.delta') && !event.type.startsWith('assistant.reasoning'))
  const startedAt = run?.started_at ? new Date(run.started_at).getTime() : run ? new Date(run.created_at).getTime() : clock
  const finishedAt = run?.finished_at ? new Date(run.finished_at).getTime() : clock
  const elapsed = formatDuration(finishedAt - startedAt)
  const processSteps = traceEvents.length + (reasoningSummary ? 1 : 0)

  useEffect(() => {
    if (!skillsOpen) return

    const closeOnOutsidePress = (event: PointerEvent) => {
      if (!composerToolsRef.current?.contains(event.target as Node)) setSkillsOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setSkillsOpen(false)
    }

    document.addEventListener('pointerdown', closeOnOutsidePress)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePress)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [skillsOpen])

  useEffect(() => {
    if (!isActive) return
    setClock(Date.now())
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [isActive])

  const stageFiles = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? [])
    if (files.length) setAttachedFiles((current) => [...current, ...files])
    event.target.value = ''
  }

  const selectSkill = (prompt: string) => {
    onChange(prompt)
    setSkillsOpen(false)
  }

  const composer = (
    <div className="conversation-composer-dock" data-inline={!sessionOpen}>
      <form className="conversation-composer" onSubmit={onSubmit}>
        <label className="conversation-visually-hidden" htmlFor="workspace-conversation-brief">Ask Ophira</label>
        <textarea
          id="workspace-conversation-brief"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if ((event.metaKey || event.ctrlKey) && event.key === 'Enter' && !busy && value.trim()) {
              event.preventDefault()
              event.currentTarget.form?.requestSubmit()
            }
          }}
          placeholder={sessionOpen ? 'Continue this conversation…' : 'Message Ophira…'}
          rows={sessionOpen ? 1 : 3}
          disabled={busy}
          autoFocus={!sessionOpen}
        />
        {attachedFiles.length > 0 && (
          <div className="conversation-attachments" aria-label="Locally staged attachments">
            <div>
              {attachedFiles.map((file, index) => (
                <span key={`${file.name}-${file.size}-${index}`}>
                  <i aria-hidden="true">↗</i>
                  <b>{file.name}</b>
                  <button
                    type="button"
                    aria-label={`Remove ${file.name}`}
                    onClick={() => setAttachedFiles((current) => current.filter((_, currentIndex) => currentIndex !== index))}
                  >×</button>
                </span>
              ))}
            </div>
            <small>Selected locally for this draft</small>
          </div>
        )}
        <div className="conversation-composer-footer">
          <div className="conversation-tools" ref={composerToolsRef}>
            <input
              ref={fileInputRef}
              className="conversation-file-input"
              type="file"
              aria-label="Choose files or images"
              accept="image/*,.pdf,.txt,.csv,.doc,.docx,.xls,.xlsx,.ppt,.pptx"
              multiple
              onChange={stageFiles}
            />
            <button
              className="conversation-tool-add"
              type="button"
              aria-label="Add files or images"
              title="Add files or images"
              onClick={() => fileInputRef.current?.click()}
            >＋</button>
            <button
              className="conversation-tool-skills"
              type="button"
              aria-haspopup="dialog"
              aria-expanded={skillsOpen}
              onClick={() => setSkillsOpen((current) => !current)}
            ><span aria-hidden="true">◇</span> Skills</button>

            {skillsOpen && (
              <section className="conversation-skills-menu" role="dialog" aria-label="Choose an OPHIRA skill">
                <header><div><span>Built-in capabilities</span><strong>Use a skill</strong></div><a href="/app/skills">Skill Studio <i aria-hidden="true">→</i></a></header>
                <div>
                  {builtInSkills.map((skill) => (
                    <button key={skill.id} type="button" onClick={() => selectSkill(skill.prompt)}>
                      <span data-category={skill.category.toLowerCase()}>{skill.code}</span>
                      <div><strong>{skill.name}</strong><small>{skill.description}</small></div>
                      <i aria-hidden="true">→</i>
                    </button>
                  ))}
                </div>
                <footer>Selecting a skill inserts its instructions into this brief for review.</footer>
              </section>
            )}
          </div>
          <span className="conversation-composer-coordinator">⌘ Enter to send</span>
          <button className="conversation-submit" type="submit" aria-label={busy ? 'Working' : 'Send'} disabled={busy || !value.trim()}>
            <span className="conversation-submit-label">{busy ? 'Working…' : 'Send'}</span>
            <span aria-hidden="true">↑</span>
          </button>
        </div>
      </form>
      {notice && <p className="conversation-notice" role="status">{notice}</p>}
      {error && <p className="conversation-error" role="alert">{error}</p>}
    </div>
  )

  return (
    <div className="conversation-entry" data-session={sessionOpen}>
      <aside className="conversation-sidebar">
        <a className="conversation-sidebar-brand" href="/" aria-label="Back to OPHIRA website"><Brand /></a>

        <button className="conversation-new-research" type="button" onClick={onNewResearch}>
          <span aria-hidden="true">＋</span> New conversation
        </button>

        <nav className="conversation-sidebar-nav" aria-label="Workspace navigation">
          <p>Workspace</p>
          <a href="/app" aria-current="page"><span>01</span>Agent workspace</a>
          <a href="/app/schedules"><span>02</span>Scheduled tasks</a>
          <a href="/app/skills"><span>03</span>Skill Studio</a>
        </nav>

        <section className="conversation-sidebar-projects" aria-labelledby="sidebar-projects-title">
          <div><h2 id="sidebar-projects-title">Recent projects</h2><span>{projects.length}</span></div>
          {projects.length > 0 ? projects.map((savedProject) => (
            <button
              key={savedProject.id}
              type="button"
              aria-pressed={project?.id === savedProject.id}
              onClick={() => onSelectProject(savedProject.id)}
            >
              <span>{savedProject.name}</span>
              <small>{label(savedProject.stage)} · {new Date(savedProject.updated_at).toLocaleDateString([], { month: 'short', day: 'numeric' })}</small>
            </button>
          )) : <p>Your saved conversations will appear here.</p>}
        </section>

        <WorkspaceAccountMenu backendState={error ? 'Backend needs attention' : run ? `Backend ${connection}` : 'Backend ready'} />
      </aside>

      <main className="conversation-entry-main" data-session={sessionOpen}>
        {!sessionOpen && (
          <a href="/" className="conversation-site-link">Website <span aria-hidden="true">↗</span></a>
        )}

        {!sessionOpen ? (
          <div className="conversation-entry-center">
            <div className="conversation-entry-mark" aria-hidden="true">
              <img src="/ophira-mark.svg" alt="" width="48" height="48" />
            </div>
            <p className="conversation-entry-kicker">Ophira</p>
            <h1>What are you working on?</h1>

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
          <section className="conversation-session" aria-label="Conversation with Ophira">
            <div className="conversation-thread">
              <article className="conversation-user-message">
                <p>{run?.prompt ?? project?.objective ?? value}</p>
              </article>

              <article className="conversation-agent-message">
                {(run || historyLoading) && (
                  <div className="conversation-activity">
                    <details className="conversation-process">
                      <summary>
                        <span>{historyLoading ? 'Restoring history' : isActive ? `Working for ${elapsed}` : `Ran for ${elapsed}`}</span>
                        <i aria-hidden="true">›</i>
                      </summary>
                      <div className="conversation-process-body">
                        <header>
                          <span>{historyLoading ? 'Restoring' : label(run?.status ?? 'Saved')}</span>
                          <small>{processSteps} {processSteps === 1 ? 'event' : 'events'} · {connection}</small>
                        </header>
                        {(reasoningSummary || traceEvents.length > 0) && (
                          <ol>
                            {reasoningSummary && (
                              <li data-kind="reasoning">
                                <details>
                                  <summary>
                                    <i aria-hidden="true">◇</i>
                                    <span><strong>{isActive ? 'Thinking' : 'Approach'}</strong><small>Reasoning summary</small></span>
                                    <b aria-hidden="true">›</b>
                                  </summary>
                                  <div className="conversation-thought-detail"><p>{reasoningSummary}</p></div>
                                </details>
                              </li>
                            )}
                            {traceEvents.map((event) => {
                              const kind = eventKind(event)
                              return (
                                <li key={event.id} data-kind={kind}>
                                  <details>
                                    <summary>
                                      <i aria-hidden="true">{processIcon(kind)}</i>
                                      <span><strong>{eventTitle(event)}</strong><small>{label(event.actor.name)} · {new Date(event.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</small></span>
                                      <b aria-hidden="true">›</b>
                                    </summary>
                                    <div className="conversation-event-detail">
                                      <dl>
                                        <div><dt>Event</dt><dd>{event.type}</dd></div>
                                        <div><dt>Actor</dt><dd>{label(event.actor.type)}</dd></div>
                                        {event.actor.agent_id && <div><dt>Agent ID</dt><dd>{event.actor.agent_id}</dd></div>}
                                        {event.tool_use_id && <div><dt>Tool use ID</dt><dd>{event.tool_use_id}</dd></div>}
                                        {event.parent_id && <div><dt>Parent</dt><dd>{event.parent_id}</dd></div>}
                                      </dl>
                                      <pre>{eventDetail(event)}</pre>
                                    </div>
                                  </details>
                                </li>
                              )
                            })}
                          </ol>
                        )}
                        {run && <p className="conversation-trace-note">Trace {run.trace_id}</p>}
                      </div>
                    </details>
                    {isActive && <button type="button" onClick={onCancel}>Cancel</button>}
                  </div>
                )}

                {historyLoading ? (
                  <div className="conversation-agent-waiting" role="status">
                    <i aria-hidden="true" />
                    <p>Restoring the recorded response and activity…</p>
                  </div>
                ) : assistantText ? (
                  <div className="conversation-agent-output">
                    <Markdown>{assistantText}</Markdown>
                  </div>
                ) : run?.status === 'failed' ? (
                  <div className="conversation-run-outcome" data-status="failed" role="alert">
                    <strong>Run failed</strong>
                    <p>{run.error || 'The backend ended this run without a response.'}</p>
                  </div>
                ) : (
                  <div className="conversation-agent-waiting" role="status">
                    {isActive && <i aria-hidden="true" />}
                    <p>{isActive
                      ? 'Ophira is working and using the available tools as needed…'
                      : run?.status === 'cancelled'
                        ? 'This run was cancelled.'
                        : 'Send a message to continue from this project.'}</p>
                  </div>
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

                {project && hasProjectTruth && (
                  <details className="conversation-project-truth">
                    <summary>
                      <span>Project record</span>
                      <small>{project.evidence.length} evidence records</small>
                      <i aria-hidden="true">›</i>
                    </summary>
                    <div className="conversation-project-truth-body">
                      <div className="conversation-assessment-grid">
                        {dimensions.map((dimension) => {
                          const assessment = project.assessments[dimension]
                          return (
                            <article key={dimension}>
                              <span>{label(dimension)}</span>
                              <strong>{assessment.score}</strong>
                              <small data-status={assessment.status}>{label(assessment.status)}</small>
                              <p>{assessment.summary || assessment.gaps[0] || 'No recorded assessment.'}</p>
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
                    </div>
                  </details>
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
