import { useEffect, useState } from 'react'
import { checkConnectorHealth, errorMessage, listConnectors } from '../api/client'
import type { Connector } from '../api/types'
import { useT } from '../i18n'
import './connectors-page.css'

function healthLabel(health: Connector['health']): string {
  if (health === 'ok') return 'Connected'
  if (health === 'no_credential') return 'Credential required'
  if (health === 'unreachable') return 'Unreachable'
  return 'Not checked'
}

export default function ConnectorsPage() {
  const t = useT()
  const [connectors, setConnectors] = useState<Connector[]>([])
  const [loading, setLoading] = useState(true)
  const [checking, setChecking] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    void listConnectors()
      .then((items) => { if (active) setConnectors(items) })
      .catch((loadError) => { if (active) setError(errorMessage(loadError)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  const checkHealth = async (connector: Connector) => {
    setChecking(connector.id)
    setError('')
    try {
      const updated = await checkConnectorHealth(connector.id)
      setConnectors((current) => current.map((item) => item.id === updated.id ? updated : item))
    } catch (healthError) {
      setError(errorMessage(healthError))
    } finally {
      setChecking('')
    }
  }

  const connected = connectors.filter((connector) => connector.health === 'ok').length

  return (
    <section className="connectors-content" aria-labelledby="connectors-page-heading">
        <header className="connectors-header">
          <div><h1 id="connectors-page-heading">{t('Connectors')}</h1><p>{t('Connect external services that Agents may request through the same approval and audit boundary.')}</p></div>
          <dl>
            <div><dt>{t('Configured')}</dt><dd>{connectors.length}</dd></div>
            <div><dt>{t('Connected')}</dt><dd>{connected}</dd></div>
          </dl>
        </header>

        <section className="connectors-registry" aria-labelledby="connectors-registry-title">
          <div><span>{t('Runtime integrations')}</span><h2 id="connectors-registry-title">{t('Connector registry')}</h2></div>
          {error && <p className="connectors-error" role="alert">{error}</p>}
          {loading ? (
            <div className="connectors-empty"><strong>{t('Loading connectors…')}</strong></div>
          ) : connectors.length ? (
            <div className="connectors-list">
              {connectors.map((connector) => (
                <article key={connector.id}>
                  <div className="connector-mark" aria-hidden="true">{connector.type.slice(0, 2).toUpperCase()}</div>
                  <div className="connector-copy">
                    <header><div><h3>{connector.display_name}</h3><p>{connector.type} · {connector.scope}</p></div><span data-health={connector.health}>{t(healthLabel(connector.health))}</span></header>
                    <dl>
                      <div><dt>{t('Credential')}</dt><dd>{connector.secret_env ?? t('No credential required')}</dd></div>
                      <div><dt>{t('Endpoint')}</dt><dd>{connector.endpoint ?? t('Managed by the connector')}</dd></div>
                      <div><dt>{t('Last checked')}</dt><dd>{connector.last_checked_at ? new Date(connector.last_checked_at).toLocaleString() : t('Never')}</dd></div>
                    </dl>
                  </div>
                  <button type="button" disabled={checking === connector.id} onClick={() => void checkHealth(connector)}>
                    {checking === connector.id ? t('Checking…') : t('Check connection')}
                  </button>
                </article>
              ))}
            </div>
          ) : (
            <div className="connectors-empty">
              <strong>{t('No connectors configured.')}</strong>
              <p>{t('Connector records appear here after an operator registers an integration on the backend.')}</p>
              <a href="/app/settings#plugins">{t('Review integration permissions')} <span aria-hidden="true">→</span></a>
            </div>
          )}
        </section>
    </section>
  )
}
