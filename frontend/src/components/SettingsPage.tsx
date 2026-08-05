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
          <a href="#permissions" aria-current="page"><span>01</span>Permissions</a>
          <a href="#general"><span>02</span>General</a>
          <a href="#runtime"><span>03</span>Runtime</a>
          <a href="#language"><span>04</span>Language</a>
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
              description="Prevent system sleep while an Opportunity Lead run is streaming."
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
      </main>
    </div>
  )
}
