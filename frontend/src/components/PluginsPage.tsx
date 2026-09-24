import { useEffect, useMemo, useState } from 'react'
import { errorMessage, listSkills } from '../api/client'
import type { SkillVersion } from '../api/types'
import { useT } from '../i18n'
import Brand from './Brand'
import ConnectorsPage from './ConnectorsPage'
import PluginImportsPanel from './PluginImportsPanel'
import SkillsPage from './SkillsPage'
import WorkspaceNav from './WorkspaceNav'
import './workspace-composer.css'
import './plugins-page.css'

type PluginTab = 'plugins' | 'skills' | 'connectors'

type PluginsPageProps = {
  legacyTab?: Exclude<PluginTab, 'plugins'>
}

type PluginSummary = {
  namespace: string
  source: SkillVersion['source']
  skills: SkillVersion[]
}

function currentTab(pathname: string): PluginTab {
  if (pathname.startsWith('/app/plugins/skills')) return 'skills'
  if (pathname.startsWith('/app/plugins/connectors')) return 'connectors'
  return 'plugins'
}

function displayName(namespace: string): string {
  return namespace
    .split('-')
    .filter(Boolean)
    .map((part) => `${part[0]?.toUpperCase() ?? ''}${part.slice(1)}`)
    .join(' ')
}

function PluginsOverview() {
  const t = useT()
  const [skills, setSkills] = useState<SkillVersion[]>([])
  const [query, setQuery] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    void listSkills()
      .then((items) => { if (active) setSkills(items) })
      .catch((loadError) => { if (active) setError(errorMessage(loadError)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  const plugins = useMemo(() => {
    const grouped = new Map<string, PluginSummary>()
    for (const skill of skills) {
      const plugin = grouped.get(skill.namespace) ?? {
        namespace: skill.namespace,
        source: skill.source,
        skills: [],
      }
      plugin.skills.push(skill)
      grouped.set(skill.namespace, plugin)
    }
    const normalized = query.trim().toLowerCase()
    return Array.from(grouped.values())
      .filter((plugin) => !normalized || `${plugin.namespace} ${plugin.skills.map((skill) => `${skill.name} ${skill.description}`).join(' ')}`.toLowerCase().includes(normalized))
      .sort((left, right) => left.namespace.localeCompare(right.namespace))
  }, [query, skills])

  return (
    <section className="plugins-overview" aria-labelledby="plugins-page-heading">
      <header className="plugins-overview-header">
        <div>
          <p>{t('Runtime extension registry')}</p>
          <h1 id="plugins-page-heading">{t('Plugins')}</h1>
          <span>{t('Plugins package related Skills and runtime extensions under one auditable namespace.')}</span>
        </div>
        <label className="plugins-search">
          <span aria-hidden="true">⌕</span>
          <input type="search" aria-label={t('Search plugins')} placeholder={t('Search plugins')} value={query} onChange={(event) => setQuery(event.target.value)} />
        </label>
      </header>

      {error && <p className="plugins-error" role="alert">{error}</p>}

      <section className="plugins-registry" aria-labelledby="installed-plugins-heading">
        <header>
          <div><span>{t('Backend managed')}</span><h2 id="installed-plugins-heading">{t('Installed plugins')}</h2></div>
          <p>{t('{n} plugins', { n: plugins.length })}</p>
        </header>

        {loading ? (
          <div className="plugins-empty"><strong>{t('Loading plugins…')}</strong></div>
        ) : plugins.length ? (
          <div className="plugins-grid">
            {plugins.map((plugin) => {
              const enabled = plugin.skills.filter((skill) => skill.enabled).length
              return (
                <article key={plugin.namespace}>
                  <header>
                    <span className="plugin-mark" aria-hidden="true">PL</span>
                    <div><h3>{displayName(plugin.namespace)}</h3><p>{plugin.namespace}</p></div>
                    <span>{t(plugin.source === 'application' ? 'Application' : 'Tenant')}</span>
                  </header>
                  <p>{plugin.skills.map((skill) => skill.description).slice(0, 2).join(' ')}</p>
                  <div className="plugin-skill-tags">
                    {plugin.skills.slice(0, 4).map((skill) => <span key={skill.id}>{skill.name.replaceAll('-', ' ')}</span>)}
                  </div>
                  <footer>
                    <span>{t('{n} skills', { n: plugin.skills.length })} · {t('{n} enabled', { n: enabled })}</span>
                    <a href="/app/plugins/skills">{t('View skills')} <i aria-hidden="true">→</i></a>
                  </footer>
                </article>
              )
            })}
          </div>
        ) : (
          <div className="plugins-empty">
            <strong>{query ? t('No plugins match this search.') : t('No runtime plugins discovered.')}</strong>
            <p>{query ? t('Try another plugin or capability name.') : t('Plugins appear here after an operator installs them on the backend runtime.')}</p>
          </div>
        )}
      </section>
      <PluginImportsPanel />
    </section>
  )
}

export default function PluginsPage({ legacyTab }: PluginsPageProps) {
  const t = useT()
  const tab = legacyTab ?? currentTab(window.location.pathname)
  const tabs: Array<{ id: PluginTab; href: string; label: string }> = [
    { id: 'plugins', href: '/app/plugins', label: 'Plugins' },
    { id: 'skills', href: '/app/plugins/skills', label: 'Skills' },
    { id: 'connectors', href: '/app/plugins/connectors', label: 'Connectors' },
  ]

  return (
    <div className="plugins-hub-shell">
      <aside className="conversation-sidebar plugins-hub-sidebar">
        <a className="conversation-sidebar-brand" href="/" aria-label={t('Back to TradeMind website')}><Brand /></a>
        <a className="conversation-new-research" href="/app"><span aria-hidden="true">＋</span> {t('New task')}</a>
        <WorkspaceNav active="plugins" />
        <div className="plugins-hub-sidebar-note">
          <strong>{t('Runtime extensions')}</strong>
          <p>{t('Plugins, Skills, and Connectors share the same workspace permission and audit boundary.')}</p>
        </div>
      </aside>

      <main className="plugins-hub-main">
        <nav className="plugins-hub-tabs" aria-label={t('Plugin sections')}>
          {tabs.map((item) => (
            <a key={item.id} href={item.href} aria-current={tab === item.id ? 'page' : undefined}>{t(item.label)}</a>
          ))}
        </nav>
        {tab === 'plugins' && <PluginsOverview />}
        {tab === 'skills' && <SkillsPage />}
        {tab === 'connectors' && <ConnectorsPage />}
      </main>
    </div>
  )
}
