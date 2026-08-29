import { useEffect, useRef, useState, type ChangeEvent, type FormEvent } from 'react'
import Markdown from 'react-markdown'
import {
  dimensions,
  type AskUserQuestionItem,
  type OpportunityProject,
  type OphiraEvent,
  type PendingPermission,
  type PermissionAnswers,
  type RunRecord,
  type RunMode,
} from '../api/types'
import { builtInSkills } from '../data/skillCatalog'
import { useLanguage, useT, type Translate } from '../i18n'
import Brand from './Brand'
import EvidenceDrawer from './EvidenceDrawer'
import WorkspaceNav from './WorkspaceNav'
import './workspace-composer.css'

type WorkspaceComposerProps = {
  value: string
  attachedFiles: File[]
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
  runMode: RunMode
  onChange: (value: string) => void
  onAttachedFilesChange: (files: File[]) => void
  onRunModeChange: (mode: RunMode) => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
  onProjectRefresh: () => void
  onNewResearch: () => void
  onSelectProject: (projectId: string) => void
  onCancel: () => void
  onDecision: (
    requestId: string,
    decision: 'allow_once' | 'allow_run' | 'deny',
    answers?: PermissionAnswers,
  ) => void
}

type ComposerMenuView = 'closed' | 'root' | 'mode' | 'skills'
type ComposerToolIconName = 'files' | 'mode' | 'experts' | 'skills' | 'connectors'

function ComposerToolIcon({ name }: { name: ComposerToolIconName }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      {name === 'files' && (
        <path d="m8.8 12.8 5.7-5.7a3 3 0 0 1 4.2 4.2l-7.2 7.2a5 5 0 0 1-7.1-7.1l7-7a3.5 3.5 0 0 1 5 5l-7.1 7.1a2 2 0 1 1-2.8-2.8l6.4-6.4" />
      )}
      {name === 'mode' && (
        <>
          <path d="M4 20c1.7-4.8 5.1-8.2 10-10" />
          <path d="M8.6 15.4c-1.8-2.9-.6-6.7 2.6-8.1L20 3.5l-3.8 8.8c-1.4 3.2-5.2 4.4-8.1 2.6" />
          <path d="m5 4 .5 1.5L7 6l-1.5.5L5 8l-.5-1.5L3 6l1.5-.5L5 4Z" />
        </>
      )}
      {name === 'experts' && (
        <>
          <circle cx="12" cy="8" r="3.2" />
          <path d="M5.8 19c.6-3.5 2.7-5.3 6.2-5.3s5.6 1.8 6.2 5.3" />
          <path d="m17.5 4.5.5 1.2 1.2.5-1.2.5-.5 1.2-.5-1.2-1.2-.5 1.2-.5.5-1.2Z" />
        </>
      )}
      {name === 'skills' && (
        <>
          <path d="m12 3 7 7-7 7-7-7 7-7Z" />
          <path d="m9 18 3 3 3-3" />
        </>
      )}
      {name === 'connectors' && (
        <>
          <path d="m9.5 14.5-1.2 1.2a3.6 3.6 0 1 1-5.1-5.1l3.4-3.4a3.6 3.6 0 0 1 5.1 0" />
          <path d="m14.5 9.5 1.2-1.2a3.6 3.6 0 1 1 5.1 5.1l-3.4 3.4a3.6 3.6 0 0 1-5.1 0" />
          <path d="m8.5 15.5 7-7" />
        </>
      )}
    </svg>
  )
}

function parseQuestions(toolInput: unknown): AskUserQuestionItem[] {
  if (!toolInput || typeof toolInput !== 'object') return []
  const questions = (toolInput as { questions?: unknown }).questions
  if (!Array.isArray(questions)) return []
  return questions.filter(
    (item): item is AskUserQuestionItem =>
      !!item && typeof item === 'object'
      && typeof (item as AskUserQuestionItem).question === 'string'
      && Array.isArray((item as AskUserQuestionItem).options),
  )
}

type QuestionCardProps = {
  permission: PendingPermission
  questions: AskUserQuestionItem[]
  onDecision: WorkspaceComposerProps['onDecision']
}

