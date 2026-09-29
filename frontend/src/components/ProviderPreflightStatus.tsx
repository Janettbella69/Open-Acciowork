import { useScopedEvents } from '../api/useScopedEvents'
import { useLanguage, useT } from '../i18n'

export default function ProviderPreflightStatus({ providerId, onSettled }: {
  providerId: string
  onSettled: () => void
}) {
  const t = useT()
  const language = useLanguage()
  const history = useScopedEvents({
    scope: 'provider', id: providerId, onReady: onSettled,
    onEvent: (event) => { if (event.type !== 'provider.preflight.started') onSettled() },
  })
  const latest = history.events.at(-1)
  return (
    <section aria-label={t('Latest preflight')}>
      {!latest && history.status === 'loading' && <p>{t('Loading…')}</p>}
      {!latest && history.status === 'live' && <p>{t('No recorded preflight activity.')}</p>}
      {latest && <p className="provider-row-status" role="status">
        <span data-tone={latest.data.status === 'failed' ? 'error' : latest.data.status === 'passed' ? 'ok' : 'neutral'}>
          {t(latest.data.reason === 'interrupted' ? 'Preflight result unconfirmed' : latest.data.status === 'started' ? 'Preflighting…' : latest.data.status === 'passed' ? 'Preflight passed' : 'Preflight failed')}
          {' · '}{new Date(latest.timestamp).toLocaleString(language === 'zh-CN' ? 'zh-CN' : 'en')}
          {typeof latest.data.code === 'string' && <> · <code>{latest.data.code}</code></>}
          {typeof latest.data.reason === 'string' && <> · {t(latest.data.reason)}</>}
        </span>
      </p>}
      {history.status === 'reconnecting' && <small>{t('Reconnecting to updates…')}</small>}
      {history.error && <p role="alert">{t(history.error)} <button type="button" onClick={history.refresh}>{t('Refresh')}</button></p>}
    </section>
  )
}
