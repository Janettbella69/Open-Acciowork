import { useEffect, useMemo, useState } from 'react'
import { createAgent, errorMessage, getProtocol, listAgents } from '../api/client'
import type { AgentProfile, AgentTemplate } from '../api/types'
import { useT } from '../i18n'
import Brand from './Brand'
import WorkspaceNav from './WorkspaceNav'
import './workspace-composer.css'
import './agents-page.css'

type DirectoryMode = 'experts' | 'teams'
type DirectoryScope = 'public' | 'personal'
type DirectorySort = 'recommended' | 'name'

type TemplatePresentation = {
  kind: DirectoryMode
  name: string
  description: string
  category: string
  tags: string[]
  mark: string
}

const knownTemplates: AgentTemplate[] = ['general', 'research', 'sourcing', 'marketing', 'docs', 'opportunity']

const templatePresentation: Record<AgentTemplate, TemplatePresentation> = {
  general: {
    kind: 'experts',
    name: 'Commerce Assistant',
    description: 'Handle broad commercial research, files, planning, and business deliverables from one accountable conversation.',
    category: 'General',
    tags: ['Coordinator', 'Operations'],
    mark: 'CA',
  },
  research: {
    kind: 'experts',
    name: 'Market Research Expert',
    description: 'Investigate markets and competitors with source grading, traceable citations, and structured findings.',
    category: 'Research',
    tags: ['Market research', 'Evidence'],
    mark: 'MR',
  },
  sourcing: {
    kind: 'experts',
    name: 'Sourcing Expert',
    description: 'Find and compare suppliers, prepare inquiry drafts, and keep MOQ, lead-time, and quality claims sourced.',
    category: 'Sourcing',
    tags: ['Suppliers', 'Landed cost'],
    mark: 'SE',
  },
  marketing: {
    kind: 'experts',
    name: 'Growth Marketing Expert',
    description: 'Create listing, SEO, and campaign drafts while keeping every external publishing action under approval.',
    category: 'Marketing',
    tags: ['Listings', 'Growth'],
    mark: 'GM',
  },
  docs: {
    kind: 'experts',
    name: 'Business Documents Expert',
    description: 'Organize, revise, and generate local business documents with version-aware file handling.',
    category: 'Documents',
    tags: ['Documents', 'Automation'],
    mark: 'BD',
  },
  opportunity: {
    kind: 'teams',
    name: 'Global Opportunity Team',
    description: 'One accountable lead coordinates demand, supply, capital, and sales perspectives around a shared opportunity record.',
    category: 'Commerce',
    tags: ['Demand', 'Supply', 'Capital', 'Sales'],
    mark: 'GO',
  },
}

function isAgentTemplate(value: string): value is AgentTemplate {
  return knownTemplates.includes(value as AgentTemplate)
}

function defaultRole(template: AgentTemplate): string {
  return templatePresentation[template].description
}

