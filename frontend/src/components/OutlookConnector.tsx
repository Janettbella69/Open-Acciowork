import { useEffect, useState } from 'react'
import { errorMessage, getOutlookOAuthStatus, getOutlookProjectAccess, grantOutlookProjectAccess, listProjects, revokeConnector, revokeOutlookProjectAccess, startOutlookOAuth, upsertConnector } from '../api/client'
import type { OutlookOAuthStatus, OutlookProjectAccess } from '../api/client'
import type { Connector, OpportunityProject } from '../api/types'
import { useT } from '../i18n'

const OUTLOOK_SCOPE = 'offline_access User.Read Mail.Read'

export default function OutlookConnector({ connector, onChange }: {
  connector: Connector
  onChange: (connector: Connector) => void
}) {
  const t = useT()
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [status, setStatus] = useState<OutlookOAuthStatus | null>(null)
  const [authorizationUrl, setAuthorizationUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [projects, setProjects] = useState<OpportunityProject[] | null>(null)
  const [projectId, setProjectId] = useState('')
  const [access, setAccess] = useState<OutlookProjectAccess | null>(null)
  const [days, setDays] = useState(7)

  useEffect(() => {
    let active = true
    void getOutlookOAuthStatus(connector.id).then((value) => {
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
        type: 'outlook', display_name: connector.display_name,
        scope: OUTLOOK_SCOPE, secret_env: connector.secret_env,
        endpoint: null,
        secret: JSON.stringify({ client_id: clientId.trim(), client_secret: clientSecret.trim() }),
      })
      setClientId('')
      setClientSecret('')
      setAuthorizationUrl('')
      setStatus((current) => current ? { ...current, authorized: false, email: null } : null)
      onChange(updated)
      setNotice(t('Outlook OAuth client saved. Authorize the mailbox next.'))
    })
  }

  function authorize() {
    void act(async () => {
      const result = await startOutlookOAuth(connector.id)
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

  function loadProjects() {
    void act(async () => { setProjects(await listProjects()) })
  }

  function loadAccess(id: string) {
    setProjectId(id)
    setAccess(null)
    if (id) void act(async () => { setAccess(await getOutlookProjectAccess(id)) })
  }

  function grantProject() {
    if (!access || access.project_id !== projectId) return
    void act(async () => {
      setAccess(await grantOutlookProjectAccess(projectId, {
        expected_revision: access.revision, connector_id: connector.id,
        expires_in_seconds: days * 86400,
      }))
      setNotice(t('Project Agent Outlook reads allowed until the grant expires.'))
    })
  }

  function revokeProject(grantId: string) {
    if (!access || access.project_id !== projectId) return
    void act(async () => {
      setAccess(await revokeOutlookProjectAccess(projectId, grantId, access.revision))
      setNotice(t('Project Outlook access revoked. Existing evidence remains.'))
    })
  }

  const grants = access?.project_id === projectId
    ? access.grants.filter((item) => item.connector_id === connector.id) : []

  return <article className="marketplace-connector" data-enabled={connector.enabled ? 'true' : 'false'}>
    <div className="connector-copy">
      <header><h3>{connector.display_name}</h3><span>{t(status?.authorized ? 'Mailbox authorized' : 'Authorization required')}</span></header>
      <p>{t('Connect an Outlook mailbox for project Agent reads. Only explicitly granted projects can read bounded Inbox plain text; this connector cannot send mail.')}</p>
      <details>
        <summary>{t('Configure Outlook OAuth')}</summary>
        <ol>
          <li>{t('Register a Microsoft Entra web app with delegated User.Read and Mail.Read permissions and a client secret.')}</li>
          <li>{t('Register the exact callback URL below in the OAuth client settings.')}</li>
        </ol>
        {status?.callback_url && <p>{t('Outlook OAuth callback URL')}: <code>{status.callback_url}</code></p>}
        {!status?.callback_url && <p>{t('A trusted gateway and public HTTPS event origin are required for Outlook OAuth.')}</p>}
        <form onSubmit={(event) => { event.preventDefault(); save() }}>
          <fieldset disabled={busy}>
            <label>{t('OAuth web client ID')}<input type="text" autoComplete="off" required maxLength={255} value={clientId} onChange={(event) => setClientId(event.target.value)} /></label>
            <label>{t('OAuth client secret')}<input type="password" autoComplete="new-password" required maxLength={512} value={clientSecret} onChange={(event) => setClientSecret(event.target.value)} /></label>
            <button type="submit">{t('Save Outlook credentials')}</button>
          </fieldset>
        </form>
      </details>
      <div className="marketplace-toolbar">
        <button type="button" disabled={busy || !connector.enabled || !status?.callback_url} onClick={authorize}>{t('Authorize Outlook mailbox')}</button>
        <button type="button" disabled={busy || !connector.enabled} onClick={revoke}>{t('Revoke')}</button>
        {busy && <span role="status">{t('Working…')}</span>}
      </div>
      {status?.email && <p>{t('Authorized mailbox')}: {status.email}</p>}
      {authorizationUrl && <p><a href={authorizationUrl} referrerPolicy="no-referrer">{t('Continue to Microsoft authorization')}</a></p>}
      <details className="marketplace-project-access" onToggle={(event) => {
        if (event.currentTarget.open && projects === null) loadProjects()
      }}>
        <summary>{t('Project access for Agents')}</summary>
        <p>{t('A tenant and project administrator grants a mailbox for up to 30 days. The Agent can read Inbox messages during a running project session; each page has a source receipt. Revoke access here at any time.')}</p>
        <fieldset disabled={busy}>
          <label>{t('Project for Outlook access')}<select value={projectId} onChange={(event) => loadAccess(event.target.value)}>
            <option value="">{t('Select a project')}</option>
            {projects?.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select></label>
          <button type="button" onClick={() => projectId ? loadAccess(projectId) : loadProjects()}>{t('Refresh Outlook grants')}</button>
          <label>{t('Mailbox access duration')}<select value={days} onChange={(event) => setDays(Number(event.target.value))}>
            <option value={1}>{t('1 day')}</option><option value={7}>{t('7 days')}</option><option value={30}>{t('30 days')}</option>
          </select></label>
          <button type="button" disabled={!access || !status?.authorized || !connector.enabled} onClick={grantProject}>{t('Allow Agent Outlook reads')}</button>
        </fieldset>
        {projects?.length === 0 && <p>{t('Create a saved project before granting Outlook access.')}</p>}
        {access && <div className="marketplace-table"><table>
          <caption>{t('Project Outlook grants')}</caption>
          <thead><tr><th>{t('Mailbox')}</th><th>{t('Expires')}</th><th>{t('Status')}</th><th>{t('Action')}</th></tr></thead>
          <tbody>{grants.map((grant) => <tr key={grant.id}>
            <td>{grant.mailbox}</td>
            <td>{new Date(grant.expires_at).toLocaleString()}</td>
            <td>{t(grant.revoked_at ? 'Revoked' : grant.active && new Date(grant.expires_at).getTime() > Date.now() ? 'Active' : 'Expired')}</td>
            <td>{!grant.revoked_at && <button type="button" disabled={busy} onClick={() => revokeProject(grant.id)}>{t('Revoke project access')}</button>}</td>
          </tr>)}</tbody>
        </table>{grants.length === 0 && <p>{t('No Outlook grants for this connector in the selected project.')}</p>}</div>}
      </details>
      {error && <p className="connectors-error" role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
    </div>
  </article>
}