function QuestionCard({ permission, questions, onDecision }: QuestionCardProps) {
  const t = useT()
  const [selected, setSelected] = useState<Record<string, string[]>>({})

  const toggle = (question: AskUserQuestionItem, label: string) => {
    setSelected((current) => {
      const existing = current[question.question] ?? []
      if (question.multiSelect) {
        const next = existing.includes(label)
          ? existing.filter((item) => item !== label)
          : [...existing, label]
        return { ...current, [question.question]: next }
      }
      return { ...current, [question.question]: [label] }
    })
  }

  const complete = questions.every((question) => (selected[question.question] ?? []).length > 0)

  const submit = () => {
    const answers: PermissionAnswers = {}
    for (const question of questions) {
      const labels = selected[question.question] ?? []
      answers[question.question] = question.multiSelect ? labels : (labels[0] ?? '')
    }
    onDecision(permission.id, 'allow_once', answers)
  }

  return (
    <section className="conversation-permission conversation-question" aria-label={t('Agent question')}>
      <div><span>{t('TradeMind needs your input')}</span></div>
      {questions.map((question) => (
        <fieldset key={question.question}>
          <legend>
            {question.header && <small>{question.header}</small>}
            <strong>{question.question}</strong>
          </legend>
          <div className="conversation-question-options" role={question.multiSelect ? 'group' : 'radiogroup'}>
            {question.options.map((option) => {
              const active = (selected[question.question] ?? []).includes(option.label)
              return (
                <button
                  type="button"
                  key={option.label}
                  data-active={active || undefined}
                  aria-pressed={active}
                  onClick={() => toggle(question, option.label)}
                >
                  <strong>{option.label}</strong>
                  {option.description && <small>{option.description}</small>}
                </button>
              )
            })}
          </div>
        </fieldset>
      ))}
      <div>
        <button type="button" disabled={!complete} onClick={submit}>{t('Submit answers')}</button>
        <button type="button" onClick={() => onDecision(permission.id, 'deny')}>{t('Skip — continue with what is known')}</button>
      </div>
    </section>
  )
}

