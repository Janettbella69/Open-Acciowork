import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import {
  createIntent,
  errorMessage,
  listIntents,
  listNotifications,
  listProjects,
  markAllNotificationsRead,
  markNotificationRead,
  pauseIntent,
  resumeIntent,
} from '../api/client'
import type { Notification, OpportunityProject, StandingIntent } from '../api/types'
import { useT } from '../i18n'
import Brand from './Brand'
import WorkspaceNav from './WorkspaceNav'
import './workspace-composer.css'
import './inbox-page.css'

export default function InboxPage() {
  const t = useT()
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [intents, setIntents] = useState<StandingIntent[]>([])
  const [projects, setProjects] = useState<OpportunityProject[]>([])
  const [error, setError] = useState('')
  const [intentText, setIntentText] = useState('')
  const [projectId, setProjectId] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [updating, setUpdating] = useState('')
  const mountedRef = useRef(false)
  const refreshInFlightRef = useRef(false)
  const actionInFlightRef = useRef('')

  const refresh = useCallback(async (silent = false) => {
    if (refreshInFlightRef.current) return
    refreshInFlightRef.current = true
    if (!silent && mountedRef.current) setRefreshing(true)
    const results = await Promise.allSettled([
      listNotifications(),
      listIntents(),
      listProjects(),
    ])
    if (mountedRef.current) {
      const [notificationResult, intentResult, projectResult] = results
      if (notificationResult.status === 'fulfilled') setNotifications(notificationResult.value)
      if (intentResult.status === 'fulfilled') setIntents(intentResult.value)
      if (projectResult.status === 'fulfilled') {
        const projectList = projectResult.value
        setProjects(projectList)
        setProjectId((current) => (
          current && projectList.some((project) => project.id === current)
            ? current
            : projectList[0]?.id ?? ''
        ))
      }
      const failures = results
        .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
        .map((result) => errorMessage(result.reason))
      setError(failures.join(' · '))
      setRefreshing(false)
    }
    refreshInFlightRef.current = false
  }, [])

  useEffect(() => {
    mountedRef.current = true
    void refresh()
    const timer = window.setInterval(() => void refresh(true), 30_000)
    return () => {
      mountedRef.current = false
      window.clearInterval(timer)
    }
  }, [refresh])

  const addIntent = async (event: FormEvent) => {
    event.preventDefault()
    if (!projectId || !intentText.trim() || actionInFlightRef.current) return
    actionInFlightRef.current = 'create-intent'
    setUpdating('create-intent')
    setError('')
    try {
      const created = await createIntent({ project_id: projectId, intent: intentText.trim() })
      setIntents((current) => [created, ...current])
      setIntentText('')
    } catch (createError) {
      setError(errorMessage(createError))
    } finally {
      actionInFlightRef.current = ''
      setUpdating('')
    }
  }

  const markAllRead = async () => {
    if (actionInFlightRef.current) return
    actionInFlightRef.current = 'read-all'
    setUpdating('read-all')
    setError('')
    try {
      await markAllNotificationsRead()
      setNotifications((current) => current.map((item) => ({ ...item, read: true })))
    } catch (readError) {
      setError(errorMessage(readError))
    } finally {
      actionInFlightRef.current = ''
      setUpdating('')
    }
  }

  const markRead = async (item: Notification) => {
    if (actionInFlightRef.current) return
    actionInFlightRef.current = item.id
    setUpdating(item.id)
    setError('')
    try {
      const updated = await markNotificationRead(item.id)
      setNotifications((current) => current.map((entry) => entry.id === updated.id ? updated : entry))
    } catch (readError) {
      setError(errorMessage(readError))
    } finally {
      actionInFlightRef.current = ''
      setUpdating('')
    }
  }

  const toggleIntent = async (item: StandingIntent) => {
    if (actionInFlightRef.current) return
    actionInFlightRef.current = item.id
    setUpdating(item.id)
    setError('')
    try {
      const updated = await (item.enabled ? pauseIntent(item.id) : resumeIntent(item.id))
      setIntents((current) => current.map((entry) => entry.id === updated.id ? updated : entry))
    } catch (toggleError) {
      setError(errorMessage(toggleError))
    } finally {
      actionInFlightRef.current = ''
      setUpdating('')
    }
  }

  const unread = notifications.filter((item) => !item.read).length

  return (
    <div className="inbox-shell">
      <aside className="conversation-sidebar">
        <a className="conversation-sidebar-brand" href="/" aria-label={t('Back to TradeMind website')}><Brand /></a>
        <a className="conversation-new-research" href="/app"><span aria-hidden="true">＋</span> {t('New task')}</a>
        <WorkspaceNav active="inbox" />
      </aside>
      <main className="inbox-main">
        <header className="inbox-header">
          <div>
            <p>{t('Proactive service')}</p>
            <h1>{t('Inbox')}</h1>
            <span>{t('Heartbeat changes, schedule failures, and standing intents land here. Unchanged patrols stay silent.')}</span>
          </div>
          <dl>
            <div><dt>{t('Unread')}</dt><dd>{unread}</dd></div>
            <div><dt>{t('Intents')}</dt><dd>{intents.filter((item) => item.enabled).length}</dd></div>
          </dl>
        </header>
        {error && <p className="inbox-error" role="alert">{error}</p>}

        <section className="inbox-section" aria-labelledby="inbox-notifications-heading">
          <div className="inbox-section-heading">
            <div>
              <span>{t('Notifications')}</span>
              <h2 id="inbox-notifications-heading">{t('Inbox')}</h2>
            </div>
            <div className="inbox-heading-actions">
              <button type="button" disabled={refreshing} onClick={() => void refresh()}>{refreshing ? t('Refreshing…') : t('Refresh')}</button>
              <button type="button" disabled={!unread || Boolean(updating)} onClick={() => void markAllRead()}>{t('Mark all read')}</button>
            </div>
          </div>
          {notifications.length ? (
            <div className="inbox-list">
              {notifications.map((item) => (
                <article key={item.id} data-read={item.read ? 'true' : 'false'} data-severity={item.severity}>
                  <header>
                    <strong>{item.title}</strong>
                    <span>{new Date(item.created_at).toLocaleString()}</span>
                  </header>
                  {item.body && <p>{item.body}</p>}
                  {!item.read && (
                    <button type="button" disabled={Boolean(updating)} onClick={() => void markRead(item)}>
                      {t('Mark read')}
                    </button>
                  )}
                </article>
              ))}
            </div>
          ) : (
            <div className="inbox-empty"><strong>{t('No notifications yet.')}</strong></div>
          )}
        </section>

        <section className="inbox-section" aria-labelledby="inbox-intents-heading">
          <div>
            <span>{t('Standing intents')}</span>
            <h2 id="inbox-intents-heading">{t('Tell me when something changes')}</h2>
          </div>
          <form className="inbox-intent-form" onSubmit={(event) => void addIntent(event)}>
            <label>
              <span>{t('Project')}</span>
              <select value={projectId} onChange={(event) => setProjectId(event.target.value)} aria-label={t('Project')}>
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>{project.name}</option>
                ))}
              </select>
            </label>
            <label>
              <span>{t('Intent')}</span>
              <input
                value={intentText}
                onChange={(event) => setIntentText(event.target.value)}
                placeholder={t('Watch competitor prices on this SKU')}
                aria-label={t('Intent')}
              />
            </label>
            <button type="submit" disabled={!projectId || !intentText.trim() || Boolean(updating)}>{updating === 'create-intent' ? t('Saving…') : t('Authorize intent')}</button>
          </form>
          <div className="inbox-list">
            {intents.map((item) => (
              <article key={item.id}>
                <header>
                  <strong>{item.intent}</strong>
                  <span>{item.enabled ? t('Enabled') : t('Paused')}</span>
                </header>
                <p>{t('Cooldown {hours}h · {count}/{max} triggers', {
                  hours: item.cooldown_hours,
                  count: item.triggered_count,
                  max: item.max_triggers,
                })}</p>
                <button
                  type="button"
                  disabled={Boolean(updating)}
                  onClick={() => void toggleIntent(item)}
                >
                  {item.enabled ? t('Pause') : t('Resume')}
                </button>
              </article>
            ))}
          </div>
        </section>
      </main>
    </div>
  )
}