export default function AgentsPage() {
  const t = useT()
  const [mode, setMode] = useState<DirectoryMode>('experts')
  const [scope, setScope] = useState<DirectoryScope>('public')
  const [sort, setSort] = useState<DirectorySort>('recommended')
  const [category, setCategory] = useState('All')
  const [templates, setTemplates] = useState<AgentTemplate[]>([])
  const [agents, setAgents] = useState<AgentProfile[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [createdAgent, setCreatedAgent] = useState<AgentProfile | null>(null)
  const [editor, setEditor] = useState<{ name: string; template: AgentTemplate; role: string } | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let active = true
    void Promise.all([getProtocol(), listAgents()])
      .then(([protocol, storedAgents]) => {
        if (!active) return
        setTemplates(protocol.agent_templates.filter(isAgentTemplate))
        setAgents(storedAgents)
      })
      .catch((loadError) => {
        if (active) setError(errorMessage(loadError))
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => { active = false }
  }, [])

  const categories = useMemo(() => {
    const source = scope === 'public'
      ? templates
      : agents.map((agent) => agent.template)
    const visible = source
      .filter((template) => templatePresentation[template].kind === mode)
      .map((template) => templatePresentation[template].category)
    return ['All', ...Array.from(new Set(visible))]
  }, [agents, mode, scope, templates])

  useEffect(() => {
    if (!categories.includes(category)) setCategory('All')
  }, [categories, category])

  const visibleTemplates = useMemo(() => {
    const filtered = templates.filter((template) => {
      const presentation = templatePresentation[template]
      return presentation.kind === mode && (category === 'All' || presentation.category === category)
    })
    return sort === 'name'
      ? [...filtered].sort((left, right) => templatePresentation[left].name.localeCompare(templatePresentation[right].name))
      : filtered
  }, [category, mode, sort, templates])

  const visibleAgents = useMemo(() => {
    const filtered = agents.filter((agent) => {
      const presentation = templatePresentation[agent.template]
      return presentation.kind === mode && (category === 'All' || presentation.category === category)
    })
    return [...filtered].sort((left, right) => sort === 'name'
      ? left.name.localeCompare(right.name)
      : Date.parse(right.updated_at) - Date.parse(left.updated_at))
  }, [agents, category, mode, sort])

  const openCreator = (template: AgentTemplate = mode === 'teams' ? 'opportunity' : 'general') => {
    const presentation = templatePresentation[template]
    setEditor({ name: presentation.name, template, role: defaultRole(template) })
    setError('')
  }

  const changeTemplate = (template: AgentTemplate) => {
    const presentation = templatePresentation[template]
    setEditor({ name: presentation.name, template, role: defaultRole(template) })
  }

  const submitAgent = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!editor?.name.trim()) return
    setSaving(true)
    setError('')
    try {
      const agent = await createAgent({
        name: editor.name.trim(),
        template: editor.template,
        role_description: editor.role.trim(),
      })
      setAgents((current) => [agent, ...current.filter((item) => item.id !== agent.id)])
      setMode(templatePresentation[agent.template].kind)
      setScope('personal')
      setCategory('All')
      setCreatedAgent(agent)
      setNotice(t('{name} is ready in your personal Agents.', { name: agent.name }))
      setEditor(null)
    } catch (createError) {
      setError(errorMessage(createError))
    } finally {
      setSaving(false)
    }
  }

  const emptyCopy = scope === 'public'
    ? t('No Agent templates are available for this category.')
    : t('No personal Agents yet. Add a public template or create your own.')

  return (
    <div className="agents-shell">
      <aside className="conversation-sidebar agents-sidebar">
        <a className="conversation-sidebar-brand" href="/" aria-label={t('Back to TradeMind website')}><Brand /></a>
        <a className="conversation-new-research" href="/app"><span aria-hidden="true">＋</span> {t('New task')}</a>
        <WorkspaceNav active="agents" />
        <div className="agents-sidebar-note">
          <strong>{t('Agents use the same project truth')}</strong>
          <p>{t('Personal Agents change the working role—not the evidence, permission, or audit rules.')}</p>
        </div>
      </aside>

      <main className="agents-main">
        <header className="agents-header">
          <div>
            <h1>{t('Agents')}</h1>
            <p>{t('Choose a focused expert, assemble an opportunity team preset, or create a personal Agent for repeat work.')}</p>
          </div>
          <button type="button" onClick={() => openCreator()}><span aria-hidden="true">＋</span>{t('New Agent')}</button>
        </header>

        <section className="agents-directory" aria-labelledby="agents-directory-title">
          <div className="agents-directory-topline">
            <div className="agents-mode-tabs" role="tablist" aria-label={t('Agent directory type')}>
              <button type="button" role="tab" aria-selected={mode === 'experts'} onClick={() => setMode('experts')}>{t('Experts')}</button>
              <button type="button" role="tab" aria-selected={mode === 'teams'} onClick={() => setMode('teams')}>{t('Expert Teams')}</button>
            </div>
            <div className="agents-sort" role="group" aria-label={t('Sort Agents')}>
              <button type="button" data-active={sort === 'recommended'} onClick={() => setSort('recommended')}>{t('Recommended')}</button>
              <button type="button" data-active={sort === 'name'} onClick={() => setSort('name')}>{t('Name')}</button>
            </div>
          </div>

          <div className="agents-scope-row">
            <div className="agents-scope-tabs" role="tablist" aria-label={t('Agent ownership')}>
              <button type="button" role="tab" aria-selected={scope === 'public'} onClick={() => setScope('public')}>{t('Public')}</button>
              <button type="button" role="tab" aria-selected={scope === 'personal'} onClick={() => setScope('personal')}>{t('Personal')} <span>{agents.length}</span></button>
            </div>
            <div className="agents-categories" aria-label={t('Agent categories')}>
              {categories.map((item) => (
                <button key={item} type="button" data-active={category === item} onClick={() => setCategory(item)}>{t(item)}</button>
              ))}
            </div>
          </div>

          <h2 id="agents-directory-title" className="conversation-visually-hidden">{t('Agent directory')}</h2>
          {notice && createdAgent && (
            <div className="agents-notice" role="status">
              <span>{notice}</span>
              <a href={`/app?agent=${encodeURIComponent(createdAgent.id)}`}>{t('Chat now')} <i aria-hidden="true">→</i></a>
            </div>
          )}
          {error && <p className="agents-error" role="alert">{error}</p>}

          {loading ? (
            <div className="agents-empty"><strong>{t('Loading Agents…')}</strong></div>
          ) : ((scope === 'public' ? visibleTemplates.length : visibleAgents.length) ? (
            <div className="agents-grid">
              {scope === 'public' ? visibleTemplates.map((template) => {
                const presentation = templatePresentation[template]
                return (
                  <article className="agent-card" data-template={template} key={template}>
                    <header>
                      <span className="agent-card-avatar" aria-hidden="true">{presentation.mark}</span>
                      <div><h3>{t(presentation.name)}</h3><p>{t('TradeMind template')}</p></div>
                    </header>
                    <p>{t(presentation.description)}</p>
                    <div className="agent-card-tags">{presentation.tags.map((tag) => <span key={tag}>{t(tag)}</span>)}</div>
                    <footer>
                      <span>{presentation.kind === 'teams' ? t('One accountable lead') : t(presentation.category)}</span>
                      <button type="button" onClick={() => openCreator(template)}>{t('Use template')} <i aria-hidden="true">→</i></button>
                    </footer>
                  </article>
                )
              }) : visibleAgents.map((agent) => {
                const presentation = templatePresentation[agent.template]
                return (
                  <article className="agent-card" data-template={agent.template} key={agent.id}>
                    <header>
                      <span className="agent-card-avatar" aria-hidden="true">{presentation.mark}</span>
                      <div><h3>{agent.name}</h3><p>{t('Personal Agent')} · v{agent.revision}</p></div>
                    </header>
                    <p>{agent.role_description || t(presentation.description)}</p>
                    <div className="agent-card-tags">{presentation.tags.map((tag) => <span key={tag}>{t(tag)}</span>)}</div>
                    <footer>
                      <span>{t(presentation.category)}</span>
                      <a href={`/app?agent=${encodeURIComponent(agent.id)}`}>{t('Chat')} <i aria-hidden="true">→</i></a>
                    </footer>
                  </article>
                )
              })}
            </div>
          ) : (
            <div className="agents-empty">
              <strong>{emptyCopy}</strong>
              {scope === 'personal' && <button type="button" onClick={() => setScope('public')}>{t('Browse public templates')}</button>}
            </div>
          ))}
        </section>
      </main>

      {editor && (
        <div className="agents-dialog-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setEditor(null)
        }}>
          <section className="agents-dialog" role="dialog" aria-modal="true" aria-labelledby="agent-editor-title">
            <header>
              <div><span>{t('Personal Agent')}</span><h2 id="agent-editor-title">{t('Create an Agent')}</h2></div>
              <button type="button" aria-label={t('Close Agent creator')} onClick={() => setEditor(null)}>×</button>
            </header>
            <form onSubmit={(event) => void submitAgent(event)}>
              <label>
                {t('Template')}
                <select value={editor.template} onChange={(event) => changeTemplate(event.target.value as AgentTemplate)}>
                  {templates.map((template) => <option key={template} value={template}>{t(templatePresentation[template].name)}</option>)}
                </select>
              </label>
              <label>
                {t('Agent name')}
                <input required maxLength={160} value={editor.name} onChange={(event) => setEditor({ ...editor, name: event.target.value })} />
              </label>
              <label>
                {t('Role description')}
                <textarea maxLength={4000} value={editor.role} onChange={(event) => setEditor({ ...editor, role: event.target.value })} />
              </label>
              <p>{t('This creates a revisioned Agent Core on the backend. Tools, memory, and consequential actions still follow workspace permissions.')}</p>
              <footer>
                <button type="button" onClick={() => setEditor(null)}>{t('Cancel')}</button>
                <button type="submit" disabled={saving || !editor.name.trim()}>{saving ? t('Creating…') : t('Create Agent')}</button>
              </footer>
            </form>
          </section>
        </div>
      )}
    </div>
  )
}
