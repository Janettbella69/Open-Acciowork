import { useCallback, useEffect, useState } from 'react'
import { getSkillPackage, getSkillPackageFile, listSkillVersions, selectPrivateSkill, skillPackageDownloadUrl } from '../api/client'
import { requireSkillPackage, type SkillPackageView, type SkillRepositorySelection } from '../api/skillRepository'
import type { SkillVersion } from '../api/types'
import { useT } from '../i18n'
import SkillRepositoryFiles from './SkillRepositoryFiles'
import { useCollaborationRequest } from './useCollaborationRequest'

type Props = { skill: SkillVersion; canManage: boolean; onUnavailable: () => void; onChanged: () => void }
export default function PrivateSkillPackage({ skill, canManage, onUnavailable, onChanged }: Props) {
  const t = useT()
  const [versions, setVersions] = useState<SkillVersion[]>([])
  const [hash, setHash] = useState(skill.content_hash)
  const [detail, setDetail] = useState<SkillPackageView | null>(null)
  const [operation, setOperation] = useState<SkillRepositorySelection['operation'] | null>(null)
  const { run, busy, error } = useCollaborationRequest(onUnavailable)
  const refresh = useCallback(() => run(async (signal) => {
    const [items, value] = await Promise.all([listSkillVersions(skill.id, signal), getSkillPackage(skill.name, hash, signal)])
    requireSkillPackage(skill.id, hash, value)
    if (items.some((row) => row.id !== skill.id || row.source !== 'tenant')) throw new Error('Private Skill response did not match this repository.')
    return { items, value }
  }, ({ items, value }) => { setVersions(items); setDetail(value); setOperation(null) }), [skill.id, skill.name, hash, run])
  useEffect(() => { void refresh() }, [refresh])
  const read = useCallback((path: string, signal: AbortSignal) => getSkillPackageFile(skill.name, hash, path, signal), [skill.name, hash])
  function confirm() {
    if (!detail || !operation) return
    const payload = { operation, content_hash: hash, expected_target: detail.current_target }
    void run(async (signal) => {
      const changed = await selectPrivateSkill(skill.name, payload, signal)
      if (changed.skill.id !== skill.id) throw new Error('Private Skill response did not match this repository.')
      const value = await getSkillPackage(skill.name, hash, signal)
      requireSkillPackage(skill.id, hash, value)
      return value
    }, (value) => { setDetail(value); setOperation(null); onChanged() })
  }
  return <section className="private-skill-detail" aria-label={t('Private package versions')}>
    {error && <p role="alert">{t(error)}</p>}
    <div className="private-skill-actions"><label>{t('Package version')}<select disabled={busy || operation !== null} value={hash} onChange={(e) => { setHash(e.target.value); setDetail(null) }}>{versions.map((item) => <option key={item.content_hash} value={item.content_hash}>v{item.version} · {item.content_hash.slice(0, 12)}</option>)}</select></label><button type="button" disabled={busy} onClick={() => void refresh()}>{t('Reload package and current selection')}</button></div>
    {detail && <>
      <h3>{detail.skill.name} · v{detail.skill.version}</h3><p>{detail.skill.description}</p>
      <small>SHA-256 {detail.skill.content_hash}</small>
      <p>{t('Current repository selection')}: {detail.current_target.current_hash} · {t(detail.current_target.enabled ? 'Enabled' : 'Disabled')}</p>
      <a href={skillPackageDownloadUrl(skill.name, hash)} download rel="noreferrer">{t('Download this package version')}</a>
      <SkillRepositoryFiles key={hash} files={detail.files} read={read} onUnavailable={onUnavailable} />
      {canManage && <>
        <div className="private-skill-actions">{detail.current_target.current_hash === hash
          ? <button type="button" disabled={busy} onClick={() => setOperation(detail.current_target.enabled ? 'disable' : 'enable')}>{t(detail.current_target.enabled ? 'Review disablement' : 'Review enablement')}</button>
          : <button type="button" disabled={busy} onClick={() => setOperation('rollback')}>{t('Review version restore')}</button>}</div>
        {operation && <div className="private-skill-review"><strong>{t(operation === 'rollback' ? 'Select this saved package version?' : operation === 'enable' ? 'Enable this reviewed package for future Runs?' : 'Disable this package for future Runs?')}</strong><p>{detail.skill.name} · v{detail.skill.version}</p><small>SHA-256 {hash}</small><p>{t('Existing Runs keep their pinned packages. Enabling a published package is a separate action.')}</p><div className="private-skill-actions"><button type="button" disabled={busy} onClick={confirm}>{t('Confirm repository change')}</button><button type="button" disabled={busy} onClick={() => setOperation(null)}>{t('Cancel')}</button></div></div>}
      </>}
    </>}
  </section>
}
