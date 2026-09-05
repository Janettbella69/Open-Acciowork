import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../api/client'
import {
  consumePairingFragment,
  getCompanionSession,
  getMobileStatus,
  isCompanionModeRemembered,
  rememberCompanionMode,
  type CompanionSession,
  type PairingFragment,
} from '../api/companion'
import { useT } from '../i18n'
import Brand from './Brand'
import MobileCompanion from './MobileCompanion'
import MobilePairingManager from './MobilePairingManager'
import WorkspaceNav from './WorkspaceNav'
import './workspace-composer.css'
import './mobile-page.css'

export default function MobilePage() {
  const t = useT()
  const [surface, setSurface] = useState<'checking' | 'owner' | 'companion'>('checking')
  const [session, setSession] = useState<CompanionSession | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const entry = useRef<PairingFragment | null>(null)

  useEffect(() => {
    let active = true
    // A ref survives StrictMode effect replay; never consume a one-time link twice.
    entry.current ??= consumePairingFragment()
    if (entry.current.kind !== 'none') {
      setSurface('companion')
      return () => { active = false }
    }
    void getCompanionSession().then((result) => {
      if (!active) return
      setSession(result)
      const remote = isCompanionModeRemembered() || result.device !== null
      if (remote) rememberCompanionMode()
      setSurface(remote ? 'companion' : 'owner')
      setError('')
    }).catch(async (cause: unknown) => {
      if (!active) return
      // A local desktop cannot call the HTTPS-only companion endpoint. Probe the
      // normal authenticated boundary instead, but never do this on a phone.
      if (!isCompanionModeRemembered()) {
        try {
          await getMobileStatus()
          if (active) { setSurface('owner'); setError('') }
          return
        } catch { /* Neither boundary accepted this browser; remain closed. */ }
      }
      if (active) setError(errorMessage(cause))
    })
    return () => { active = false }
  }, [attempt])

  return (
    <div className={`mobile-shell${surface === 'companion' ? ' mobile-shell-companion' : ''}`}>
      {surface === 'owner' && <aside className="conversation-sidebar mobile-sidebar">
        <a className="conversation-sidebar-brand" href="/" aria-label={t('Back to TradeMind website')}><Brand /></a>
        <a className="conversation-new-research" href="/app"><span aria-hidden="true">＋</span> {t('New task')}</a>
        <WorkspaceNav active="mobile" />
        <div className="mobile-sidebar-note"><strong>{t('Companion access')}</strong><p>{t('Only explicitly paired devices can access the projects you select. Revoke a device here at any time.')}</p></div>
      </aside>}
      <main className="mobile-main">
        <header className="mobile-hero">
          <span>{t(surface === 'owner' ? 'Device trust' : 'Companion access')}</span>
          <h1>{t('Mobile approval')}</h1>
          <p>{t('Pair a trusted device to review requests, follow task status and start work in authorized projects.')}</p>
        </header>
        {surface === 'checking' && <section className="mobile-settings">
          {error ? <><p className="inbox-error" role="alert">{error}</p><button type="button" onClick={() => setAttempt((value) => value + 1)}>{t('Retry connection')}</button></> : <p role="status">{t('Checking device session…')}</p>}
        </section>}
        {surface === 'owner' && <MobilePairingManager />}
        {surface === 'companion' && <MobileCompanion initialSession={session} entry={entry.current ?? { kind: 'none' }} />}
        <p className="mobile-service-note">{t('This page refreshes while open. Background push notifications and OS sleep prevention are not enabled. GUI tasks still require the host computer to be online.')}</p>
      </main>
    </div>
  )
}
