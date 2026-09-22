import { useCallback, useEffect, useState } from 'react'
import { getProjectAccess, grantProjectAccess, revokeProjectAccess } from '../api/client'
import { requireCollaborationScope, type CollaborationCapabilities, type ProjectAccess, type ProjectRole } from '../api/collaboration'
import { useT } from '../i18n'
import { useCollaborationRequest } from './useCollaborationRequest'

type Grantable = Exclude<ProjectRole, 'owner'>
const descriptions: Record<ProjectRole, string> = {
  owner: 'Owns this project and manages access.',
  admin: 'Manages project work, reviews, and members.',
  member: 'Runs tasks, edits project work, and comments.',
  reviewer: 'Reviews commercial actions and comments.',
  viewer: 'Reads project work and discussions.',
}
type Review = { action: 'grant' | 'revoke'; principalId: string; role: Grantable; revision: number }

export default function ProjectMembers({ capabilities, onUnavailable }: {
  capabilities: CollaborationCapabilities; onUnavailable: () => void
}) {
  const t = useT()
  const projectId = capabilities.project_id
  const { run, busy, error } = useCollaborationRequest(onUnavailable)
  const [access, setAccess] = useState<ProjectAccess | null>(null)
  const [principalId, setPrincipalId] = useState('')
  const [role, setRole] = useState<Grantable>('member')
  const [review, setReview] = useState<Review | null>(null)
  const [notice, setNotice] = useState('')
  const refresh = useCallback(() => run(async (signal) => {
    const result = await getProjectAccess(projectId, signal)
    requireCollaborationScope(projectId, result)
    return result
  }, (result) => { setAccess(result); setReview(null) }), [projectId, run])
  useEffect(() => { void refresh() }, [refresh])

  const save = () => {
    if (!review) return
    void run(async (signal) => {
      const result = review.action === 'grant'
        ? await grantProjectAccess(projectId, { principal_id: review.principalId, role: review.role, expected_revision: review.revision }, signal)
        : await revokeProjectAccess(projectId, review.principalId, review.revision, signal)
      requireCollaborationScope(projectId, result)
      // Demoting/removing oneself also removes access to this private management view.
      const own = result.memberships[capabilities.actor_id]
      if (!own || own.role !== capabilities.role) { onUnavailable(); return null }
      return result
    }, (result) => {
      if (result) { setAccess(result); setReview(null); setPrincipalId(''); setNotice('Project membership saved.') }
    })
  }
  return <section aria-label={t('Project members')}>
    <p>{t('Use the account ID assigned by your organization. The account can open this project after signing in with its assigned role.')}</p>
    <button type="button" disabled={busy} onClick={() => void refresh()}>{t('Refresh members')}</button>
    {error && <p role="alert">{t(error)} {t('Refresh members before reviewing another change.')}</p>}
    {notice && <p role="status">{t(notice)}</p>}
    {!access && busy && <p role="status">{t('Loading members…')}</p>}
    {access && <>
      <ul className="collaboration-members">{Object.values(access.memberships).map((member) => <li key={member.principal_id}>
        <strong>{member.principal_id}</strong><span>{t(member.role)} · {t(descriptions[member.role])}</span>
        <small>{t('Access granted by {name}', { name: member.granted_by })} · {new Date(member.granted_at).toLocaleString()}</small>
        {member.principal_id !== access.owner_principal_id && <div className="collaboration-actions">
          <button type="button" disabled={busy} onClick={() => { setPrincipalId(member.principal_id); setRole(member.role === 'owner' || !capabilities.grantable_roles.includes(member.role) ? 'member' : member.role); setReview(null); setNotice('') }}>{t('Change role')}</button>
          <button type="button" disabled={busy} onClick={() => { setReview({ action: 'revoke', principalId: member.principal_id, role: member.role as Grantable, revision: access.revision }); setNotice('') }}>{t('Remove access')}</button>
        </div>}
      </li>)}</ul>
      <form onSubmit={(event) => {
        event.preventDefault()
        if (principalId.trim() && capabilities.grantable_roles.includes(role)) { setReview({ action: 'grant', principalId: principalId.trim(), role, revision: access.revision }); setNotice('') }
      }}>
        <fieldset disabled={busy}><legend>{t('Add or update a member')}</legend>
          <label>{t('Organization account ID')}<input required maxLength={160} value={principalId} onChange={(event) => { setPrincipalId(event.target.value); setReview(null) }} /></label>
          <label>{t('Project role')}<select value={role} onChange={(event) => { setRole(event.target.value as Grantable); setReview(null) }}>{capabilities.grantable_roles.map((value) => <option key={value} value={value}>{t(value)}</option>)}</select></label>
          <p>{t(descriptions[role])}</p>
          <button type="submit" disabled={!principalId.trim() || principalId.trim() === access.owner_principal_id}>{t('Review access change')}</button>
        </fieldset>
      </form>
      {review && <div className="collaboration-review" aria-label={t('Review access change')}>
        <strong>{review.principalId}</strong>
        <p>{review.action === 'grant' ? t('Set project access to {role}.', { role: t(review.role) }) : t('Remove this account’s access to the project and its discussions.')}</p>
        {review.principalId === capabilities.actor_id && <p>{t('This change affects your own project access.')}</p>}
        <button type="button" disabled={busy} onClick={save}>{t(busy ? 'Saving…' : 'Confirm access change')}</button>
        <button type="button" disabled={busy} onClick={() => setReview(null)}>{t('Cancel')}</button>
      </div>}
    </>}
  </section>
}
