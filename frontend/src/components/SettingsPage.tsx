import { useEffect, useState } from 'react'
import Brand from './Brand'
import './settings-page.css'

type LocalSettings = {
  workspaceAccess: boolean
  autoReview: boolean
  fullAccess: boolean
  keepAwake: boolean
  rememberProjects: boolean
  approvalMode: 'review_external' | 'review_all' | 'allow_low_risk'
  language: 'auto' | 'en' | 'zh-CN'
  permissionTimeout: '120' | '300' | '600'
  pluginAccess: 'ask' | 'disabled'
  browserAccess: 'ask' | 'disabled'
  computerControl: 'ask' | 'disabled'
  computerDiscoverable: boolean
  workspaceMemory: boolean
  conversationMemory: boolean
}

const storageKey = 'ophira-workspace-settings'

const defaultSettings: LocalSettings = {
  workspaceAccess: true,
  autoReview: true,
  fullAccess: false,
  keepAwake: true,
  rememberProjects: true,
  approvalMode: 'review_external',
  language: 'auto',
  permissionTimeout: '300',
  pluginAccess: 'ask',
  browserAccess: 'ask',
  computerControl: 'disabled',
  computerDiscoverable: false,
  workspaceMemory: true,
  conversationMemory: false,
}

function loadSettings(): LocalSettings {
  try {
    const saved = window.localStorage.getItem(storageKey)
    return saved ? { ...defaultSettings, ...JSON.parse(saved) as Partial<LocalSettings> } : defaultSettings
  } catch {
    return defaultSettings
  }
}

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
  return (
    <label className="settings-row settings-toggle-row">
      <span><strong>{title}</strong><small>{description}</small>{warning && <i>High-risk setting</i>}</span>
      <input type="checkbox" role="switch" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    </label>
  )
}

