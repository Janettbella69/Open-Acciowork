import { useT } from '../i18n'
import { permissionLifecycleState, type PermissionLifecycle } from '../permissionLifecycle'

export default function DurablePermissionNotice({ permission, busy, onResume, onCancel, onRefresh }: {
  permission: PermissionLifecycle
  busy?: boolean
  onResume?: () => void
  onCancel: () => void
  onRefresh?: () => void
}) {
  const t = useT()
  if (!permission.durable) return null
  const state = permissionLifecycleState(permission)
  const message = state === 'pending'
    ? 'This approval is saved across disconnects. Approve only the exact tool input before it expires.'
    : state === 'resolved'
      ? 'Decision saved, awaiting continuation. Continue to apply the saved approval or denial to this tool.'
      : state === 'expired'
        ? 'This approval has expired. It cannot approve or resume work; cancel this run and request a new task.'
        : 'This approval cannot be acted on in its current state. Refresh to check its recorded outcome.'
  return <div className="permission-request-details">
    <p role="status">{t(message)}</p>
    {permission.expiresAt && <p>{t('Approval expires: {date}', { date: new Date(permission.expiresAt).toLocaleString() })}</p>}
    {state !== 'pending' && <div className="mobile-actions">
      {state === 'resolved' && <button type="button" disabled={busy || !onResume} onClick={onResume}>{t('Continue saved decision')}</button>}
      <button type="button" disabled={busy} onClick={onCancel}>{t('Cancel run')}</button>
      {onRefresh && <button type="button" disabled={busy} onClick={onRefresh}>{t('Refresh approvals')}</button>}
    </div>}
  </div>
}
