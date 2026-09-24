import { useEffect, useState } from 'react'
import {
  errorMessage, getPluginImport, getSkillRepositoryCapabilities,
  listPluginImports, submitPluginImport,
} from '../api/client'
import { requirePluginImportScope, type PluginImportRecord, type PluginImportView } from '../api/pluginImports'
import type { SkillRepositoryCapabilities } from '../api/skillRepository'
import { useT } from '../i18n'
import './plugin-imports.css'

const MAX_ZIP_BYTES = 2_000_000

function mcpLink(plugin: string, name: string, url: string): string {
  return `/app/plugins/connectors?${new URLSearchParams({
    plugin_mcp_name: name, plugin_mcp_id: `plugin_${plugin}_${name}`, plugin_mcp_url: url,
  })}`
}

export default function PluginImportsPanel() {
  const t = useT()
  const [capabilities, setCapabilities] = useState<SkillRepositoryCapabilities | null>(null)
  const canManage = capabilities?.can_manage ?? false
  const [records, setRecords] = useState<PluginImportRecord[]>([])
  const [selected, setSelected] = useState('')
  const [detail, setDetail] = useState<PluginImportView | null>(null)
  const [file, setFile] = useState<{ blob: File; requestId: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    void getSkillRepositoryCapabilities().then(async (capabilities) => {
      if (!capabilities.can_manage || !active) return
      const next = await listPluginImports()
      next.forEach((record) => requirePluginImportScope(capabilities, record))
      if (active) { setCapabilities(capabilities); setRecords(next) }
    }).catch((cause: unknown) => { if (active) setError(errorMessage(cause)) })
    return () => { active = false }
  }, [])

  const show = async (identifier: string) => {
    if (!capabilities) return
    if (identifier === selected) { setSelected(''); setDetail(null); return }
    setSelected(identifier); setDetail(null); setError('')
    try {
      const next = await getPluginImport(identifier)
      requirePluginImportScope(capabilities, next.import, identifier)
      setDetail(next)
    } catch (cause) { setError(errorMessage(cause)) }
  }

  const upload = async () => {
    if (!file || !capabilities || busy || file.blob.size > MAX_ZIP_BYTES) return
    setBusy(true); setError('')
    try {
      const result = await submitPluginImport(file.blob, file.requestId)
      requirePluginImportScope(capabilities, result.import)
      if (result.import.uploaded_by.id !== capabilities.actor_id) {
        throw new Error('Plugin import response did not match this repository.')
      }
      setRecords((current) => [result.import, ...current.filter((item) => item.id !== result.import.id)])
      setSelected(result.import.id)
      setDetail(result)
      setFile(null)
    } catch (cause) { setError(errorMessage(cause)) }
    finally { setBusy(false) }
  }

  if (!canManage && !error) return null
  return <section className="plugin-imports" aria-label={t('Plugin package imports')}>
    <header><h2>{t('Import a plugin package')}</h2>
      <p>{t('Upload a Claude plugin ZIP. Each Skill enters private review; MCP addresses need a separate project review. Agent definitions are ignored.')}</p>
    </header>
    {error && <p role="alert">{error}</p>}
    {canManage && <>
      <label>{t('Choose plugin ZIP')}
        <input type="file" accept=".zip,application/zip" disabled={busy}
          onChange={(event) => {
            const blob = event.target.files?.[0]
            setFile(blob ? { blob, requestId: crypto.randomUUID() } : null)
            event.target.value = ''
          }} />
      </label>
      {file && <p>{file.blob.name} · {file.blob.size.toLocaleString()} B</p>}
      {file && file.blob.size > MAX_ZIP_BYTES && <p role="alert">{t('Plugin ZIP must be no larger than 2 MB.')}</p>}
      <button type="button" onClick={() => void upload()}
        disabled={!file || busy || file.blob.size > MAX_ZIP_BYTES}>{t('Inspect plugin package')}</button>
      <div className="plugin-imports-list">
        {!records.length && <p>{t('No plugin packages imported yet.')}</p>}
        {records.map((item) => <button type="button" key={item.id}
          aria-expanded={selected === item.id} onClick={() => void show(item.id)}>
          <strong>{item.name}{item.version ? ` · ${item.version}` : ''}</strong>
          <span>{item.skills.length} {t('Skills')} · {item.mcp_servers.length} MCP</span>
        </button>)}
      </div>
      {selected && !detail && !error && <p role="status">{t('Loading plugin review…')}</p>}
      {detail?.import.id === selected && <div className="plugin-imports-detail">
        <p>{detail.import.description}</p>
        <p>{t('Archive SHA-256')}: <code>{detail.import.archive_hash}</code></p>
        <h3>{t('Skills awaiting individual review')}</h3>
        {!detail.skill_states.length && <p>{t('This plugin contains no importable Skills.')}</p>}
        {detail.skill_states.map((item) => <article key={item.import_id}>
          <div><strong>{item.path}</strong><small>{item.skill_id} · {t(item.state)} · {item.scan_ok ? t('Scan passed') : t('Scan failed')}</small></div>
          <a href={`/app/plugins/skills?import=${encodeURIComponent(item.import_id)}`}>
            {t('Review Skill files and decision')}
          </a>
        </article>)}
        <h3>{t('MCP addresses requiring project review')}</h3>
        {!detail.import.mcp_servers.length && <p>{t('No compatible remote MCP addresses found.')}</p>}
        {detail.import.mcp_servers.map((item) => <article key={item.name}>
          <div><strong>{item.name}</strong><small>{item.url}</small>
            {item.credentials_required && <small>{t('Enter credentials separately; the ZIP credentials were not imported.')}</small>}</div>
          <a href={mcpLink(detail.import.name, item.name, item.url)}>{t('Set up in a project')}</a>
        </article>)}
        <h3>{t('Ignored plugin components')}</h3>
        {!detail.import.ignored_agents.length && !detail.import.ignored_components.length
          && <p>{t('No ignored components.')}</p>}
        {detail.import.ignored_agents.length > 0 && <p>{t('Agent definitions were ignored')}: {detail.import.ignored_agents.join(', ')}</p>}
        {detail.import.ignored_components.length > 0 && <p>{detail.import.ignored_components.join(', ')}</p>}
      </div>}
    </>}
  </section>
}
