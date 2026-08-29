import { useEffect, useState } from 'react'
import Brand from './Brand'
import MemoryReview from './MemoryReview'
import ProviderSettings from './ProviderSettings'
import './settings-page.css'
import { languageChangedEvent, useT } from '../i18n'
import { loadLocalSettings, saveLocalSettings, type LocalSettings } from '../localSettings'

function ToggleRow({
  title,
  description,
  checked,
  warning,
  onChange,
}: {
  title: string
  description: string
  checked: boolean
  warning?: boolean
  onChange: (checked: boolean) => void
}) {
  const t = useT()
  return (
    <label className="settings-row settings-toggle-row">
      <span><strong>{t(title)}</strong><small>{t(description)}</small>{warning && <i>{t('High-risk setting')}</i>}</span>
      <input type="checkbox" role="switch" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    </label>
  )
}

export default function SettingsPage() {
  const t = useT()
  const [settings, setSettings] = useState<LocalSettings>(loadLocalSettings)
  const [activeSection, setActiveSection] = useState('providers')

  useEffect(() => {
    saveLocalSettings(settings)
    // 语言改动即时生效（同页广播；跨标签页由 storage 事件覆盖）。
    window.dispatchEvent(new Event(languageChangedEvent))
  }, [settings])

  const update = <Key extends keyof LocalSettings>(key: Key, value: LocalSettings[Key]) => {
    setSettings((current) => ({ ...current, [key]: value }))
  }

  return (
    <div className="settings-shell">
      <aside className="settings-sidebar">
        <a className="settings-back" href="/app"><span aria-hidden="true">←</span> {t('Back to workspace')}</a>
        <a className="settings-brand" href="/" aria-label={t('TradeMind website')}><Brand /></a>
        <nav aria-label={t('Settings sections')}>
          <p>{t('Runtime')}</p>
          <a href="#providers" aria-current={activeSection === 'providers' ? 'page' : undefined} onClick={() => setActiveSection('providers')}><span>01</span>{t('Model providers')}</a>
          <a href="#memory-review" aria-current={activeSection === 'memory-review' ? 'page' : undefined} onClick={() => setActiveSection('memory-review')}><span>02</span>{t('Memory review')}</a>
          <p>{t('Workspace settings')}</p>
          <a href="#permissions" aria-current={activeSection === 'permissions' ? 'page' : undefined} onClick={() => setActiveSection('permissions')}><span>03</span>{t('Permissions')}</a>
          <a href="#general" aria-current={activeSection === 'general' ? 'page' : undefined} onClick={() => setActiveSection('general')}><span>04</span>{t('General')}</a>
          <a href="#runtime" aria-current={activeSection === 'runtime' ? 'page' : undefined} onClick={() => setActiveSection('runtime')}><span>05</span>{t('Runtime')}</a>
          <a href="#language" aria-current={activeSection === 'language' ? 'page' : undefined} onClick={() => setActiveSection('language')}><span>06</span>{t('Language')}</a>
          <a href="#memory" aria-current={activeSection === 'memory' ? 'page' : undefined} onClick={() => setActiveSection('memory')}><span>07</span>{t('Memory')}</a>
          <p>{t('Integrations')}</p>
          <a href="#plugins" aria-current={activeSection === 'plugins' ? 'page' : undefined} onClick={() => setActiveSection('plugins')}><span>08</span>{t('Plugins')}</a>
          <a href="#browser" aria-current={activeSection === 'browser' ? 'page' : undefined} onClick={() => setActiveSection('browser')}><span>09</span>{t('Browser')}</a>
          <a href="#computer-control" aria-current={activeSection === 'computer-control' ? 'page' : undefined} onClick={() => setActiveSection('computer-control')}><span>10</span>{t('Computer control')}</a>
        </nav>
        <div className="settings-sidebar-note">
          <span>{t('Saved locally')}</span>
          <p>{t('These controls apply to this browser until account-level settings are connected.')}</p>
        </div>
      </aside>

      <main className="settings-main">
        <header>
          <p>{t('TradeMind workspace')}</p>
          <h1>{t('General settings')}</h1>
          <span>{t('Configure how the opportunity team accesses context and asks for approval.')}</span>
        </header>

        <section id="providers" className="settings-section" aria-labelledby="providers-heading">
          <div className="settings-section-heading"><span>01</span><div><h2 id="providers-heading">{t('Model providers')}</h2><p>{t('Anthropic-compatible endpoints the Agent runs on. Keys are stored in the backend vault and never shown again.')}</p></div></div>
          <ProviderSettings />
        </section>

        <section id="memory-review" className="settings-section" aria-labelledby="memory-review-heading">
          <div className="settings-section-heading"><span>02</span><div><h2 id="memory-review-heading">{t('Memory review')}</h2><p>{t('Agent memory proposals take effect only after your explicit approval.')}</p></div></div>
          <MemoryReview />
        </section>

        <section id="permissions" className="settings-section" aria-labelledby="permissions-heading">
          <div className="settings-section-heading"><span>03</span><div><h2 id="permissions-heading">{t('Permissions')}</h2><p>{t('Choose what the Agent team can access without interrupting your work.')}</p></div></div>
          <div className="settings-card">
            <ToggleRow
              title="Workspace access"
              description="Allow TradeMind to read and update files inside the selected project workspace. Access outside the workspace still requires approval."
              checked={settings.workspaceAccess}
              onChange={(checked) => update('workspaceAccess', checked)}
            />
            <ToggleRow
              title="Auto-review low-risk requests"
              description="Automatically approve read-only and reversible requests. Network access and external side effects still require review."
              checked={settings.autoReview}
              onChange={(checked) => update('autoReview', checked)}
            />
            <ToggleRow
              title="Full access"
              description="Allow file edits and network commands without per-action approval. Enable only for a workspace you trust."
              checked={settings.fullAccess}
              warning
              onChange={(checked) => update('fullAccess', checked)}
            />
          </div>
        </section>

        <section id="general" className="settings-section" aria-labelledby="general-heading">
          <div className="settings-section-heading"><span>04</span><div><h2 id="general-heading">{t('General')}</h2><p>{t('Set the default behavior for new opportunity studies.')}</p></div></div>
          <div className="settings-card">
            <label className="settings-row settings-select-row">
              <span><strong>{t('Default approval mode')}</strong><small>{t('How TradeMind should handle tool and external-action requests.')}</small></span>
              <select value={settings.approvalMode} onChange={(event) => update('approvalMode', event.target.value as LocalSettings['approvalMode'])}>
                <option value="review_external">{t('Review external actions')}</option>
                <option value="review_all">{t('Review every action')}</option>
                <option value="allow_low_risk">{t('Allow low-risk actions')}</option>
              </select>
            </label>
            <ToggleRow
              title="Remember recent projects"
              description="Show saved opportunity studies in the workspace sidebar on this device."
              checked={settings.rememberProjects}
              onChange={(checked) => update('rememberProjects', checked)}
            />
          </div>
        </section>

        <section id="runtime" className="settings-section" aria-labelledby="runtime-heading">
          <div className="settings-section-heading"><span>05</span><div><h2 id="runtime-heading">{t('Runtime')}</h2><p>{t('Control long-running research and approval waits.')}</p></div></div>
          <div className="settings-card">
            <ToggleRow
              title="Keep this device awake during active runs"
              description="Prevent system sleep while an Agent run is streaming."
              checked={settings.keepAwake}
              onChange={(checked) => update('keepAwake', checked)}
            />
            <label className="settings-row settings-select-row">
              <span><strong>{t('Permission request timeout')}</strong><small>{t('How long a run waits for your decision before denying the request.')}</small></span>
              <select value={settings.permissionTimeout} onChange={(event) => update('permissionTimeout', event.target.value as LocalSettings['permissionTimeout'])}>
                <option value="120">{t('2 minutes')}</option>
                <option value="300">{t('5 minutes')}</option>
                <option value="600">{t('10 minutes')}</option>
              </select>
            </label>
          </div>
        </section>

        <section id="language" className="settings-section" aria-labelledby="language-heading">
          <div className="settings-section-heading"><span>06</span><div><h2 id="language-heading">{t('Language')}</h2><p>{t('Choose the interface language for this browser.')}</p></div></div>
          <div className="settings-card">
            <label className="settings-row settings-select-row">
              <span><strong>{t('Interface language')}</strong><small>{t('Research evidence keeps its original source language.')}</small></span>
              <select value={settings.language} onChange={(event) => update('language', event.target.value as LocalSettings['language'])}>
                <option value="auto">{t('Auto-detect')}</option>
                <option value="en">English</option>
                <option value="zh-CN">简体中文</option>
              </select>
            </label>
          </div>
        </section>

        <section id="memory" className="settings-section" aria-labelledby="memory-heading">
          <div className="settings-section-heading"><span>07</span><div><h2 id="memory-heading">{t('Memory')}</h2><p>{t('Control which workspace context TradeMind may carry into future research sessions.')}</p></div></div>
          <div className="settings-card">
            <ToggleRow
              title="Workspace memory"
              description="Keep approved project facts, decisions, and operating preferences available across future sessions in this browser."
              checked={settings.workspaceMemory}
              onChange={(checked) => update('workspaceMemory', checked)}
            />
            <ToggleRow
              title="Reference recent conversations"
              description="Allow relevant context from recent project conversations to be considered when a new session begins."
              checked={settings.conversationMemory}
              onChange={(checked) => update('conversationMemory', checked)}
            />
          </div>
          <p className="settings-integration-note">{t('These controls save your local memory preference. Runtime use begins after the backend memory policy is connected.')}</p>
        </section>

        <section className="settings-section" aria-labelledby="integrations-heading">
          <div className="settings-section-heading"><span>08</span><div><h2 id="integrations-heading">{t('Integrations')}</h2><p>{t('Set the local permission gate for external tools used by the Agent team.')}</p></div></div>
          <div className="settings-card">
            <label id="plugins" className="settings-row settings-select-row settings-integration-row">
              <span><strong>{t('Plugins')}</strong><small>{t('Allow Agents to request tools supplied by connected plugin providers.')}</small><em>{t('Local permission preference')}</em></span>
              <select aria-label={t('Plugin access')} value={settings.pluginAccess} onChange={(event) => update('pluginAccess', event.target.value as LocalSettings['pluginAccess'])}>
                <option value="ask">{t('Ask before use')}</option>
                <option value="disabled">{t('Disabled')}</option>
              </select>
            </label>
            <label id="browser" className="settings-row settings-select-row settings-integration-row">
              <span><strong>{t('Browser')}</strong><small>{t('Allow Agents to request a browser session for research and page interaction.')}</small><em>{t('Local permission preference')}</em></span>
              <select aria-label={t('Browser access')} value={settings.browserAccess} onChange={(event) => update('browserAccess', event.target.value as LocalSettings['browserAccess'])}>
                <option value="ask">{t('Ask before use')}</option>
                <option value="disabled">{t('Disabled')}</option>
              </select>
            </label>
            <label className="settings-row settings-select-row settings-integration-row">
              <span><strong>{t('Computer control')}</strong><small>{t('Allow Agents to request control of approved desktop applications. Every session still requires explicit approval.')}</small><i>{t('High-risk setting')}</i></span>
              <select aria-label={t('Computer control access')} value={settings.computerControl} onChange={(event) => update('computerControl', event.target.value as LocalSettings['computerControl'])}>
                <option value="disabled">{t('Disabled')}</option>
                <option value="ask">{t('Ask every session')}</option>
              </select>
            </label>
          </div>
          <p className="settings-integration-note">{t('These controls save the intended local policy. Runtime enforcement depends on the corresponding backend integration being installed and connected.')}</p>
        </section>

        <section id="computer-control" className="settings-section settings-computer-control" aria-labelledby="computer-control-heading">
          <div className="settings-section-heading"><span>10</span><div><h2 id="computer-control-heading">{t('Computer control')}</h2><p>{t('Prepare this workspace for a trusted companion device without exposing it by default.')}</p></div></div>
          <div className="settings-card">
            <ToggleRow
              title="Allow this computer to be discovered and controlled"
              description="Authorized devices may request control only after the backend pairing service verifies the session."
              checked={settings.computerDiscoverable}
              warning
              onChange={(checked) => update('computerDiscoverable', checked)}
            />
            <ToggleRow
              title="Keep this computer awake"
              description="Prevent sleep while a verified remote-control or Agent run is active."
              checked={settings.keepAwake}
              onChange={(checked) => update('keepAwake', checked)}
            />
          </div>

          <div className="settings-pairing-card">
            <header><div><span>{t('Trusted device pairing')}</span><h3>{t('Connect a TradeMind companion device')}</h3></div><strong>{t('Not connected')}</strong></header>
            <ol>
              <li><span>01</span><p>{t('Install a signed TradeMind companion app when a mobile build is available.')}</p></li>
              <li><span>02</span><p>{t('Sign in with the same account and request a time-limited pairing code.')}</p></li>
              <li><span>03</span><p>{t('Approve control on this computer before the remote session can begin.')}</p></li>
            </ol>
            <div className="settings-pairing-placeholder">
              <span>{t('Pairing service required')}</span>
              <p>{t('A real QR code will appear here after the mobile client, account authentication, and encrypted pairing API are connected.')}</p>
            </div>
          </div>
        </section>
      </main>
    </div>
  )
}
