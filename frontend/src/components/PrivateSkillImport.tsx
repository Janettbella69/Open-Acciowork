import { useCallback, useEffect, useMemo, useState } from 'react'
import { getSkillImport, getSkillImportFile, reviewSkillImport, skillImportDownloadUrl } from '../api/client'
import { requireSkillImportScope, type SkillImportView, type SkillRepositoryCapabilities } from '../api/skillRepository'
import { proposalStateLabels } from '../api/skillProposals'
import { useT } from '../i18n'
import SkillRepositoryFiles from './SkillRepositoryFiles'
import { useCollaborationRequest, useCommentRequestKey } from './useCollaborationRequest'

type Props = { identifier: string; capabilities: SkillRepositoryCapabilities; onUnavailable: () => void; onChanged: () => void }
export default function PrivateSkillImport({ identifier, capabilities, onUnavailable, onChanged }: Props) {
  const t = useT()
  const [detail, setDetail] = useState<SkillImportView | null>(null)
  const [note, setNote] = useState('')
  const [decision, setDecision] = useState<'approve' | 'reject' | null>(null)
  const [reviewed, setReviewed] = useState(false)
  const { run, busy, error } = useCollaborationRequest(onUnavailable)
  const keys = useCommentRequestKey()
  const { identity_mode, tenant_id, actor_id, can_manage } = capabilities
  const currentScope = useMemo(() => ({ identity_mode, tenant_id, actor_id, can_manage }), [identity_mode, tenant_id, actor_id, can_manage])
  const refresh = useCallback(() => run(async (signal) => {
    const value = await getSkillImport(identifier, signal)
    requireSkillImportScope(currentScope, value, identifier)
    return value
  }, (value) => { setDetail(value); setDecision(null); setReviewed(false) }), [identifier, currentScope, run])
  useEffect(() => { void refresh() }, [refresh])
  const read = useCallback((path: string, signal: AbortSignal) => getSkillImportFile(identifier, path, signal), [identifier])
  function confirm() {
    if (!detail || !decision || (decision === 'approve' && (!reviewed || !detail.can_publish)) || (decision === 'reject' && !note.trim())) return
    const data = { decision, revision: detail.import.revision, content_hash: detail.import.skill.content_hash, note: note.trim(), ...(decision === 'approve' ? { expected_target: detail.current_target } : {}) }
    void run(async (signal) => {
      const value = await reviewSkillImport(identifier, { ...data, request_id: keys.forPayload(data) }, signal)
      requireSkillImportScope(capabilities, value, identifier)
      return value
    }, (value) => { setDetail(value); setDecision(null); keys.clear(); onChanged() })
  }
  return <section className="private-skill-detail" aria-label={t('Private Skill import review')}>
    {error && <p role="alert">{t(error)}</p>}
    <button type="button" disabled={busy} onClick={() => void refresh()}>{t('Reload import and current selection')}</button>
    {detail && <>
      <h3>{detail.import.skill.name} · v{detail.import.skill.version}</h3><p>{detail.import.skill.description}</p>
      <p>{t(proposalStateLabels[detail.import.state])} · {detail.import.uploaded_by.id} · {new Date(detail.import.created_at).toLocaleString()}</p>
      <small>SHA-256 {detail.import.skill.content_hash}</small>
      <p>{t('Uploaded file')}: {detail.import.filename}</p>
      <ul className="private-skill-checks" aria-label={t('Package scan results')}>{detail.import.scan.checks.map((check) => <li key={check.name} data-status={check.status}>{check.status === 'passed' ? '✓' : check.status === 'failed' ? '✗' : '△'} {check.name}: {check.detail}</li>)}</ul>
      <SkillRepositoryFiles key={detail.import.skill.content_hash} files={detail.import.files} read={read} download={(path) => skillImportDownloadUrl(identifier, path)} onUnavailable={onUnavailable} />
      {detail.import.state === 'pending' && <>
        <label>{t('Review note')}<textarea maxLength={4000} value={note} disabled={busy} onChange={(e) => { setNote(e.target.value); setDecision(null) }} /></label>
        <label className="private-skill-attestation"><input type="checkbox" checked={reviewed} disabled={busy || !detail.can_publish} onChange={(e) => { setReviewed(e.target.checked); setDecision(null) }} />{t('I reviewed the package files, scan results, and current selection.')}</label>
        <div className="private-skill-actions"><button type="button" disabled={busy || !detail.can_publish || !reviewed} onClick={() => setDecision('approve')}>{t('Review publication')}</button><button type="button" disabled={busy || !note.trim()} onClick={() => setDecision('reject')}>{t('Review rejection')}</button></div>
        {!detail.can_publish && <p>{t('This package must pass every blocking scan before publication.')}</p>}
        {decision && <div className="private-skill-review">
          <strong>{t(decision === 'approve' ? 'Publish this exact package, disabled?' : 'Reject this imported package?')}</strong>
          <p>{detail.import.skill.name} · v{detail.import.skill.version}</p><small>SHA-256 {detail.import.skill.content_hash}</small>
          <p>{t('Current repository selection')}: {detail.current_target.current_hash ?? t('No published version')} · {t('Revision')} {detail.current_target.generation}</p>
          <p>{note}</p><p>{t('Existing Runs keep their pinned packages. Enabling a published package is a separate action.')}</p>
          <button type="button" disabled={busy} onClick={confirm}>{t(busy ? 'Saving…' : decision === 'approve' ? 'Confirm private publication' : 'Confirm import rejection')}</button>
        </div>}
      </>}
      {detail.import.history.length > 0 && <details><summary>{t('Import review history')}</summary><ol>{detail.import.history.map((row) => <li key={row.review_revision}><strong>{row.reviewer.id} · {t(row.outcome === 'interrupted' ? 'Interrupted review' : proposalStateLabels[row.outcome])}</strong><small>{new Date(row.reviewed_at).toLocaleString()} · {t('Revision')} {row.review_revision}</small><p>{row.note}</p></li>)}</ol></details>}
    </>}
  </section>
}