const quickStarts = [
  {
    title: 'Find what sells',
    prompt: 'Find products gaining demand on Amazon and TikTok Shop',
    image: '/images/recommendations/product-discovery-classical.jpg',
  },
  {
    title: 'Source with confidence',
    prompt: 'Compare suppliers, quotes, and landed margin for this product',
    image: '/images/recommendations/global-sourcing-classical.jpg',
  },
  {
    title: 'Localize to convert',
    prompt: 'Localize this listing for the US, UK, and Germany',
    image: '/images/recommendations/localize-convert-classical.jpg',
  },
  {
    title: 'Launch and grow',
    prompt: 'Turn this product into a launch plan across Shopify and social',
    image: '/images/recommendations/launch-growth-classical.jpg',
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

function eventTitle(event: OphiraEvent, t: Translate): string {
  const toolName = typeof event.data.tool_name === 'string' ? label(event.data.tool_name.replace(/^mcp__ophira__/, '')) : 'Tool'
  const actorName = typeof event.data.agent_type === 'string' ? label(event.data.agent_type) : label(event.actor.name)
  const text = typeof event.data.text === 'string' ? event.data.text : ''
  const description = typeof event.data.description === 'string' ? event.data.description : ''
  const summary = typeof event.data.summary === 'string' ? event.data.summary : ''
  const permissionDecisionLabels: Record<string, string> = {
    allow_once: 'Allow once',
    allow_run: 'Allow for run',
    deny: 'Deny',
  }
  const decision = typeof event.data.decision === 'string'
    ? t(permissionDecisionLabels[event.data.decision] ?? label(event.data.decision))
    : ''

  if (event.type === 'tool.planned') return t('Preparing {tool}', { tool: toolName })
  if (event.type === 'tool.started') return t('Using {tool}', { tool: toolName })
  if (event.type === 'tool.completed') return t('{tool} completed', { tool: toolName })
  if (event.type === 'tool.failed') return t('{tool} failed', { tool: toolName })
  if (event.type === 'subagent.started') return t('{actor} joined the work', { actor: actorName })
  if (event.type === 'subagent.stopped') return t('{actor} finished', { actor: actorName })
  if (event.type === 'task.started') return description || t('Agent task started')
  if (event.type === 'task.progress') return description || t('Agent task updated')
  if (event.type === 'task.completed') return summary || t('Agent task completed')
  if (event.type === 'assistant.message.completed' && text) return text
  if (event.type === 'permission.requested') return t('Approval requested for {tool}', { tool: toolName })
  if (event.type === 'permission.resolved') return decision ? t('Confirmed: {decision}', { decision }) : t('Approval confirmed')
  if (event.type === 'run.result') return event.data.is_error ? t('Research ended with an error') : t('Research response prepared')
  return t(activityLabel(event))
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
  const t = useT()
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
        <section className="conversation-account-menu" role="dialog" aria-label={t('Workspace account menu')}>
          <header>
            <span>TM</span>
            <div><strong>{t('TradeMind workspace')}</strong><small>{t('Local development workspace')}</small></div>
          </header>

          <div className="conversation-account-plan">
            <div><strong>{t('Developer preview')}</strong><span>{t('Local')}</span></div>
            <p><span>{t('Runtime')}</span><strong>{backendState}</strong></p>
            <small>{t('Usage follows the AI provider configured on this backend.')}</small>
          </div>

          <nav aria-label={t('Account destinations')}>
            <a href="/app/settings"><span><strong>{t('Settings')}</strong><small>{t('Permissions, runtime, and language')}</small></span><i aria-hidden="true">→</i></a>
            <a href="/"><span><strong>{t('Homepage')}</strong><small>{t('Return to the TradeMind website')}</small></span><i aria-hidden="true">↗</i></a>
            <a href="https://github.com/Janettbella69/Open-Acciowork" target="_blank" rel="noreferrer"><span><strong>GitHub</strong><small>{t('Open the source repository')}</small></span><i aria-hidden="true">↗</i></a>
          </nav>

          <footer>{t('Account authentication and billing are not enabled in this local build.')}</footer>
        </section>
      )}

      <button
        className="conversation-sidebar-profile"
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={t('Open account menu for TradeMind workspace')}
        onClick={() => setOpen((current) => !current)}
      >
        <span>TM</span>
        <div><strong>{t('TradeMind workspace')}</strong><small>{backendState}</small></div>
        <i aria-hidden="true">⌃</i>
      </button>
    </div>
  )
}

