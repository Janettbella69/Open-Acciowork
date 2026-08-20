import { useEffect, useMemo, useState } from 'react'
import { errorMessage, listSkills, setSkillEnabled } from '../api/client'
import type { SkillVersion } from '../api/types'
import Brand from './Brand'
import './workspace-composer.css'
import './skills-page.css'

function initials(name: string): string {
  return name.split('-').slice(0, 2).map((part) => part[0]?.toUpperCase()).join('')
}

export default function SkillsPage() {
  const [skills, setSkills] = useState<SkillVersion[]>([])
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  const [updating, setUpdating] = useState('')

  useEffect(() => {
    let active = true
    void listSkills()
      .then((items) => { if (active) setSkills(items) })
      .catch((loadError) => { if (active) setError(errorMessage(loadError)) })
    return () => { active = false }
  }, [])

  const visibleSkills = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    if (!normalized) return skills
    return skills.filter((skill) => (
      `${skill.name} ${skill.description} ${skill.namespace}`.toLowerCase().includes(normalized)
    ))
  }, [query, skills])

  const toggleSkill = async (skill: SkillVersion) => {
    setUpdating(skill.id)
    setError('')
    try {
      const updated = await setSkillEnabled(skill.id, !skill.enabled)
      setSkills((current) => current.map((item) => item.id === updated.id ? updated : item))
    } catch (toggleError) {
      setError(errorMessage(toggleError))
    } finally {
      setUpdating('')
    }
  }

  const featured = skills.find((skill) => skill.name === 'global-opportunity-execution')

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
          <strong>Real Plugin Skills</strong>
          <p>Every item below is a versioned SKILL.md discovered by Claude Agent SDK.</p>
        </div>
      </aside>

      <main className="skills-main">
        <header className="skills-header">
          <div className="skills-title-group"><p>Runtime capability registry</p><h1>Skills</h1></div>
          <div className="skills-view-switch" role="group" aria-label="Skill view">
            <button type="button" data-active="true">Installed <span>{skills.length}</span></button>
          </div>
          <div className="skills-actions">
            <label className="skills-search">
              <span aria-hidden="true">⌕</span>
              <input type="search" aria-label="Search skills" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search skills" />
            </label>
          </div>
        </header>

        {error && <p className="conversation-error" role="alert">{error}</p>}

        {featured && !query && (
          <section className="skills-feature" aria-labelledby="featured-skill-heading">
            <div>
              <span>Featured Plugin Skill · v{featured.version}</span>
              <h2 id="featured-skill-heading">Global Opportunity Execution</h2>
              <p>{featured.description}</p>
              <a href="/app">Start a task <span aria-hidden="true">→</span></a>
            </div>
            <dl>
              <div><dt>Runtime name</dt><dd>{featured.id}</dd></div>
              <div><dt>Content hash</dt><dd>{featured.content_hash.slice(0, 12)}</dd></div>
              <div><dt>Status</dt><dd>{featured.enabled ? 'Enabled' : 'Disabled'}</dd></div>
            </dl>
          </section>
        )}

        <section className="skills-catalog" aria-labelledby="skills-catalog-heading">
          <div className="skills-catalog-heading">
            <div><p>Application Plugin</p><h2 id="skills-catalog-heading">Installed OPHIRA skills</h2></div>
            <span>{visibleSkills.length} {visibleSkills.length === 1 ? 'skill' : 'skills'}</span>
          </div>

          {visibleSkills.length ? (
            <div className="skills-grid">
              {visibleSkills.map((skill) => (
                <article key={skill.id} className="skill-card">
                  <div className="skill-card-visual" data-category={skill.enabled ? 'research' : 'disabled'}>
                    <span>{initials(skill.name)}</span><small>{skill.namespace}</small>
                  </div>
                  <div className="skill-card-copy">
                    <h3>{skill.name.replaceAll('-', ' ')}</h3>
                    <p>{skill.description}</p>
                    <footer>
                      <span>v{skill.version} · {skill.content_hash.slice(0, 8)}</span>
                      <button type="button" disabled={updating === skill.id} onClick={() => void toggleSkill(skill)}>
                        {updating === skill.id ? 'Saving…' : skill.enabled ? 'Disable' : 'Enable'}
                      </button>
                    </footer>
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="skills-empty">
              <strong>{query ? 'No skills match this search.' : 'No Plugin Skills discovered.'}</strong>
              <p>{query ? 'Try another capability name.' : 'Check the backend capability Plugin path and manifest.'}</p>
            </div>
          )}
        </section>
      </main>
    </div>
  )
}
