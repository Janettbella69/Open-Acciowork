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
      <header className="conversation-entry-header">
        <a href="/" aria-label="Back to OPHIRA website"><Brand /></a>
        <a href="/" className="conversation-site-link">Website <span aria-hidden="true">↗</span></a>
      </header>

      <main className="conversation-entry-main">
        <div className="conversation-entry-mark" aria-hidden="true">
          <img src="/ophira-mark.svg" alt="" width="58" height="58" />
        </div>
        <p className="conversation-entry-kicker">Opportunity Lead · Global commerce</p>
        <h1>What opportunity should we explore?</h1>
        <p className="conversation-entry-intro">
          Start with a product, market, capability, or customer. Ophira will coordinate demand,
          supply, capital, and sales around one shared project truth.
        </p>

        <form className="conversation-composer" onSubmit={onSubmit}>
          <label htmlFor="workspace-conversation-brief">Ask Ophira</label>
          <textarea
            id="workspace-conversation-brief"
            value={value}
            onChange={(event) => onChange(event.target.value)}
            placeholder="Describe the opportunity, market, product, or question you want to investigate..."
            rows={4}
            disabled={busy}
            autoFocus
          />
          <div className="conversation-composer-footer">
            <span>Demand · Supply · Capital · Sales</span>
            <button type="submit" disabled={busy || !value.trim()}>
              {busy ? 'Starting…' : 'Start research'} <span aria-hidden="true">→</span>
            </button>
          </div>
        </form>

        {notice && <p className="conversation-notice" role="status">{notice}</p>}
        {error && <p className="conversation-error" role="alert">{error}</p>}

        <section className="conversation-quick-starts" aria-labelledby="quick-starts-title">
          <div className="conversation-section-heading">
            <h2 id="quick-starts-title">Or begin with a validation task</h2>
            <span>Four connected dimensions</span>
          </div>
          <div className="conversation-quick-grid">
            {quickStarts.map((item, index) => (
              <button key={item.dimension} type="button" onClick={() => onChange(item.prompt)}>
                <span>{String(index + 1).padStart(2, '0')} · {item.dimension}</span>
                <strong>{item.title}</strong>
                <i aria-hidden="true">↗</i>
              </button>
            ))}
          </div>
        </section>

        {projects.length > 0 && (
          <section className="conversation-recent" aria-labelledby="recent-projects-title">
            <div className="conversation-section-heading">
              <h2 id="recent-projects-title">Continue a recent project</h2>
              <span>{projects.length} saved</span>
            </div>
            <div>
              {projects.slice(0, 4).map((project) => (
                <button key={project.id} type="button" onClick={() => onSelectProject(project.id)}>
                  <span>{project.name}</span>
                  <small>{project.objective}</small>
                  <i aria-hidden="true">→</i>
                </button>
              ))}
            </div>
          </section>
        )}
      </main>

      <footer className="conversation-entry-footer">
        <span>OPHIRA AI</span>
        <span>Discover what the world needs. Connect who can supply it.</span>
      </footer>
    </div>
  )
}
