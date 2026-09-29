import type { OpportunityProject, Schedule } from '../api/types'
import { useScopedEvents } from '../api/useScopedEvents'
import { useLanguage, useT } from '../i18n'

const labels: Record<string, string> = {
  'schedule.fired': 'Triggered', 'schedule.skipped': 'Skipped',
  'schedule.completed': 'Completed', 'schedule.failed': 'Failed', 'schedule.cancelled': 'Cancelled',
  missed_run_policy: 'Skipped by missed-run policy', active_run: 'A previous run is still active',
  provider_unavailable: 'Provider unavailable', authorization_denied: 'Authorization denied',
  policy_denied: 'Blocked by policy',
}

export default function ScheduleProjectHistory({ project, schedules, live, onChanged, onUnavailable }: {
  project: OpportunityProject
  schedules: Schedule[]
  live: boolean
  onChanged: () => void
  onUnavailable: () => void
}) {
  const t = useT()
  const language = useLanguage()
  const history = useScopedEvents({
    scope: 'project', id: project.id, live, onUnavailable, onReady: onChanged,
    onEvent: (event) => { if (event.type.startsWith('schedule.')) onChanged() },
  })
  const events = history.events.filter((event) => event.type.startsWith('schedule.')).slice(-10).reverse()
  return <section aria-label={t('Recent schedule activity for {project}', { project: project.name })}>
    <h3>{project.name}</h3>
    {history.error && <p role="alert">{t(history.error)}</p>}
    {history.status === 'loading' && <p>{t('Loading…')}</p>}
    {history.status === 'reconnecting' && <p role="status">{t('Reconnecting to updates…')}</p>}
    {!events.length && history.status !== 'loading' && !history.error && <p>{t('No recent schedule activity.')}</p>}
    <button type="button" onClick={history.refresh}>{t('Refresh')}</button>
    <ol>
      {events.map((event) => {
        const scheduleId = typeof event.data.schedule_id === 'string' ? event.data.schedule_id : ''
        const runId = typeof event.data.run_id === 'string' ? event.data.run_id : event.run_id
        const schedule = schedules.find((item) => item.id === scheduleId)
        return <li key={event.id}>
          <strong>{t(labels[event.type] ?? event.type)}</strong> · {schedule?.prompt ?? scheduleId}
          <p><time dateTime={event.timestamp}>{new Date(event.timestamp).toLocaleString(language === 'zh-CN' ? 'zh-CN' : 'en')}</time>
            {typeof event.data.reason === 'string' && <> · {t(labels[event.data.reason] ?? event.data.reason)}</>}
            {typeof event.data.scheduled_for === 'string' && <> · {t('Scheduled for')} {new Date(event.data.scheduled_for).toLocaleString(language === 'zh-CN' ? 'zh-CN' : 'en')}</>}
            {runId && <> · <a href={`/app?project=${encodeURIComponent(project.id)}&run=${encodeURIComponent(runId)}`}>{t('Open run')}</a></>}
          </p>
        </li>
      })}
    </ol>
    <small>{t('Showing recent activity. Older events may not appear here.')}</small>
  </section>
}
