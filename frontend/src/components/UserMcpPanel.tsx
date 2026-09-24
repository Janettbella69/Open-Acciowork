import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import {
  addCatalogMcp,
  errorMessage,
  listProjects,
  listUserMcp,
  listUserMcpCatalog,
  reviewUserMcp,
  revokeUserMcp,
  saveUserMcp,
  setUserMcpToolPolicy,
} from '../api/client'
import type { OpportunityProject, UserMcpCatalogItem, UserMcpConnector, UserMcpTool } from '../api/types'
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
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [name, setName] = useState(pluginName)
  const [id, setId] = useState(pluginName ? pluginId : '')
  const [url, setUrl] = useState(pluginUrl)
  const [token, setToken] = useState('')
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
    void Promise.all([listUserMcpCatalog(projectId), listUserMcp(projectId)])
      .then(([available, configured]) => {
        if (!active) return
        setCatalog(available)
        setServers(configured)
        setError('')
      })
      .catch((loadError) => { if (active) setError(errorMessage(loadError)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [projectId])

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
      name: name.trim(), url: url.trim(), ...(token ? { bearer_token: token } : {}),
    }))
    if (saved) {
      setName('')
      setId('')
      setUrl('')
      setToken('')
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
          <select aria-label={t('MCP project')} value={projectId} onChange={(event) => setProjectId(event.target.value)} disabled={Boolean(busy)}>
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
            <label>{t('Server ID')}<input value={id} onChange={(event) => setId(event.target.value)} pattern="[a-z][a-z0-9_]{2,63}" placeholder="my_mcp" required /></label>
            <label>{t('HTTPS MCP address')}<input type="url" value={url} onChange={(event) => setUrl(event.target.value)} placeholder="https://mcp.example.com/mcp" required /></label>
            <label>{t('Bearer token (optional)')}<input type="password" value={token} onChange={(event) => setToken(event.target.value)} autoComplete="off" /></label>
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
              <button type="button" disabled={Boolean(busy)} onClick={() => void act(`${server.id}:review`, () => reviewUserMcp(projectId, server.id))}>{t('Review tools')}</button>
              <button type="button" disabled={Boolean(busy)} onClick={() => void act(`${server.id}:revoke`, () => revokeUserMcp(projectId, server.id))}>{t('Revoke')}</button>
            </div>
          </article>)}
        </div>
      </>}
    </section>
  )
}
