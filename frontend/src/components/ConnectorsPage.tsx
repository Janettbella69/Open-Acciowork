import { useEffect, useRef, useState } from 'react'
import { checkConnectorHealth, errorMessage, listConnectors, revokeConnector, upsertConnector } from '../api/client'
import type { Connector } from '../api/types'
import { useT } from '../i18n'
import './connectors-page.css'

function statusLabel(connector: Connector): string {
  if (!connector.enabled) return 'Revoked'
  if (connector.health === 'ok') return 'Configured · not verified'
  if (connector.health === 'no_credential') return 'Credential required'
  if (connector.health === 'unreachable') return 'Unreachable'
  return 'Not verified'
}

export default function ConnectorsPage() {
  const t = useT()
  const [connectors, setConnectors] = useState<Connector[]>([])
  const [loading, setLoading] = useState(true)
  const [secretDraft, setSecretDraft] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState('')
  const [checking, setChecking] = useState('')
  const [error, setError] = useState('')
  const actionInFlightRef = useRef(new Set<string>())
  const available = connectors.filter((item) => item.enabled).length
  const configured = connectors.filter((item) => item.enabled && item.health === 'ok').length

  useEffect(() => {
    let active = true
    void listConnectors()
      .then((items) => { if (active) setConnectors(items) })
      .catch((loadError) => { if (active) setError(errorMessage(loadError)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [])

  const checkHealth = async (connector: Connector) => {
    if (!connector.enabled || actionInFlightRef.current.has(connector.id)) return
    actionInFlightRef.current.add(connector.id)
    setChecking(connector.id)
    setError('')
    try {
      const updated = await checkConnectorHealth(connector.id)
      setConnectors((current) => current.map((item) => item.id === updated.id ? updated : item))
    } catch (healthError) {
      setError(errorMessage(healthError))
    } finally {
      actionInFlightRef.current.delete(connector.id)
      setChecking('')
    }
  }

  const saveSecret = async (connector: Connector) => {
    if (!connector.enabled || !connector.secret_env || actionInFlightRef.current.has(connector.id)) return
    const secret = secretDraft[connector.id]?.trim()
    if (!secret) return
    actionInFlightRef.current.add(connector.id)
    setSaving(connector.id)
    setError('')
    try {
      const updated = await upsertConnector(connector.id, {
        type: connector.type,
        display_name: connector.display_name,
        scope: connector.scope,
        secret_env: connector.secret_env,
        endpoint: connector.endpoint,
        secret,
      })
      setConnectors((current) => current.map((item) => item.id === updated.id ? updated : item))
      setSecretDraft((current) => ({ ...current, [connector.id]: '' }))
    } catch (saveError) {
      setError(errorMessage(saveError))
    } finally {
      actionInFlightRef.current.delete(connector.id)
      setSaving('')
    }
  }

  const revoke = async (connector: Connector) => {
    if (!connector.enabled || actionInFlightRef.current.has(connector.id)) return
    actionInFlightRef.current.add(connector.id)
    setSaving(connector.id)
    setError('')
    try {
      const updated = await revokeConnector(connector.id)
      setConnectors((current) => current.map((item) => item.id === updated.id ? updated : item))
    } catch (revokeError) {
      setError(errorMessage(revokeError))
    } finally {
      actionInFlightRef.current.delete(connector.id)
      setSaving('')
    }
  }

  return (
    <section className="connectors-content" aria-labelledby="connectors-page-heading">
        <header className="connectors-header">
          <div><h1 id="connectors-page-heading">{t('Connectors')}</h1><p>{t('Connect external services that Agents may request through the same approval and audit boundary.')}</p></div>
          <dl>
            <div><dt>{t('Available')}</dt><dd>{available}</dd></div>
            <div><dt>{t('Configured')}</dt><dd>{configured}</dd></div>
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
                <article key={connector.id} data-enabled={connector.enabled ? 'true' : 'false'}>
                  <div className="connector-mark" aria-hidden="true">{connector.type.slice(0, 2).toUpperCase()}</div>
                  <div className="connector-copy">
                    <header><div><h3>{connector.display_name}</h3><p>{connector.type} · {connector.scope}</p></div><span data-health={connector.health} data-enabled={connector.enabled ? 'true' : 'false'}>{t(statusLabel(connector))}</span></header>
                    <dl>
                      <div><dt>{t('Credential')}</dt><dd>{connector.secret_env ?? t('No credential required')}</dd></div>
                      <div><dt>{t('Endpoint')}</dt><dd>{connector.endpoint ?? t('Managed by the connector')}</dd></div>
                      <div><dt>{t('Last checked')}</dt><dd>{connector.last_checked_at ? new Date(connector.last_checked_at).toLocaleString() : t('Never')}</dd></div>
                    </dl>
                  </div>
                  <div className="connector-actions">
                    {connector.enabled ? (
                      <>
                        {connector.secret_env && (
                          <>
                            <label>
                              <span className="visually-hidden">{t('Secret')}</span>
                              <input
                                type="password"
                                aria-label={t('Secret for {name}', { name: connector.display_name })}
                                value={secretDraft[connector.id] ?? ''}
                                onChange={(event) => setSecretDraft((current) => ({ ...current, [connector.id]: event.target.value }))}
                                placeholder={t('Store credential')}
                              />
                            </label>
                            <button type="button" disabled={saving === connector.id || checking === connector.id || !(secretDraft[connector.id] ?? '').trim()} onClick={() => void saveSecret(connector)}>
                              {t('Save secret')}
                            </button>
                          </>
                        )}
                        <button type="button" disabled={checking === connector.id || saving === connector.id} onClick={() => void checkHealth(connector)}>
                          {checking === connector.id ? t('Checking…') : t('Check configuration')}
                        </button>
                        <button type="button" disabled={saving === connector.id || checking === connector.id} onClick={() => void revoke(connector)}>
                          {t('Revoke')}
                        </button>
                      </>
                    ) : (
                      <p className="connector-revoked-note">{t('Revoked connectors cannot be restored here. Ask an administrator to recreate this connector.')}</p>
                    )}
                  </div>
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
