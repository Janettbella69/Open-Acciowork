import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { builtInSkills, type SkillTemplate } from '../data/skillCatalog'
import Brand from './Brand'
import './workspace-composer.css'
import './skills-page.css'

const storageKey = 'ophira-custom-skill-drafts'

function loadCustomSkills(): SkillTemplate[] {
  try {
    return JSON.parse(window.localStorage.getItem(storageKey) ?? '[]') as SkillTemplate[]
  } catch {
    return []
  }
}

function saveCustomSkills(skills: SkillTemplate[]) {
  try {
    window.localStorage?.setItem(storageKey, JSON.stringify(skills))
  } catch {
    // Keep the studio usable when storage is blocked by the browser.
  }
}

export default function SkillsPage() {
  const [view, setView] = useState<'explore' | 'yours'>('explore')
  const [query, setQuery] = useState('')
  const [customSkills, setCustomSkills] = useState<SkillTemplate[]>(loadCustomSkills)
  const [creatorOpen, setCreatorOpen] = useState(false)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')

  useEffect(() => {
    saveCustomSkills(customSkills)
  }, [customSkills])

  useEffect(() => {
    if (!creatorOpen) return
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setCreatorOpen(false)
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [creatorOpen])

  const visibleSkills = useMemo(() => {
    const source = view === 'explore' ? builtInSkills : customSkills
    const normalizedQuery = query.trim().toLowerCase()
    if (!normalizedQuery) return source
    return source.filter((skill) => `${skill.name} ${skill.description} ${skill.category}`.toLowerCase().includes(normalizedQuery))
  }, [customSkills, query, view])

  const openCreator = () => {
    setName('')
    setDescription('')
    setCreatorOpen(true)
  }

  const createSkill = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!name.trim() || !description.trim()) return
    const words = name.trim().split(/\s+/)
    setCustomSkills((current) => [{
      id: `skill_${Date.now()}`,
      code: words.slice(0, 2).map((word) => word[0]).join('').toUpperCase(),
      name: name.trim(),
      description: description.trim(),
      prompt: description.trim(),
      category: 'Custom',
      source: 'Local draft',
    }, ...current])
    setCreatorOpen(false)
    setView('yours')
    setQuery('')
  }

  return (
    <div className="skills-shell">
      <aside className="conversation-sidebar skills-sidebar">
        <a className="conversation-sidebar-brand" href="/" aria-label="Back to OPHIRA website"><Brand /></a>
        <a className="conversation-new-research" href="/app"><span aria-hidden="true">＋</span> New conversation</a>
        <nav className="conversation-sidebar-nav" aria-label="Workspace navigation">
          <p>Workspace</p>
          <a href="/app"><span>01</span>Agent workspace</a>
          <a href="/app/schedules"><span>02</span>Scheduled tasks</a>
          <a href="/app/skills" aria-current="page"><span>03</span>Skill Studio</a>
        </nav>
        <div className="skills-sidebar-note">
          <strong>Reusable operating knowledge</strong>
          <p>Turn repeatable research methods into clear, reviewable skill drafts.</p>
        </div>
      </aside>

      <main className="skills-main">
        <header className="skills-header">
          <div className="skills-title-group">
            <p>OPHIRA capabilities</p>
            <h1>Skills</h1>
          </div>
          <div className="skills-view-switch" role="group" aria-label="Skill view">
            <button type="button" data-active={view === 'explore'} onClick={() => setView('explore')}>Explore</button>
            <button type="button" data-active={view === 'yours'} onClick={() => setView('yours')}>Yours <span>{customSkills.length}</span></button>
          </div>
          <div className="skills-actions">
            <label className="skills-search">
              <span aria-hidden="true">⌕</span>
              <input type="search" aria-label="Search skills" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search skills" />
            </label>
            <button type="button" onClick={openCreator}><span aria-hidden="true">＋</span> New skill</button>
          </div>
        </header>

        {view === 'explore' && !query && (
          <section className="skills-feature" aria-labelledby="featured-skill-heading">
            <div>
              <span>Featured operating skill</span>
              <h2 id="featured-skill-heading">Opportunity Validation Brief</h2>
              <p>Use several commercial perspectives when they help, without forcing every request through the same sequence.</p>
              <a href="/app">Start with this workflow <span aria-hidden="true">→</span></a>
            </div>
            <dl>
              <div><dt>Inputs</dt><dd>Market question · constraints · target</dd></div>
              <div><dt>Output</dt><dd>Evidence gaps · stop conditions · next test</dd></div>
              <div><dt>Owner</dt><dd>Ophira</dd></div>
            </dl>
          </section>
        )}

        <section className="skills-catalog" aria-labelledby="skills-catalog-heading">
          <div className="skills-catalog-heading">
            <div><p>{view === 'explore' ? 'Built-in library' : 'Local workspace'}</p><h2 id="skills-catalog-heading">{view === 'explore' ? 'Explore OPHIRA skills' : 'Your skill drafts'}</h2></div>
            <span>{visibleSkills.length} {visibleSkills.length === 1 ? 'skill' : 'skills'}</span>
          </div>

          {visibleSkills.length ? (
            <div className="skills-grid">
              {visibleSkills.map((skill) => (
                <article key={skill.id} className="skill-card">
                  <div className="skill-card-visual" data-category={skill.category.toLowerCase()}>
                    <span>{skill.code}</span>
                    <small>{skill.category}</small>
                  </div>
                  <div className="skill-card-copy">
                    <h3>{skill.name}</h3>
                    <p>{skill.description}</p>
                    <footer><span>{skill.source}</span><a href={`/app?brief=${encodeURIComponent(`Use the ${skill.name} skill: ${skill.description}`)}`}>Use skill <i aria-hidden="true">→</i></a></footer>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="skills-empty">
              <strong>{query ? 'No skills match this search.' : 'No skill drafts yet.'}</strong>
              <p>{query ? 'Try a capability, category, or outcome.' : 'Create a local draft from a repeatable workflow your team already uses.'}</p>
              {!query && <button type="button" onClick={openCreator}>Create your first skill</button>}
            </div>
          )}
        </section>
      </main>

      {creatorOpen && (
        <div className="skill-creator-backdrop" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setCreatorOpen(false)
        }}>
          <section className="skill-creator" role="dialog" aria-modal="true" aria-labelledby="skill-creator-heading">
            <header><div><span>Local skill draft</span><h2 id="skill-creator-heading">Create a skill</h2></div><button type="button" aria-label="Close skill creator" onClick={() => setCreatorOpen(false)}>×</button></header>
            <form onSubmit={createSkill}>
              <label>Skill name<input value={name} onChange={(event) => setName(event.target.value)} placeholder="Channel Readiness Review" required /></label>
              <label>Instructions<textarea value={description} onChange={(event) => setDescription(event.target.value)} placeholder="Describe the repeatable outcome, inputs, and boundaries..." rows={6} required /></label>
              <p>This saves a local draft only. Publishing, sharing, and remote execution require account and registry services.</p>
              <footer><button type="button" onClick={() => setCreatorOpen(false)}>Cancel</button><button type="submit">Save skill <span aria-hidden="true">→</span></button></footer>
            </form>
          </section>
        </div>
      )}
    </div>
  )
}
