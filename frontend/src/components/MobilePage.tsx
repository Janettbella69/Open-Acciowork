import { useEffect, useState } from 'react'
import { useT } from '../i18n'
import { loadLocalSettings, saveLocalSettings, type LocalSettings } from '../localSettings'
import Brand from './Brand'
import WorkspaceNav from './WorkspaceNav'
import './workspace-composer.css'
import './mobile-page.css'

function MobileToggle({
  title,
  description,
  checked,
  onChange,
}: {
  title: string
  description: string
  checked: boolean
  onChange: (checked: boolean) => void
}) {
  const t = useT()
  return (
    <label className="mobile-setting-row">
      <span><strong>{t(title)}</strong><small>{t(description)}</small></span>
      <input type="checkbox" role="switch" checked={checked} onChange={(event) => onChange(event.target.checked)} />
    </label>
  )
}

export default function MobilePage() {
  const t = useT()
  const [settings, setSettings] = useState<LocalSettings>(loadLocalSettings)

  useEffect(() => {
    saveLocalSettings(settings)
  }, [settings])

  const update = <Key extends keyof LocalSettings>(key: Key, value: LocalSettings[Key]) => {
    setSettings((current) => ({ ...current, [key]: value }))
  }

  return (
    <div className="mobile-shell">
      <aside className="conversation-sidebar mobile-sidebar">
        <a className="conversation-sidebar-brand" href="/" aria-label={t('Back to TradeMind website')}><Brand /></a>
        <a className="conversation-new-research" href="/app"><span aria-hidden="true">＋</span> {t('New task')}</a>
        <WorkspaceNav active="mobile" />
        <div className="mobile-sidebar-note"><strong>{t('Companion access')}</strong><p>{t('Remote access remains off until both local policy and secure pairing are enabled.')}</p></div>
      </aside>

      <main className="mobile-main">
        <header className="mobile-hero">
          <svg aria-hidden="true" viewBox="0 0 64 64">
            <rect x="7" y="12" width="35" height="27" rx="3" />
            <path d="M18 48h14m-7-9v9" />
            <rect x="39" y="22" width="18" height="31" rx="3" />
            <path d="M46 48h4" />
          </svg>
          <span>{t('Not connected')}</span>
          <h1>{t('Connect to TradeMind Mobile')}</h1>
          <p>{t('Receive updates and continue approved tasks from a trusted companion device when the mobile client and pairing service are available.')}</p>
        </header>

        <section className="mobile-settings" aria-labelledby="mobile-settings-heading">
          <h2 id="mobile-settings-heading">{t('Keep these settings enabled')}</h2>
          <div>
            <MobileToggle
              title="Allow this computer to be discovered and controlled"
              description="Authorized devices may request control only after the backend pairing service verifies the session."
              checked={settings.computerDiscoverable}
              onChange={(checked) => update('computerDiscoverable', checked)}
            />
            <MobileToggle
              title="Keep this computer awake"
              description="Prevent sleep while a verified remote-control or Agent run is active."
              checked={settings.keepAwake}
              onChange={(checked) => update('keepAwake', checked)}
            />
          </div>
        </section>

        <section className="mobile-pairing" aria-labelledby="mobile-pairing-heading">
          <div><span>{t('Trusted device pairing')}</span><h2 id="mobile-pairing-heading">{t('Connect a mobile device')}</h2></div>
          <ol>
            <li><span>01</span><div><strong>{t('Install the companion app')}</strong><p>{t('Install a signed TradeMind companion app when a mobile build is available.')}</p></div></li>
            <li><span>02</span><div><strong>{t('Use the same account')}</strong><p>{t('Sign in with the same account and request a time-limited pairing code.')}</p></div></li>
            <li><span>03</span><div><strong>{t('Approve this computer')}</strong><p>{t('Approve control on this computer before the remote session can begin.')}</p></div></li>
          </ol>
          <div className="mobile-pairing-status">
            <div><span>{t('Pairing service required')}</span><strong>{t('No QR code generated')}</strong></div>
            <p>{t('A real QR code will appear here after the mobile client, account authentication, and encrypted pairing API are connected.')}</p>
          </div>
        </section>
      </main>
    </div>
  )
}
