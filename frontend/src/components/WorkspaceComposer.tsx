import type { FormEvent } from 'react'
import Brand from './Brand'
import './workspace-composer.css'

type SavedProject = {
  id: string
  name: string
  objective: string
}

type WorkspaceComposerProps = {
  value: string
  busy: boolean
  notice: string
  error: string
  projects: SavedProject[]
  onChange: (value: string) => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void
  onSelectProject: (projectId: string) => void
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

export default function WorkspaceComposer({
  value,
  busy,
  notice,
  error,
  projects,
  onChange,
  onSubmit,
  onSelectProject,
}: WorkspaceComposerProps) {
  return (
    <div className="conversation-entry">
      <aside className="conversation-sidebar">
        <a className="conversation-sidebar-brand" href="/" aria-label="Back to OPHIRA website"><Brand /></a>

        <button className="conversation-new-research" type="button" onClick={() => onChange('')}>
          <span aria-hidden="true">＋</span> New research
        </button>

        <nav className="conversation-sidebar-nav" aria-label="Workspace navigation">
          <p>Workspace</p>
          <a href="/app" aria-current="page"><span>01</span>Opportunity studio</a>
          <a href="/"><span>02</span>Marketing site</a>
          <a href="/app/settings"><span>03</span>Settings</a>
        </nav>

        <section className="conversation-sidebar-projects" aria-labelledby="sidebar-projects-title">
          <div><h2 id="sidebar-projects-title">Recent projects</h2><span>{projects.length}</span></div>
          {projects.length > 0 ? projects.slice(0, 6).map((project) => (
            <button key={project.id} type="button" onClick={() => onSelectProject(project.id)}>
              <span>{project.name}</span>
              <small>{project.objective}</small>
            </button>
          )) : <p>Your saved opportunity studies will appear here.</p>}
        </section>

        <section className="conversation-sidebar-agents" aria-labelledby="sidebar-agents-title">
          <h2 id="sidebar-agents-title">Agent team</h2>
          <ul>
            {['Demand', 'Supply', 'Capital', 'Sales'].map((dimension) => <li key={dimension}><i />{dimension}</li>)}
          </ul>
        </section>

        <div className="conversation-sidebar-profile">
          <span>OP</span>
          <div><strong>Ophira workspace</strong><small>Opportunity team ready</small></div>
        </div>
      </aside>

      <main className="conversation-entry-main">
        <a href="/" className="conversation-site-link">Website <span aria-hidden="true">↗</span></a>

        <div className="conversation-entry-center">
          <div className="conversation-entry-mark" aria-hidden="true">
            <img src="/ophira-mark.svg" alt="" width="48" height="48" />
          </div>
          <p className="conversation-entry-kicker">Opportunity Lead</p>
          <h1>What opportunity should we explore?</h1>

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

        <div className="conversation-composer-dock">
          <div className="conversation-context-bar">
            <span>New opportunity</span>
            <small>One shared project truth</small>
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
              autoFocus
            />
            <div className="conversation-composer-footer">
              <span><b>Opportunity Lead</b> coordinates Demand · Supply · Capital · Sales</span>
              <button type="submit" disabled={busy || !value.trim()}>
                {busy ? 'Starting…' : 'Start research'} <span aria-hidden="true">→</span>
              </button>
            </div>
          </form>
          {notice && <p className="conversation-notice" role="status">{notice}</p>}
          {error && <p className="conversation-error" role="alert">{error}</p>}
        </div>
      </main>
    </div>
  )
}
