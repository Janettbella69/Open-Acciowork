import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import {
  addCatalogMcp,
  errorMessage,
  getUserMcpOAuthSetup,
  listProjects,
  listUserMcp,
  listUserMcpCatalog,
  reviewUserMcp,
  revokeUserMcp,
  saveUserMcp,
  startUserMcpOAuth,
  setUserMcpToolPolicy,
} from '../api/client'
import type { OpportunityProject, UserMcpCatalogItem, UserMcpConnector, UserMcpOAuthSetup, UserMcpTool } from '../api/types'
import { useT } from '../i18n'

export default function UserMcpPanel() {
  const t = useT()
  const pluginPrefill = new URLSearchParams(window.location.search)
  const pluginName = (pluginPrefill.get('plugin_mcp_name') ?? '').slice(0, 100)
  const pluginUrl = (pluginPrefill.get('plugin_mcp_url') ?? '').slice(0, 2000)
  const pluginId = (pluginPrefill.get('plugin_mcp_id') ?? `plugin_${pluginName}`)
    .toLowerCase().replace(/[^a-z0-9]+/g, '_').slice(0, 64)
  const [projects, setProjects] = useState<OpportunityProject[]>([])
  const [projectId, setProjectId] = useState('')
  const [catalog, setCatalog] = useState<UserMcpCatalogItem[]>([])
  const [servers, setServers] = useState<UserMcpConnector[]>([])
  const [oauthSetup, setOauthSetup] = useState<UserMcpOAuthSetup | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [name, setName] = useState(pluginName)
  const [id, setId] = useState(pluginName ? pluginId : '')
  const [url, setUrl] = useState(pluginUrl)
  const [token, setToken] = useState('')
  const [oauthIssuer, setOauthIssuer] = useState('')
  const [oauthClientId, setOauthClientId] = useState('')
  const [oauthClientSecret, setOauthClientSecret] = useState('')
  const [oauthClientAuthMethod, setOauthClientAuthMethod] = useState<'client_secret_basic' | 'client_secret_post'>('client_secret_basic')
  const [oauthLink, setOauthLink] = useState<{ id: string; url: string } | null>(null)
  const busyRef = useRef(false)
  const advancedRef = useRef<HTMLDetailsElement>(null)

  useEffect(() => { if (pluginName && advancedRef.current) advancedRef.current.open = true }, [pluginName])

  useEffect(() => {
    let active = true
    void listProjects()
      .then((items) => {
        if (!active) return
        setProjects(items)
        setProjectId((current) => current || items[0]?.id || '')
      })
      .catch((loadError) => { if (active) setError(errorMessage(loadError)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (!projectId) return
    let active = true
    setLoading(true)
    setOauthSetup(null)
    void Promise.all([listUserMcpCatalog(projectId), listUserMcp(projectId), getUserMcpOAuthSetup(projectId)])
      .then(([available, configured, setup]) => {
        if (!active) return
        setCatalog(available)
        setServers(configured)
        setOauthSetup(setup)
        setError('')
      })
      .catch((loadError) => { if (active) setError(errorMessage(loadError)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [projectId])

  useEffect(() => {
    if (!projectId || !oauthLink) return
    const interval = window.setInterval(() => {
      void listUserMcp(projectId).then((items) => {
        setServers(items)
        const target = items.find((item) => item.id === oauthLink.id)
        if (target?.enabled || target?.review_error) setOauthLink(null)
      }).catch(() => undefined)
    }, 3000)
    return () => window.clearInterval(interval)
  }, [projectId, oauthLink])

  const act = async (key: string, action: () => Promise<UserMcpConnector>) => {
    if (!projectId || busyRef.current) return
    busyRef.current = true
    setBusy(key)
    setError('')
    try {
      const updated = await action()
      setServers((current) => [updated, ...current.filter((item) => item.id !== updated.id)])
      return updated
    } catch (actionError) {
      setError(errorMessage(actionError))
      void listUserMcp(projectId).then(setServers).catch(() => undefined)
    } finally {
      busyRef.current = false
      setBusy('')
    }
  }

  const saveCustom = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!projectId || !/^[a-z][a-z0-9_]{2,63}$/.test(id)) return
    const saved = await act('custom', () => saveUserMcp(projectId, id, {
      name: name.trim(), url: url.trim(),
      ...(token ? { bearer_token: token } : {}),
      ...(oauthIssuer.trim() ? { oauth_issuer: oauthIssuer.trim() } : {}),
      ...(oauthIssuer.trim() && oauthClientId.trim() ? {
        oauth_client_id: oauthClientId.trim(),
        oauth_client_auth_method: oauthClientSecret ? oauthClientAuthMethod : 'none',
        ...(oauthClientSecret ? { oauth_client_secret: oauthClientSecret } : {}),
      } : {}),
    }))
    if (saved) {
      setName('')
      setId('')
      setUrl('')
      setToken('')
      setOauthIssuer('')
      setOauthClientId('')
      setOauthClientSecret('')
      setOauthClientAuthMethod('client_secret_basic')
    }
  }

  const beginOAuth = async (serverId: string) => {
    if (!projectId || busyRef.current) return
    busyRef.current = true
    setBusy(`${serverId}:oauth`)
    setError('')
    setOauthLink(null)
    try {
      const result = await startUserMcpOAuth(projectId, serverId)
      setOauthLink({ id: serverId, url: result.authorization_url })
      setServers(await listUserMcp(projectId))
    } catch (actionError) {
      setError(errorMessage(actionError))
      void listUserMcp(projectId).then(setServers).catch(() => undefined)
    } finally {
      busyRef.current = false
      setBusy('')
    }
  }

  return (
    <section className="user-mcp" aria-labelledby="user-mcp-heading">
      <header>
        <div>
          <span>{t('External tools')}</span>
          <h2 id="user-mcp-heading">{t('MCP connectors')}</h2>
          <p>{t('Connect a reviewed server to one project. Each tool has its own approval rule.')}</p>
        </div>
        <label>
          <span>{t('Project')}</span>
          <select aria-label={t('MCP project')} value={projectId} onChange={(event) => { setProjectId(event.target.value); setOauthLink(null) }} disabled={Boolean(busy)}>
            {projects.length === 0 && <option value="">{t('No projects yet')}</option>}
            {projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select>
        </label>
      </header>
      {error && <p className="connectors-error" role="alert">{error}</p>}
      {loading && <p role="status">{t('Loading MCP connectors…')}</p>}
      {!loading && !projectId && <p>{t('Create a project to connect external tools.')}</p>}
      {projectId && <>
        <div className="user-mcp-catalog">
          <h3>{t('Connector directory')}</h3>
          <div className="user-mcp-cards">
            {catalog.map((item) => <article key={item.id}>
              <div><strong>{item.name}</strong><p>{t(item.description)}</p><small>{item.url}</small></div>
              <button type="button" disabled={Boolean(busy) || servers.some((server) => server.id === item.id)} onClick={() => void act(item.id, () => addCatalogMcp(projectId, item.id))}>
                {servers.some((server) => server.id === item.id) ? t('Added') : busy === item.id ? t('Reviewing…') : t('Add and review')}
              </button>
            </article>)}
          </div>
        </div>
        <details className="user-mcp-advanced" ref={advancedRef}>
          <summary>{t('Advanced: add a remote MCP address')}</summary>
          <p>{t('Custom hosts need organization approval in Settings. Saving a server keeps it disabled until its live tool review succeeds.')}</p>
          <form onSubmit={(event) => void saveCustom(event)}>
            <label>{t('Server name')}<input value={name} onChange={(event) => setName(event.target.value)} maxLength={160} required /></label>
            <label>{t('Server ID')}<input value={id} onChange={(event) => { setId(event.target.value); setOauthClientId(''); setOauthClientSecret('') }} pattern="[a-z][a-z0-9_]{2,63}" placeholder="my_mcp" required /></label>
            <label>{t('HTTPS MCP address')}<input type="url" value={url} onChange={(event) => { setUrl(event.target.value); setOauthClientId(''); setOauthClientSecret('') }} placeholder="https://mcp.example.com/mcp" required /></label>
            <label>{t('Bearer token (optional)')}<input type="password" value={token} onChange={(event) => setToken(event.target.value)} autoComplete="off" disabled={Boolean(oauthIssuer)} /></label>
            <label>{t('OAuth issuer URL (optional)')}<input type="url" value={oauthIssuer} onChange={(event) => { setOauthIssuer(event.target.value); setOauthClientId(''); setOauthClientSecret('') }} disabled={Boolean(token)} placeholder="https://auth.example.com" /></label>
            <p>{t('For OAuth, approve the issuer host in organization settings, then save and connect in the browser.')}</p>
            {oauthIssuer && <>
              {oauthSetup?.redirect_uri
                ? <p>{t('Register this OAuth redirect URI with the authorization server:')} <code>{oauthSetup.redirect_uri}</code></p>
                : <p>{t('OAuth callback origin is not configured by the operator.')}</p>}
              {oauthSetup?.client_metadata_url && <p>{t('When supported, OPHIRA automatically uses this client ID metadata URL:')} <code>{oauthSetup.client_metadata_url}</code></p>}
              <label>{t('Pre-registered OAuth client ID (optional)')}<input value={oauthClientId} onChange={(event) => { setOauthClientId(event.target.value); setOauthClientSecret('') }} maxLength={500} autoComplete="off" /></label>
              {oauthClientId && <>
                <label>{t('OAuth client secret (if required)')}<input type="password" value={oauthClientSecret} onChange={(event) => setOauthClientSecret(event.target.value)} autoComplete="off" /></label>
                {oauthClientSecret && <label>{t('Token endpoint authentication')}<select value={oauthClientAuthMethod} onChange={(event) => setOauthClientAuthMethod(event.target.value as 'client_secret_basic' | 'client_secret_post')}>
                  <option value="client_secret_basic">{t('HTTP Basic')}</option>
                  <option value="client_secret_post">{t('Form body')}</option>
                </select></label>}
              </>}
            </>}
            <button type="submit" disabled={Boolean(busy)}>{t('Save for review')}</button>
          </form>
        </details>
        <div className="user-mcp-configured">
          <h3>{t('Project MCP servers')}</h3>
          {servers.length === 0 && <p>{t('No remote MCP servers in this project.')}</p>}
          {servers.map((server) => <article key={server.id}>
            <header><div><strong>{server.name}</strong><small>{server.url}</small></div>
              <span>{server.enabled ? t('Reviewed') : t('Disabled · review required')}</span>
            </header>
            {server.review_error && <p role="alert">{t(server.review_error)}</p>}
            {server.auth_mode === 'oauth' && server.oauth_issuer && <p>{t('OAuth issuer')}: {server.oauth_issuer}</p>}
            {server.oauth_client_configured && <p>{t('Pre-registered OAuth client configured')}</p>}
            {oauthLink?.id === server.id && <p><a href={oauthLink.url} target="_blank" rel="noopener noreferrer">{t('Open OAuth authorization')}</a> · {t('Waiting for authorization and tool review…')}</p>}
            {server.enabled && <div className="user-mcp-tools">
              {server.tools.map((tool: UserMcpTool) => <label key={tool.name}>
                <code>{tool.name}</code>
                <select
                  aria-label={t('Policy for {tool}', { tool: tool.name })}
                  value={tool.policy}
                  disabled={Boolean(busy)}
                  onChange={(event) => void act(`${server.id}:${tool.name}`, () => setUserMcpToolPolicy(
                    projectId, server.id, tool.name, event.target.value as UserMcpTool['policy'],
                  ))}
                >
                  <option value="allow">{t('Allow')}</option>
                  <option value="ask">{t('Ask each time')}</option>
                  <option value="deny">{t('Deny')}</option>
                </select>
              </label>)}
            </div>}
            <div className="user-mcp-actions">
              {server.auth_mode === 'oauth' && <button type="button" disabled={Boolean(busy) || !oauthSetup?.redirect_uri} onClick={() => void beginOAuth(server.id)}>{t('Connect with OAuth')}</button>}
              <button type="button" disabled={Boolean(busy) || (server.auth_mode === 'oauth' && !server.credential_configured)} onClick={() => void act(`${server.id}:review`, () => reviewUserMcp(projectId, server.id))}>{t('Review tools')}</button>
              <button type="button" disabled={Boolean(busy)} onClick={() => void act(`${server.id}:revoke`, () => revokeUserMcp(projectId, server.id))}>{t('Revoke')}</button>
            </div>
          </article>)}
        </div>
      </>}
    </section>
  )
}
