import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  errorMessage,
  installSkill,
  listSkills,
  listSkillVersions,
  rollbackSkill,
  setSkillEnabled,
  testSkill,
} from '../api/client'
import type { SkillTestResult, SkillVersion } from '../api/types'
import { useT } from '../i18n'
import SkillProposalsPanel from './SkillProposalsPanel'
import './skills-page.css'

function initials(name: string): string {
  return name.split('-').slice(0, 2).map((part) => part[0]?.toUpperCase()).join('')
}

export default function SkillsPage() {
  const t = useT()
  const [skills, setSkills] = useState<SkillVersion[]>([])
  const [query, setQuery] = useState('')
  const [error, setError] = useState('')
  const [updating, setUpdating] = useState('')
  const [testing, setTesting] = useState('')
  const [testResults, setTestResults] = useState<Record<string, SkillTestResult>>({})
  const [historyOpen, setHistoryOpen] = useState('')
  const [historyLoading, setHistoryLoading] = useState('')
  const [versions, setVersions] = useState<Record<string, SkillVersion[]>>({})
  const [rollingBack, setRollingBack] = useState('')
  const [view, setView] = useState<'installed' | 'proposals'>('installed')
  const [proposalsOpened, setProposalsOpened] = useState(false)
  const active = useRef(false)
  const listRevision = useRef(0)
  const registryEpoch = useRef(0)

  const refreshSkills = useCallback(async () => {
    const revision = ++listRevision.current
    try {
      const items = await listSkills()
      if (active.current && revision === listRevision.current) { setSkills(items); setError('') }
    } catch (cause) {
      if (active.current && revision === listRevision.current) setError(errorMessage(cause))
      throw cause
    }
  }, [])

  const published = useCallback(async () => {
    if (!active.current) return
    registryEpoch.current += 1
    setVersions({})
    setTestResults({})
    setHistoryOpen('')
    setHistoryLoading('')
    setUpdating('')
    setTesting('')
    setRollingBack('')
    await refreshSkills()
  }, [refreshSkills])

  useEffect(() => {
    active.current = true
    void refreshSkills().catch(() => undefined)
    return () => { active.current = false; listRevision.current += 1 }
  }, [refreshSkills])

  const visibleSkills = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    if (!normalized) return skills
    return skills.filter((skill) => (
      `${skill.name} ${skill.description} ${skill.namespace}`.toLowerCase().includes(normalized)
    ))
  }, [query, skills])

  const toggleSkill = async (skill: SkillVersion) => {
    const epoch = registryEpoch.current
    const current = () => active.current && epoch === registryEpoch.current
    setUpdating(skill.id)
    setError('')
    try {
      const updated = await setSkillEnabled(skill.id, !skill.enabled)
      if (current()) setSkills((items) => items.map((item) => item.id === updated.id ? updated : item))
    } catch (toggleError) {
      if (current()) setError(errorMessage(toggleError))
    } finally {
      if (current()) setUpdating('')
    }
  }

  const runSkillTest = async (skill: SkillVersion) => {
    const epoch = registryEpoch.current
    const current = () => active.current && epoch === registryEpoch.current
    setTesting(skill.id)
    setError('')
    try {
      const result = await testSkill(skill.id)
      if (current()) setTestResults((items) => ({ ...items, [skill.id]: result }))
    } catch (testError) {
      if (current()) setError(errorMessage(testError))
    } finally {
      if (current()) setTesting('')
    }
  }

  const loadHistory = async (skill: SkillVersion) => {
    if (historyOpen === skill.id) {
      setHistoryOpen('')
      return
    }
    const epoch = registryEpoch.current
    const current = () => active.current && epoch === registryEpoch.current
    setHistoryOpen(skill.id)
    setHistoryLoading(skill.id)
    setError('')
    try {
      const items = await listSkillVersions(skill.id)
      if (current()) setVersions((previous) => ({ ...previous, [skill.id]: items }))
    } catch (historyError) {
      if (current()) { setError(errorMessage(historyError)); setHistoryOpen('') }
    } finally {
      if (current()) setHistoryLoading('')
    }
  }

  const restoreVersion = async (skill: SkillVersion, contentHash: string) => {
    const epoch = registryEpoch.current
    const current = () => active.current && epoch === registryEpoch.current
    setRollingBack(contentHash)
    setError('')
    try {
      const updated = await rollbackSkill(skill.id, { content_hash: contentHash })
      if (!current()) return
      setSkills((items) => items.map((item) => item.id === updated.id ? updated : item))
      const items = await listSkillVersions(skill.id)
      if (current()) setVersions((previous) => ({ ...previous, [skill.id]: items }))
    } catch (rollbackError) {
      if (current()) setError(errorMessage(rollbackError))
    } finally {
      if (current()) setRollingBack('')
    }
  }

  const featured = skills.find((skill) => skill.name === 'global-opportunity-execution')

  return (
    <section className="skills-content" aria-labelledby="skills-page-heading">
        <header className="skills-header">
          <div className="skills-title-group"><p>{t('Runtime capability registry')}</p><h1 id="skills-page-heading">{t('Skills')}</h1></div>
          <div className="skills-view-switch" role="group" aria-label={t('Skill view')}>
            <button type="button" data-active={view === 'installed'} aria-pressed={view === 'installed'} onClick={() => {
              setView('installed'); void refreshSkills().catch(() => undefined)
            }}>{t('Installed')} <span>{skills.length}</span></button>
            <button type="button" data-active={view === 'proposals'} aria-pressed={view === 'proposals'} onClick={() => {
              setProposalsOpened(true); setView('proposals')
            }}>{t('Proposals')}</button>
          </div>
          {view === 'installed' && <div className="skills-actions">
            <label className="skills-search">
              <span aria-hidden="true">⌕</span>
              <input type="search" aria-label={t('Search skills')} value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t('Search skills')} />
            </label>
            <label className="skills-upload">
              <span>{t('Upload skill zip')}</span>
              <input
                type="file"
                accept=".zip,application/zip"
                aria-label={t('Upload skill zip')}
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  event.target.value = ''
                  if (!file) return
                  const epoch = registryEpoch.current
                  const current = () => active.current && epoch === registryEpoch.current
                  setError('')
                  void installSkill(file)
                    .then((uploaded) => {
                      if (!current()) return
                      setSkills((current) => [uploaded, ...current.filter((item) => item.id !== uploaded.id)])
                      setVersions((current) => {
                        const next = { ...current }
                        delete next[uploaded.id]
                        return next
                      })
                    })
                    .catch((uploadError) => { if (current()) setError(errorMessage(uploadError)) })
                }}
              />
            </label>
          </div>}
        </header>

        {error && <p className="conversation-error" role="alert">{t(error)}</p>}

        {proposalsOpened && <div hidden={view !== 'proposals'}><SkillProposalsPanel onPublished={published} /></div>}
        {view === 'installed' && <>

        {featured && !query && (
          <section className="skills-feature" aria-labelledby="featured-skill-heading">
            <div>
              <span>{t('Featured Plugin Skill')} · v{featured.version}</span>
              <h2 id="featured-skill-heading">{featured.name.replaceAll('-', ' ')}</h2>
              <p>{featured.description}</p>
              <a href="/app">{t('Start a task')} <span aria-hidden="true">→</span></a>
            </div>
            <dl>
              <div><dt>{t('Runtime name')}</dt><dd>{featured.id}</dd></div>
              <div><dt>{t('Content hash')}</dt><dd>{featured.content_hash.slice(0, 12)}</dd></div>
              <div><dt>{t('Status')}</dt><dd>{featured.enabled ? t('Enabled') : t('Disabled')}</dd></div>
            </dl>
          </section>
        )}

        <section className="skills-catalog" aria-labelledby="skills-catalog-heading">
          <div className="skills-catalog-heading">
            <div><p>{t('Application Plugin')}</p><h2 id="skills-catalog-heading">{t('Installed TradeMind skills')}</h2></div>
            <span>{t('{n} skills', { n: visibleSkills.length })}</span>
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
                      <span className="skill-card-buttons">
                        {skill.source === 'tenant' && (
                          <button
                            type="button"
                            aria-expanded={historyOpen === skill.id}
                            aria-controls={`skill-history-${skill.id.replace(/[^A-Za-z0-9_-]/g, '-')}`}
                            disabled={historyLoading === skill.id}
                            onClick={() => void loadHistory(skill)}
                          >
                            {historyLoading === skill.id ? t('Loading…') : t('Versions')}
                          </button>
                        )}
                        <button type="button" disabled={testing === skill.id} onClick={() => void runSkillTest(skill)}>
                          {testing === skill.id ? t('Testing…') : t('Test')}
                        </button>
                        <button type="button" disabled={updating === skill.id} onClick={() => void toggleSkill(skill)}>
                          {updating === skill.id ? t('Saving…') : skill.enabled ? t('Disable') : t('Enable')}
                        </button>
                      </span>
                    </footer>
                    {testResults[skill.id] && (
                      <div className="skill-test-result" data-ok={testResults[skill.id].ok} role="status">
                        <strong>{testResults[skill.id].ok ? `✓ ${t('Package test passed')}` : `✗ ${t('Package test failed')}`}</strong>
                        <ul>
                          {testResults[skill.id].checks.map((check) => (
                            <li key={check.name} data-status={check.status}>
                              <span>{check.status === 'passed' ? '✓' : check.status === 'warning' ? '⚠' : '✗'}</span>
                              <b>{check.name}</b> {check.detail}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                    {historyOpen === skill.id && versions[skill.id] && (
                      <section
                        id={`skill-history-${skill.id.replace(/[^A-Za-z0-9_-]/g, '-')}`}
                        className="skill-version-history"
                        aria-label={t('Version history')}
                      >
                        <header>
                          <strong>{t('Version history')}</strong>
                          <span>{t('{n} immutable versions', { n: versions[skill.id].length })}</span>
                        </header>
                        <ol>
                          {versions[skill.id].map((item) => {
                            const current = item.content_hash === skill.content_hash
                            return (
                              <li key={item.content_hash} data-current={current}>
                                <span>
                                  <b>v{item.version}</b>
                                  <code>{item.content_hash.slice(0, 12)}</code>
                                </span>
                                {current ? (
                                  <em>{t('Current version')}</em>
                                ) : (
                                  <button
                                    type="button"
                                    disabled={Boolean(rollingBack)}
                                    onClick={() => void restoreVersion(skill, item.content_hash)}
                                  >
                                    {rollingBack === item.content_hash ? t('Restoring…') : t('Restore')}
                                  </button>
                                )}
                              </li>
                            )
                          })}
                        </ol>
                        <p>{t('Restored packages are reviewed again; current enablement is preserved.')}</p>
                      </section>
                    )}
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <div className="skills-empty">
              <strong>{query ? t('No skills match this search.') : t('No Plugin Skills discovered.')}</strong>
              <p>{query ? t('Try another capability name.') : t('Check the backend capability Plugin path and manifest.')}</p>
            </div>
          )}
        </section>
        </>}
    </section>
  )
}
