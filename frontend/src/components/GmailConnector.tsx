import { useEffect, useState } from 'react'
import { errorMessage, getGmailOAuthStatus, revokeConnector, startGmailOAuth, upsertConnector } from '../api/client'
import type { GmailOAuthStatus } from '../api/client'
import type { Connector } from '../api/types'
import { useT } from '../i18n'

const GMAIL_SCOPE = 'https://www.googleapis.com/auth/gmail.readonly'

export default function GmailConnector({ connector, onChange }: {
  connector: Connector
  onChange: (connector: Connector) => void
}) {
  const t = useT()
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [status, setStatus] = useState<GmailOAuthStatus | null>(null)
  const [authorizationUrl, setAuthorizationUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    let active = true
    void getGmailOAuthStatus(connector.id).then((value) => {
      if (active) setStatus(value)
    }).catch((cause: unknown) => {
      if (active) setError(errorMessage(cause))
    })
    return () => { active = false }
  }, [connector.id, connector.revision])

  async function act(action: () => Promise<void>) {
    if (busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await action()
    } catch (cause) {
      setError(errorMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  function save() {
    void act(async () => {
      const updated = await upsertConnector(connector.id, {
        type: 'gmail', display_name: connector.display_name,
        scope: GMAIL_SCOPE, secret_env: connector.secret_env,
        endpoint: null,
        secret: JSON.stringify({ client_id: clientId.trim(), client_secret: clientSecret.trim() }),
      })
      setClientId('')
      setClientSecret('')
      setAuthorizationUrl('')
      setStatus((current) => current ? { ...current, authorized: false, email: null } : null)
      onChange(updated)
      setNotice(t('Gmail OAuth client saved. Authorize the mailbox next.'))
    })
  }

  function authorize() {
    void act(async () => {
      const result = await startGmailOAuth(connector.id)
      setAuthorizationUrl(result.authorization_url)
    })
  }

  function revoke() {
    void act(async () => {
      const updated = await revokeConnector(connector.id)
      onChange(updated)
      setStatus((current) => current ? { ...current, authorized: false, email: null } : null)
      setAuthorizationUrl('')
    })
  }

  return <article className="marketplace-connector" data-enabled={connector.enabled ? 'true' : 'false'}>
    <div className="connector-copy">
      <header><h3>{connector.display_name}</h3><span>{t(status?.authorized ? 'Mailbox authorized' : 'Authorization required')}</span></header>
      <p>{t('Connect a Gmail mailbox for inquiry email triggers. OPHIRA reads new Inbox messages only; the Agent receives bounded plain text and cannot send mail through this connector.')}</p>
      <details>
        <summary>{t('Configure Gmail OAuth')}</summary>
        <ol>
          <li>{t('Create a Google OAuth web client and enable the Gmail API in the same Google Cloud project.')}</li>
          <li>{t('Register the exact callback URL below in the OAuth client settings.')}</li>
          <li>{t('Grant Gmail publish access to the configured Pub/Sub topic and set up an authenticated push subscription to the URL shown on the trigger.')}</li>
        </ol>
        {status?.callback_url && <p>{t('Gmail OAuth callback URL')}: <code>{status.callback_url}</code></p>}
        {!status?.callback_url && <p>{t('A trusted gateway and public HTTPS event origin are required for Gmail OAuth.')}</p>}
        <form onSubmit={(event) => { event.preventDefault(); save() }}>
          <fieldset disabled={busy}>
            <label>{t('OAuth web client ID')}<input type="text" autoComplete="off" required maxLength={255} value={clientId} onChange={(event) => setClientId(event.target.value)} /></label>
            <label>{t('OAuth client secret')}<input type="password" autoComplete="new-password" required maxLength={512} value={clientSecret} onChange={(event) => setClientSecret(event.target.value)} /></label>
            <button type="submit">{t('Save Gmail credentials')}</button>
          </fieldset>
        </form>
      </details>
      <div className="marketplace-toolbar">
        <button type="button" disabled={busy || !connector.enabled || !status?.callback_url} onClick={authorize}>{t('Authorize Gmail mailbox')}</button>
        <button type="button" disabled={busy || !connector.enabled} onClick={revoke}>{t('Revoke')}</button>
        {busy && <span role="status">{t('Working…')}</span>}
      </div>
      {status?.email && <p>{t('Authorized mailbox')}: {status.email}</p>}
      {authorizationUrl && <p><a href={authorizationUrl} referrerPolicy="no-referrer">{t('Continue to Google authorization')}</a></p>}
      {error && <p className="connectors-error" role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
    </div>
  </article>
}
