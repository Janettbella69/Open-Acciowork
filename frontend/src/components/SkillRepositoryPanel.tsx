import { useCallback, useEffect, useMemo, useState } from 'react'
import { listSkillImports, submitSkillImport } from '../api/client'
import { requireSkillImportScope, type SkillImportView, type SkillRepositoryCapabilities } from '../api/skillRepository'
import { proposalStateLabels } from '../api/skillProposals'
import type { SkillVersion } from '../api/types'
import { useT } from '../i18n'
import PrivateSkillImport from './PrivateSkillImport'
import PrivateSkillPackage from './PrivateSkillPackage'
import { useCollaborationRequest } from './useCollaborationRequest'
import './skill-repository.css'

type Props = { skills: SkillVersion[]; capabilities: SkillRepositoryCapabilities; onUnavailable: () => void; onChanged: () => void }
function Imports({ capabilities, onUnavailable, onChanged }: Props) {
  const t = useT()
  const [items, setItems] = useState<SkillImportView[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [file, setFile] = useState<{ value: File; requestId: string } | null>(null)
  const { run, busy, error } = useCollaborationRequest(onUnavailable)
  const { identity_mode, tenant_id, actor_id, can_manage } = capabilities
  const scope = useMemo(() => ({ identity_mode, tenant_id, actor_id, can_manage }), [identity_mode, tenant_id, actor_id, can_manage])
  const refresh = useCallback(() => run(async (signal) => {
    const values = await listSkillImports(signal)
    values.forEach((value) => requireSkillImportScope(scope, value))
    return values
  }, setItems), [scope, run])
  useEffect(() => { void refresh() }, [refresh])
  function submit() {
    if (!file) return
    void run(async (signal) => {
      const value = await submitSkillImport(file.value, file.requestId, signal)
      requireSkillImportScope(scope, value)
      if (value.import.uploaded_by.id !== scope.actor_id) throw new Error('Private Skill response did not match this repository.')
      return value
    }, (value) => { setItems((current) => [value, ...current.filter((row) => row.import.id !== value.import.id)]); setSelected(value.import.id); setFile(null) })
  }
  return <section aria-label={t('Private Skill imports')}>
    <p>{t('Upload a Skill ZIP to prepare a private review. Inspect every file and scan result before publication.')}</p>
    {error && <p role="alert">{t(error)}</p>}
    <label>{t('Choose Skill ZIP')}<input type="file" accept=".zip,application/zip" disabled={busy} onChange={(e) => { const value = e.target.files?.[0]; setFile(value ? { value, requestId: crypto.randomUUID() } : null); e.target.value = '' }} /></label>
    {file && <p>{file.value.name} · {file.value.size.toLocaleString()} B</p>}
    {file && file.value.size > 2_000_000 && <p role="alert">{t('Skill archives must be no larger than 2 MB.')}</p>}
    <div className="private-skill-actions"><button type="button" disabled={busy || !file || file.value.size > 2_000_000} onClick={submit}>{t('Upload for review')}</button><button type="button" disabled={busy} onClick={() => void refresh()}>{t('Refresh import reviews')}</button></div>
    {items.length === 0 && !busy && <p>{t('No private Skill imports yet.')}</p>}
    <ul className="private-skill-list">{items.map((row) => <li key={row.import.id}><button type="button" aria-expanded={selected === row.import.id} onClick={() => setSelected(selected === row.import.id ? null : row.import.id)}><strong>{row.import.skill.name} · v{row.import.skill.version}</strong><span>{t(proposalStateLabels[row.import.state])} · {row.import.uploaded_by.id}</span></button></li>)}</ul>
    {selected && <PrivateSkillImport key={selected} identifier={selected} capabilities={scope} onUnavailable={onUnavailable} onChanged={() => { void refresh(); onChanged() }} />}
  </section>
}
export default function SkillRepositoryPanel(props: Props) {
  const t = useT()
  const [tab, setTab] = useState<'published' | 'imports'>('published')
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const skills = props.skills.filter((row) => row.source === 'tenant')
  const item = skills.find((row) => row.id === selected)
  return <section className="skill-repository" aria-label={t('Private Skill repository')}>
    <header><h2>{t('Private Skill repository')}</h2><p>{t(props.capabilities.identity_mode === 'local_only' ? 'Private packages for this local installation.' : 'Private packages shared within this deployment organization.')}</p><small>{props.capabilities.actor_id}{props.capabilities.tenant_id && ` · ${props.capabilities.tenant_id}`}</small></header>
    <div className="private-skill-actions" role="group" aria-label={t('Repository views')}><button type="button" aria-pressed={tab === 'published'} onClick={() => setTab('published')}>{t('Published packages')}</button>{props.capabilities.can_manage && <button type="button" aria-pressed={tab === 'imports'} onClick={() => setTab('imports')}>{t('Import reviews')}</button>}</div>
    {tab === 'imports' && props.capabilities.can_manage ? <Imports {...props} /> : <>
      <label>{t('Search private packages')}<input type="search" value={query} onChange={(e) => setQuery(e.target.value)} /></label>
      {!skills.length && <p>{t('No private packages have been published yet.')}</p>}
      <ul className="private-skill-list">{skills.filter((row) => `${row.name} ${row.description}`.toLowerCase().includes(query.trim().toLowerCase())).map((row) => <li key={row.id}><button type="button" aria-expanded={selected === row.id} onClick={() => setSelected(selected === row.id ? null : row.id)}><strong>{row.name} · v{row.version}</strong><span>{t(row.enabled ? 'Enabled' : 'Disabled')} · {row.content_hash.slice(0, 12)}</span></button></li>)}</ul>
      {item && <PrivateSkillPackage key={item.id} skill={item} canManage={props.capabilities.can_manage} onUnavailable={props.onUnavailable} onChanged={props.onChanged} />}
    </>}
  </section>
}