export default function WorkspaceComposer({
  value,
  attachedFiles,
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
  runMode,
  onChange,
  onAttachedFilesChange,
  onRunModeChange,
  onSubmit,
  onProjectRefresh,
  onNewResearch,
  onSelectProject,
  onCancel,
  onDecision,
}: WorkspaceComposerProps) {
  const [composerMenu, setComposerMenu] = useState<ComposerMenuView>('closed')
  const [clock, setClock] = useState(() => Date.now())
  const t = useT()
  const language = useLanguage()
  const composerToolsRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const sessionOpen = Boolean(project || run)
  const hasProjectTruth = Boolean(project && (
    project.evidence.length
    || project.hypotheses.length
    || project.decisions.length
    || project.artifacts.length
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
    if (composerMenu === 'closed') return

    const closeOnOutsidePress = (event: PointerEvent) => {
      if (!composerToolsRef.current?.contains(event.target as Node)) setComposerMenu('closed')
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setComposerMenu('closed')
    }

    document.addEventListener('pointerdown', closeOnOutsidePress)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePress)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [composerMenu])

  useEffect(() => {
    if (!isActive) return
    setClock(Date.now())
    const timer = window.setInterval(() => setClock(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [isActive])

  const stageFiles = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? [])
    if (files.length) onAttachedFilesChange([...attachedFiles, ...files])
    event.target.value = ''
  }

  const selectSkill = (prompt: string) => {
    onChange(prompt)
    setComposerMenu('closed')
  }

  const chooseFiles = () => {
    setComposerMenu('closed')
    fileInputRef.current?.click()
  }

  const selectRunMode = (mode: RunMode) => {
    onRunModeChange(mode)
    setComposerMenu('closed')
  }

  const composer = (
    <div className="conversation-composer-dock" data-inline={!sessionOpen}>
      <form className="conversation-composer" onSubmit={onSubmit}>
        <label className="conversation-visually-hidden" htmlFor="workspace-conversation-brief">{t('Ask TradeMind')}</label>
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
          placeholder={sessionOpen ? t('Continue this conversation…') : t('Message TradeMind…')}
          rows={sessionOpen ? 1 : 3}
          disabled={busy}
          autoFocus={!sessionOpen}
        />
        {attachedFiles.length > 0 && (
          <div className="conversation-attachments" aria-label={t('Locally staged attachments')}>
            <div>
              {attachedFiles.map((file, index) => (
                <span key={`${file.name}-${file.size}-${index}`}>
                  <i aria-hidden="true">↗</i>
                  <b>{file.name}</b>
                  <button
                    type="button"
                    aria-label={t('Remove {file}', { file: file.name })}
                    onClick={() => onAttachedFilesChange(attachedFiles.filter((_, currentIndex) => currentIndex !== index))}
                  >×</button>
                </span>
              ))}
            </div>
            <small>{t('Uploaded into the isolated project workspace when you send')}</small>
          </div>
        )}
        <div className="conversation-composer-footer">
          <div className="conversation-tools" ref={composerToolsRef}>
            <input
              ref={fileInputRef}
              className="conversation-file-input"
              type="file"
              aria-label={t('Choose files or images')}
              accept="image/*,.pdf,.txt,.csv,.doc,.docx,.xls,.xlsx,.ppt,.pptx"
              multiple
              onChange={stageFiles}
            />
            <button
              className="conversation-tool-menu-trigger"
              type="button"
              aria-label={t('Open tools menu')}
              title={t('Open tools menu')}
              aria-haspopup="dialog"
              aria-expanded={composerMenu !== 'closed'}
              onClick={() => setComposerMenu((current) => current === 'closed' ? 'root' : 'closed')}
            ><span aria-hidden="true">＋</span></button>

            {composerMenu === 'root' && (
              <section className="conversation-tools-menu" role="dialog" aria-label={t('Composer tools')}>
                <button className="conversation-tool-menu-row conversation-tool-menu-row-files" type="button" onClick={chooseFiles}>
                  <ComposerToolIcon name="files" />
                  <strong>{t('Add Files')}</strong>
                  <i aria-hidden="true">›</i>
                </button>
                <button className="conversation-tool-menu-row" type="button" onClick={() => setComposerMenu('mode')}>
                  <ComposerToolIcon name="mode" />
                  <strong>{t('Mode')}</strong>
                  <i aria-hidden="true">›</i>
                </button>
                <a className="conversation-tool-menu-row" href="/app/agents">
                  <ComposerToolIcon name="experts" />
                  <strong>{t('Experts')}</strong>
                  <i aria-hidden="true">›</i>
                </a>
                <button className="conversation-tool-menu-row" type="button" onClick={() => setComposerMenu('skills')}>
                  <ComposerToolIcon name="skills" />
                  <strong>{t('Skills')}</strong>
                  <i aria-hidden="true">›</i>
                </button>
                <a className="conversation-tool-menu-row" href="/app/plugins/connectors">
                  <ComposerToolIcon name="connectors" />
                  <strong>{t('Connectors')}</strong>
                  <i aria-hidden="true">›</i>
                </a>
              </section>
            )}

            {composerMenu === 'mode' && (
              <section className="conversation-mode-menu" role="dialog" aria-label={t('Choose a mode')}>
                <header>
                  <button type="button" aria-label={t('Back to tools')} onClick={() => setComposerMenu('root')}>←</button>
                  <div><span>{t('Run mode')}</span><strong>{t('Choose a mode')}</strong></div>
                </header>
                <div>
                  <button type="button" data-active={runMode === 'standard' || undefined} onClick={() => selectRunMode('standard')}>
                    <ComposerToolIcon name="experts" />
                    <span><strong>{t('Single Agent')}</strong><small>{t('Standard · one Agent')}</small></span>
                    <i aria-hidden="true">{runMode === 'standard' ? '✓' : ''}</i>
                  </button>
                  <button type="button" data-active={runMode === 'deep_research' || undefined} onClick={() => selectRunMode('deep_research')}>
                    <ComposerToolIcon name="mode" />
                    <span><strong>{t('Deep Research')}</strong><small>{t('Deep Research · explicit beta')}</small></span>
                    <i aria-hidden="true">{runMode === 'deep_research' ? '✓' : ''}</i>
                  </button>
                </div>
              </section>
            )}

            {composerMenu === 'skills' && (
              <section className="conversation-skills-menu" role="dialog" aria-label={t('Choose a TradeMind skill')}>
                <header>
                  <button type="button" aria-label={t('Back to tools')} onClick={() => setComposerMenu('root')}>←</button>
                  <div><span>{t('Built-in capabilities')}</span><strong>{t('Use a skill')}</strong></div>
                  <a href="/app/plugins/skills">{t('Skills')} <i aria-hidden="true">→</i></a>
                </header>
                <div>
                  {builtInSkills.map((skill) => (
                    <button key={skill.id} type="button" onClick={() => selectSkill(t(skill.prompt))}>
                      <span data-category={skill.category.toLowerCase()}>{skill.code}</span>
                      <div><strong>{t(skill.name)}</strong><small>{t(skill.description)}</small></div>
                      <i aria-hidden="true">→</i>
                    </button>
                  ))}
                </div>
                <footer>{t('Installed Plugin Skills are discovered by the Agent at runtime; this menu only helps frame intent.')}</footer>
              </section>
            )}
          </div>
          <span className="conversation-composer-coordinator">{t('⌘ Enter to send')}</span>
          <button className="conversation-submit" type="submit" aria-label={busy ? t('Working') : t('Send')} disabled={busy || !value.trim()}>
            <span className="conversation-submit-label">{busy ? t('Working…') : t('Send')}</span>
            <span aria-hidden="true">↑</span>
          </button>
        </div>
      </form>
      {notice && <p className="conversation-notice" role="status">{t(notice)}</p>}
      {error && (sessionOpen || value.trim()) && <p className="conversation-error" role="alert">{t(error)}</p>}
    </div>
  )

  return (
    <div className="conversation-entry" data-session={sessionOpen}>
      <aside className="conversation-sidebar">
        <a className="conversation-sidebar-brand" href="/" aria-label={t('Back to TradeMind website')}><Brand /></a>

        <button className="conversation-new-research" type="button" onClick={onNewResearch}>
          <span aria-hidden="true">＋</span> {t('New task')}
        </button>

        <WorkspaceNav />

        <section className="conversation-sidebar-projects" aria-labelledby="sidebar-projects-title">
          <div><h2 id="sidebar-projects-title">{t('Recents')}</h2><span>{projects.length}</span></div>
          {projects.length > 0 ? projects.map((savedProject) => (
            <button
              key={savedProject.id}
              type="button"
              aria-pressed={project?.id === savedProject.id}
              onClick={() => onSelectProject(savedProject.id)}
            >
              <span>{savedProject.name}</span>
              <small>{t(label(savedProject.stage))} · {new Date(savedProject.updated_at).toLocaleDateString(language === 'zh-CN' ? 'zh-CN' : 'en', { month: 'short', day: 'numeric' })}</small>
            </button>
          )) : <p>{t('Your saved conversations will appear here.')}</p>}
        </section>

        <WorkspaceAccountMenu backendState={error ? t('Backend needs attention') : run ? t('Backend {state}', { state: connection }) : t('Backend ready')} />
      </aside>

      <main className="conversation-entry-main" data-session={sessionOpen}>
        {!sessionOpen && (
          <a href="/" className="conversation-site-link">{t('Website')} <span aria-hidden="true">↗</span></a>
        )}

        {!sessionOpen ? (
          <div className="conversation-entry-center">
            <h1>What can I do for you?</h1>

            {composer}

            <section className="conversation-quick-starts" aria-label={t('Example opportunities')}>
              <div className="conversation-quick-grid">
                {quickStarts.map((item) => (
                  <button key={item.title} type="button" onClick={() => onChange(t(item.prompt))}>
                    <img src={item.image} alt="" loading="eager" />
                    <strong>{t(item.title)}</strong>
                  </button>
                ))}
              </div>
            </section>
          </div>
        ) : (
          <section className="conversation-session" aria-label={t('Conversation with TradeMind')}>
            <div className="conversation-thread">
              <article className="conversation-user-message">
                <p>{run?.prompt ?? project?.objective ?? value}</p>
              </article>

              <article className="conversation-agent-message">
                {(run || historyLoading) && (
                  <div className="conversation-activity">
                    {/* 上线前处于调试阶段：过程默认展开，先过程后结果（教材第 11 章
                        "调试阶段还是显示原始日志"）。上线后按开发者/用户模式再分档。 */}
                    <details className="conversation-process" open>
                      <summary>
                        <span>{historyLoading ? t('Restoring history') : isActive ? t('Working for {elapsed}', { elapsed }) : t('Ran for {elapsed}', { elapsed })}</span>
                        <i aria-hidden="true">›</i>
                      </summary>
                      <div className="conversation-process-body">
                        <header>
                          <span>{historyLoading ? t('Restoring') : t(label(run?.status ?? 'Saved'))}</span>
                          <small>{t(processSteps === 1 ? '{n} event' : '{n} events', { n: processSteps })} · {t(label(connection))}</small>
                        </header>
                        {(reasoningSummary || traceEvents.length > 0) && (
                          <ol>
                            {reasoningSummary && (
                              <li data-kind="reasoning">
                                <details>
                                  <summary>
                                    <i aria-hidden="true">◇</i>
                                    <span><strong>{isActive ? t('Thinking') : t('Approach')}</strong><small>{t('Reasoning summary')}</small></span>
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
                                      <span><strong>{eventTitle(event, t)}</strong><small>{label(event.actor.name)} · {new Date(event.timestamp).toLocaleTimeString(language === 'zh-CN' ? 'zh-CN' : 'en', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</small></span>
                                      <b aria-hidden="true">›</b>
                                    </summary>
                                    <div className="conversation-event-detail">
                                      <dl>
                                        <div><dt>{t('Event')}</dt><dd>{event.type}</dd></div>
                                        <div><dt>{t('Actor')}</dt><dd>{t(label(event.actor.type))}</dd></div>
                                        {event.actor.agent_id && <div><dt>{t('Agent ID')}</dt><dd>{event.actor.agent_id}</dd></div>}
                                        {event.tool_use_id && <div><dt>{t('Tool use ID')}</dt><dd>{event.tool_use_id}</dd></div>}
                                        {event.parent_id && <div><dt>{t('Parent')}</dt><dd>{event.parent_id}</dd></div>}
                                      </dl>
                                      <pre>{eventDetail(event)}</pre>
                                    </div>
                                  </details>
                                </li>
                              )
                            })}
                          </ol>
                        )}
                        {run && (
                          <p className="conversation-trace-note">
                            {run.mode === 'deep_research' ? `${t('Deep Research')} · ` : `${t('Single Agent')} · `}
                            {t('Trace')} {run.trace_id}
                          </p>
                        )}
                      </div>
                    </details>
                    {isActive && <button type="button" onClick={onCancel}>{t('Cancel')}</button>}
                  </div>
                )}

                {historyLoading ? (
                  <div className="conversation-agent-waiting" role="status">
                    <i aria-hidden="true" />
                    <p>{t('Restoring the recorded response and activity…')}</p>
                  </div>
                ) : assistantText ? (
                  <div className="conversation-agent-output">
                    <Markdown>{assistantText}</Markdown>
                  </div>
                ) : run?.status === 'failed' ? (
                  <div className="conversation-run-outcome" data-status="failed" role="alert">
                    <strong>{t('Run failed')}</strong>
                    <p>{run.error || t('The backend ended this run without a response.')}</p>
                  </div>
                ) : (
                  <div className="conversation-agent-waiting" role="status">
                    {isActive && <i aria-hidden="true" />}
                    <p>{isActive
                      ? t('TradeMind is working and using the available tools as needed…')
                      : run?.status === 'cancelled'
                        ? t('This run was cancelled.')
                        : t('Send a message to continue from this project.')}</p>
                  </div>
                )}

                {permissions.map((permission) => {
                  if (permission.toolName === 'AskUserQuestion') {
                    const questions = parseQuestions(permission.toolInput)
                    if (questions.length > 0) {
                      return (
                        <QuestionCard
                          key={permission.id}
                          permission={permission}
                          questions={questions}
                          onDecision={onDecision}
                        />
                      )
                    }
                  }
                  return (
                    <section className="conversation-permission" key={permission.id} aria-label={t('Approval for {tool}', { tool: permission.toolName })}>
                      <div><span>{t('Approval required')}</span><strong>{t('{actor} wants to use {tool}', { actor: permission.actor.name, tool: permission.toolName })}</strong></div>
                      <div>
                        <button type="button" onClick={() => onDecision(permission.id, 'allow_once')}>{t('Allow once')}</button>
                        <button type="button" onClick={() => onDecision(permission.id, 'allow_run')}>{t('Allow for run')}</button>
                        <button type="button" onClick={() => onDecision(permission.id, 'deny')}>{t('Deny')}</button>
                      </div>
                    </section>
                  )
                })}

                {project && hasProjectTruth && (
                  <details className="conversation-project-truth">
                    <summary>
                      <span>{t('Project record')}</span>
                      <small>{t('{n} evidence records', { n: project.evidence.length })}</small>
                      <i aria-hidden="true">›</i>
                    </summary>
                    <div className="conversation-project-truth-body">
                      {dimensions.some((dimension) => project.assessments[dimension]) && (
                        <div className="conversation-assessment-grid">
                          {dimensions.map((dimension) => {
                            const assessment = project.assessments[dimension]
                            if (!assessment) return null
                            return (
                              <article key={dimension}>
                                <span>{t(label(dimension))}</span>
                                <strong>{assessment.score}</strong>
                                <small data-status={assessment.status}>{t(label(assessment.status))}</small>
                                <p>{assessment.summary || assessment.gaps[0] || t('No recorded assessment.')}</p>
                              </article>
                            )
                          })}
                        </div>
                      )}
                      {project.evidence.length > 0 && (
                        <div className="conversation-evidence-ledger">
                          <div><strong>{t('Evidence ledger')}</strong><span>{t('{n} records', { n: project.evidence.length })}</span></div>
                          <EvidenceDrawer project={project} onChanged={onProjectRefresh} />
                        </div>
                      )}
                      {project.decisions.length > 0 && (
                        <div className="conversation-evidence-ledger">
                          <div><strong>{t('Decisions')}</strong><span>{t('{n} recorded', { n: project.decisions.length })}</span></div>
                          {project.decisions.slice(-3).reverse().map((decision) => (
                            <article key={decision.id} data-status={decision.status}>
                              <small>{t(label(decision.decision_type))} · {t(label(decision.status))}</small>
                              <p>{decision.outcome}</p>
                              <span>{t('{n} linked evidence records', { n: decision.evidence_ids.length })}</span>
                              {decision.invalidated_by_evidence_ids.length > 0 && (
                                <em className="conversation-decision-invalidated" role="alert">
                                  ⚠ {t('{n} supporting evidence records are no longer active and require review', {
                                    n: decision.invalidated_by_evidence_ids.length,
                                  })}
                                </em>
                              )}
                            </article>
                          ))}
                        </div>
                      )}
                      {project.artifacts.length > 0 && (
                        <div className="conversation-evidence-ledger">
                          <div><strong>{t('Artifacts')}</strong><span>{t('{n} files', { n: project.artifacts.length })}</span></div>
                          {project.artifacts.slice(-4).reverse().map((artifact) => (
                            <article key={artifact.id}>
                              <small>{artifact.mime} · v{artifact.version}</small>
                              <p>{artifact.name}</p>
                              <a href={artifact.download_url}>{t('Download')}</a>
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
