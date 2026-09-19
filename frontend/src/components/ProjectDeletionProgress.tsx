import type { useProjectDeletion } from '../api/useProjectDeletion'
import { useT } from '../i18n'
import { DeletionScope } from './ProjectDeletionPanel'

export default function ProjectDeletionProgress({ deletion }: { deletion: ReturnType<typeof useProjectDeletion> }) {
  const t = useT()
  const { attempt, action, refresh, retry, resend, dismiss } = deletion
  if (!attempt) return null
  const { handle, operation, phase, accepted, request, error } = attempt
  const dismissible = !action && (operation?.status === 'deleted' || (!accepted && ['missing', 'rejected', 'forbidden'].includes(phase)))
  return <section className="project-deletion project-deletion-progress" aria-label={t('Project deletion progress')}>
    <div className="project-deletion-body">
      <h3>{t('Project deletion progress')}</h3>
      <p>{t('This progress link can be reopened or refreshed. Leaving this page does not cancel a submitted deletion.')}</p>
      <dl className="project-deletion-identifiers"><dt>{t('Project')}</dt><dd>{handle.projectId}</dd><dt>{t('Deletion operation')}</dt><dd>{handle.operationId}</dd></dl>
      <p role="status">{t(action === 'submit' ? 'Submitting deletion request…' : action === 'retry' ? 'Retrying the same deletion…'
        : operation?.status === 'deleted' ? 'Application-controlled project content has been deleted.'
          : operation?.status === 'blocked' ? 'Deletion is blocked. Erasure is not complete.'
            : operation?.status === 'deleting' ? 'Deletion is in progress. Erasure is not yet complete.'
              : action === 'query' ? 'Checking deletion status…'
                : phase === 'missing' ? 'No deletion operation was found. Submission and completion are not confirmed.'
                  : phase === 'forbidden' ? 'Deletion progress is unavailable with your current access. Private review details have been cleared.'
                    : phase === 'rejected' ? 'Deletion did not start. Review the project again before confirming.'
                      : 'The deletion outcome is not confirmed. Check the same operation before taking another action.')}</p>
      {error && <p role="alert">{t(error)}</p>}
      {operation && <>
        {operation.error_message && <p role="alert">{t(operation.error_message)}</p>}
        {operation.error_code && <small>{t('Error code')}: <code>{operation.error_code}</code></small>}
        <p>{t('Last updated')}: <time dateTime={operation.updated_at}>{new Date(operation.updated_at).toLocaleString()}</time></p>
        <DeletionScope domains={operation.domains} retained={operation.retained} />
      </>}
      {phase === 'missing' && !request && <p>{t('Reopen the project to prepare a new deletion review. No deletion is inferred from a missing operation.')}</p>}
      <div className="project-deletion-actions">
        <button type="button" disabled={!!action} onClick={() => void refresh()}>{t('Refresh deletion status')}</button>
        {operation && ['deleting', 'blocked'].includes(operation.status) && operation.can_retry && <button type="button" disabled={!!action} onClick={() => void retry()}>{t('Retry this deletion')}</button>}
        {phase === 'missing' && !accepted && request && <button type="button" disabled={!!action} onClick={resend}>{t('Resend the same confirmed request')}</button>}
        {dismissible && <button type="button" onClick={dismiss}>{t(operation?.status === 'deleted' ? 'Dismiss deletion result' : 'Close unconfirmed deletion request')}</button>}
      </div>
    </div>
  </section>
}
