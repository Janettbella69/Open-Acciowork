import type { OphiraEvent } from '../api/types'
import { useT } from '../i18n'
import { nativeTaskProgress } from './nativeTasks'

export default function NativeTaskProgress({ events, runId }: { events: OphiraEvent[]; runId: string }) {
  const t = useT()
  const tasks = nativeTaskProgress(events, runId)
  if (!tasks.length) return null
  return (
    <section className="conversation-native-tasks" aria-label={t('Task progress')}>
      <h3>{t('Task progress')}</h3>
      <ol>
        {tasks.map((task) => (
          <li key={task.id} data-status={task.status}>
            <span aria-hidden="true">{task.status === 'completed' ? '✓' : task.status === 'in_progress' ? '◉' : '○'}</span>
            <span>{task.title}</span>
            <small>{t(task.status === 'in_progress' ? 'In progress' : task.status === 'completed' ? 'Completed' : 'Pending')}</small>
          </li>
        ))}
      </ol>
    </section>
  )
}