export default function SettingsPage() {
  const [settings, setSettings] = useState<LocalSettings>(loadSettings)
  const [activeSection, setActiveSection] = useState('permissions')

  useEffect(() => {
    window.localStorage.setItem(storageKey, JSON.stringify(settings))
  }, [settings])

  const update = <Key extends keyof LocalSettings>(key: Key, value: LocalSettings[Key]) => {
    setSettings((current) => ({ ...current, [key]: value }))
  }

  return (
    <div className="settings-shell">
      <aside className="settings-sidebar">
        <a className="settings-back" href="/app"><span aria-hidden="true">←</span> Back to workspace</a>
        <a className="settings-brand" href="/" aria-label="OPHIRA website"><Brand /></a>
        <nav aria-label="Settings sections">
          <p>Workspace settings</p>
          <a href="#permissions" aria-current={activeSection === 'permissions' ? 'page' : undefined} onClick={() => setActiveSection('permissions')}><span>01</span>Permissions</a>
          <a href="#general" aria-current={activeSection === 'general' ? 'page' : undefined} onClick={() => setActiveSection('general')}><span>02</span>General</a>
          <a href="#runtime" aria-current={activeSection === 'runtime' ? 'page' : undefined} onClick={() => setActiveSection('runtime')}><span>03</span>Runtime</a>
          <a href="#language" aria-current={activeSection === 'language' ? 'page' : undefined} onClick={() => setActiveSection('language')}><span>04</span>Language</a>
          <a href="#memory" aria-current={activeSection === 'memory' ? 'page' : undefined} onClick={() => setActiveSection('memory')}><span>05</span>Memory</a>
          <p>Integrations</p>
          <a href="#plugins" aria-current={activeSection === 'plugins' ? 'page' : undefined} onClick={() => setActiveSection('plugins')}><span>06</span>Plugins</a>
          <a href="#browser" aria-current={activeSection === 'browser' ? 'page' : undefined} onClick={() => setActiveSection('browser')}><span>07</span>Browser</a>
          <a href="#computer-control" aria-current={activeSection === 'computer-control' ? 'page' : undefined} onClick={() => setActiveSection('computer-control')}><span>08</span>Computer control</a>
        </nav>
        <div className="settings-sidebar-note">
          <span>Saved locally</span>
          <p>These controls apply to this browser until account-level settings are connected.</p>
        </div>
      </aside>

      <main className="settings-main">
        <header>
          <p>OPHIRA workspace</p>
          <h1>General settings</h1>
          <span>Configure how the opportunity team accesses context and asks for approval.</span>
        </header>

        <section id="permissions" className="settings-section" aria-labelledby="permissions-heading">
          <div className="settings-section-heading"><span>01</span><div><h2 id="permissions-heading">Permissions</h2><p>Choose what the Agent team can access without interrupting your work.</p></div></div>
          <div className="settings-card">
            <ToggleRow
              title="Workspace access"
              description="Allow Ophira to read and update files inside the selected project workspace. Access outside the workspace still requires approval."
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
          <div className="settings-section-heading"><span>02</span><div><h2 id="general-heading">General</h2><p>Set the default behavior for new opportunity studies.</p></div></div>
          <div className="settings-card">
            <label className="settings-row settings-select-row">
              <span><strong>Default approval mode</strong><small>How Ophira should handle tool and external-action requests.</small></span>
              <select value={settings.approvalMode} onChange={(event) => update('approvalMode', event.target.value as LocalSettings['approvalMode'])}>
                <option value="review_external">Review external actions</option>
                <option value="review_all">Review every action</option>
                <option value="allow_low_risk">Allow low-risk actions</option>
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
          <div className="settings-section-heading"><span>03</span><div><h2 id="runtime-heading">Runtime</h2><p>Control long-running research and approval waits.</p></div></div>
          <div className="settings-card">
            <ToggleRow
              title="Keep this device awake during active runs"
              description="Prevent system sleep while an Agent run is streaming."
              checked={settings.keepAwake}
              onChange={(checked) => update('keepAwake', checked)}
            />
            <label className="settings-row settings-select-row">
              <span><strong>Permission request timeout</strong><small>How long a run waits for your decision before denying the request.</small></span>
              <select value={settings.permissionTimeout} onChange={(event) => update('permissionTimeout', event.target.value as LocalSettings['permissionTimeout'])}>
                <option value="120">2 minutes</option>
                <option value="300">5 minutes</option>
                <option value="600">10 minutes</option>
              </select>
            </label>
          </div>
        </section>

        <section id="language" className="settings-section" aria-labelledby="language-heading">
          <div className="settings-section-heading"><span>04</span><div><h2 id="language-heading">Language</h2><p>Choose the interface language for this browser.</p></div></div>
          <div className="settings-card">
            <label className="settings-row settings-select-row">
              <span><strong>Interface language</strong><small>Research evidence keeps its original source language.</small></span>
              <select value={settings.language} onChange={(event) => update('language', event.target.value as LocalSettings['language'])}>
                <option value="auto">Auto-detect</option>
                <option value="en">English</option>
                <option value="zh-CN">简体中文</option>
              </select>
            </label>
          </div>
        </section>

        <section id="memory" className="settings-section" aria-labelledby="memory-heading">
          <div className="settings-section-heading"><span>05</span><div><h2 id="memory-heading">Memory</h2><p>Control which workspace context Ophira may carry into future research sessions.</p></div></div>
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
          <p className="settings-integration-note">These controls save your local memory preference. Runtime use begins after the backend memory policy is connected.</p>
        </section>

        <section className="settings-section" aria-labelledby="integrations-heading">
          <div className="settings-section-heading"><span>06</span><div><h2 id="integrations-heading">Integrations</h2><p>Set the local permission gate for external tools used by the Agent team.</p></div></div>
          <div className="settings-card">
            <label id="plugins" className="settings-row settings-select-row settings-integration-row">
              <span><strong>Plugins</strong><small>Allow Agents to request tools supplied by connected plugin providers.</small><em>Local permission preference</em></span>
              <select aria-label="Plugin access" value={settings.pluginAccess} onChange={(event) => update('pluginAccess', event.target.value as LocalSettings['pluginAccess'])}>
                <option value="ask">Ask before use</option>
                <option value="disabled">Disabled</option>
              </select>
            </label>
            <label id="browser" className="settings-row settings-select-row settings-integration-row">
              <span><strong>Browser</strong><small>Allow Agents to request a browser session for research and page interaction.</small><em>Local permission preference</em></span>
              <select aria-label="Browser access" value={settings.browserAccess} onChange={(event) => update('browserAccess', event.target.value as LocalSettings['browserAccess'])}>
                <option value="ask">Ask before use</option>
                <option value="disabled">Disabled</option>
              </select>
            </label>
            <label className="settings-row settings-select-row settings-integration-row">
              <span><strong>Computer control</strong><small>Allow Agents to request control of approved desktop applications. Every session still requires explicit approval.</small><i>High-risk setting</i></span>
              <select aria-label="Computer control access" value={settings.computerControl} onChange={(event) => update('computerControl', event.target.value as LocalSettings['computerControl'])}>
                <option value="disabled">Disabled</option>
                <option value="ask">Ask every session</option>
              </select>
            </label>
          </div>
          <p className="settings-integration-note">These controls save the intended local policy. Runtime enforcement depends on the corresponding backend integration being installed and connected.</p>
        </section>

        <section id="computer-control" className="settings-section settings-computer-control" aria-labelledby="computer-control-heading">
          <div className="settings-section-heading"><span>08</span><div><h2 id="computer-control-heading">Computer control</h2><p>Prepare this workspace for a trusted companion device without exposing it by default.</p></div></div>
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
            <header><div><span>Trusted device pairing</span><h3>Connect an OPHIRA companion device</h3></div><strong>Not connected</strong></header>
            <ol>
              <li><span>01</span><p>Install a signed OPHIRA companion app when a mobile build is available.</p></li>
              <li><span>02</span><p>Sign in with the same account and request a time-limited pairing code.</p></li>
              <li><span>03</span><p>Approve control on this computer before the remote session can begin.</p></li>
            </ol>
            <div className="settings-pairing-placeholder">
              <span>Pairing service required</span>
              <p>A real QR code will appear here after the mobile client, account authentication, and encrypted pairing API are connected.</p>
            </div>
          </div>
        </section>
      </main>
    </div>
  )
}
