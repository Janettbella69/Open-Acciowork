import { useEffect, useRef, useState } from 'react'
import { errorMessage, listConnectors, readMarketplace, revokeConnector, upsertConnector } from '../api/client'
import type { Connector } from '../api/types'
import { PRODUCT_READ_SCOPE, type MarketplaceRead } from '../api/marketplaces'
import { useT } from '../i18n'
import MarketplaceProjectAccess from './MarketplaceProjectAccess'

type Props = { connector: Connector; onChange: (connector: Connector) => void }

const STORE_SCOPE = 'shein:store:read'
const ORIGINS = ['https://openapi.sheincorp.com', 'https://openapi.sheincorp.cn']
const STATUSES: Record<string, string> = { ON_SHELF: 'On shelf', OFF_SHELF: 'Off shelf', UNKNOWN: 'Unknown' }

export default function SheinConnector({ connector, onChange }: Props) {
  const t = useT()
  const [origin, setOrigin] = useState(connector.endpoint ?? ORIGINS[0])
  const [openKeyId, setOpenKeyId] = useState('')
  const [secretKey, setSecretKey] = useState('')
  const [allowProducts, setAllowProducts] = useState(connector.scope.split(/\s+/).includes(PRODUCT_READ_SCOPE.shein))
  const [status, setStatus] = useState('ALL')
  const [shop, setShop] = useState<MarketplaceRead | null>(null)
  const [page, setPage] = useState<MarketplaceRead | null>(null)
  const [busy, setBusy] = useState(false)
  const [needsRefresh, setNeedsRefresh] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const pending = useRef(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const act = async (action: () => Promise<void>, refresh = false) => {
    if (pending.current || (needsRefresh && !refresh)) return
    pending.current = true
    setBusy(true); setError(''); setNotice('')
    try {
      await action()
    } catch (reason) {
      if (mounted.current) setError(errorMessage(reason))
    } finally {
      pending.current = false
      if (mounted.current) setBusy(false)
    }
  }
  const clearResults = () => { setShop(null); setPage(null) }
  const accept = (updated: Connector) => {
    if (updated.id !== connector.id || updated.type !== 'shein') throw new Error(t('Refresh the connector status before continuing.'))
    if (!mounted.current) return
    setOrigin(updated.endpoint ?? ORIGINS[0])
    setAllowProducts(updated.scope.split(/\s+/).includes(PRODUCT_READ_SCOPE.shein))
    onChange(updated)
    setNeedsRefresh(false)
  }
  const save = () => act(async () => {
    clearResults()
    setNeedsRefresh(true)
    try {
      const updated = await upsertConnector(connector.id, {
        type: connector.type, display_name: connector.display_name,
        endpoint: origin, secret_env: connector.secret_env,
        scope: [STORE_SCOPE, ...(allowProducts ? [PRODUCT_READ_SCOPE.shein] : [])].join(' '),
        secret: JSON.stringify({ open_key_id: openKeyId.trim(), secret_key: secretKey.trim() }),
      })
      accept(updated)
      if (mounted.current) setNotice(t('Credentials saved. Read the store to verify access.'))
    } finally {
      if (mounted.current) { setOpenKeyId(''); setSecretKey('') }
    }
  })
  const revoke = () => act(async () => {
    clearResults()
    setOpenKeyId(''); setSecretKey(''); setNeedsRefresh(true)
    accept(await revokeConnector(connector.id))
  })
  const refresh = () => act(async () => {
    clearResults()
    const updated = (await listConnectors()).find((item) => item.id === connector.id && item.type === 'shein')
    if (!updated) throw new Error(t('This connector is no longer available. Reload Connectors.'))
    accept(updated)
  }, true)
  const loadShop = () => act(async () => {
    clearResults()
    const result = await readMarketplace(connector.id, { operation: 'authorized_shops' })
    if (mounted.current) setShop(result)
  })
  const loadProducts = (next = false) => act(async () => {
    const selected = shop?.shops[0]
    if (!selected) return
    const cursor = next ? page?.next_page_token : null
    setPage(null)
    const result = await readMarketplace(connector.id, {
      operation: 'products', shop_id: selected.id, page_size: 10, product_status: status,
      ...(cursor ? { page_token: cursor } : {}),
    })
    if (mounted.current) setPage(result)
  })

  const disabled = busy || needsRefresh
  const selected = shop?.shops[0] ?? null
  const configured = connector.scope.split(/\s+/)
  const canReadStore = connector.enabled && configured.includes(STORE_SCOPE)
  const canReadProducts = canReadStore && configured.includes(PRODUCT_READ_SCOPE.shein)
  const observation = page ?? shop
  const shelfStatus = (value: string) => t(STATUSES[value] ?? 'Unknown')

  return <article className="marketplace-connector" data-enabled={connector.enabled ? 'true' : 'false'}>
    <div className="connector-copy">
      <header><h3>{connector.display_name}</h3><span>{t(connector.enabled ? 'Seller read access' : 'Revoked')}</span></header>
      <p>{t('Read your authorized SHEIN store and product groups with their variants. Store listings do not describe market-wide demand.')}</p>
      <details>
        <summary>{t(connector.enabled ? 'Configure seller access' : 'Reconnect seller access')}</summary>
        <p>{t('Use the openKeyId and already decrypted seller secretKey from your authorized SHEIN integration. The encrypted secretKey returned by authorization exchange cannot be used directly.')}</p>
        <p><a href="https://open.sheincorp.com/zh/documents/system/passwdrule" target="_blank" rel="noreferrer">{t('Official developer guide')}</a></p>
        <form onSubmit={(event) => { event.preventDefault(); void save() }}>
          <fieldset disabled={disabled}>
            <label>{t('SHEIN API origin')}<select value={origin} onChange={(event) => setOrigin(event.target.value)}>
              {ORIGINS.map((value) => <option key={value} value={value}>{value}</option>)}
            </select></label>
            <label>{t('Seller openKeyId')}<input type="password" required autoComplete="off" maxLength={160} value={openKeyId} onChange={(event) => setOpenKeyId(event.target.value)} /></label>
            <label>{t('Decrypted seller secretKey')}<input type="password" required autoComplete="new-password" maxLength={512} value={secretKey} onChange={(event) => setSecretKey(event.target.value)} /></label>
            <label className="marketplace-checkbox"><input type="checkbox" checked={allowProducts} onChange={(event) => setAllowProducts(event.target.checked)} />{t('Allow SHEIN product reads')}</label>
            <p>{t('This setting controls OPHIRA reads. SHEIN must also authorize these APIs for your integration.')}</p>
            <button type="submit">{t('Save seller credentials')}</button>
          </fieldset>
        </form>
      </details>
      <div className="marketplace-toolbar">
        <button type="button" disabled={disabled || !canReadStore} onClick={() => void loadShop()}>{t('Read store')}</button>
        <button type="button" disabled={disabled || !connector.enabled} onClick={() => void revoke()}>{t('Revoke')}</button>
        {busy && <span role="status">{t('Working…')}</span>}
      </div>
      {error && <p className="connectors-error" role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {needsRefresh && !busy && <div>
        <p>{t('The change may have reached the server. Refresh its status before continuing.')}</p>
        <button type="button" onClick={() => void refresh()}>{t('Refresh connector status')}</button>
      </div>}
      {selected && <div className="marketplace-shop-picker">
        <p><strong>{selected.name || selected.id}</strong>{selected.name ? ` · ${selected.id}` : ''}{selected.seller_type ? ` · ${selected.seller_type}` : ''}</p>
        <label>{t('SKC shelf status')}<select disabled={disabled} value={status} onChange={(event) => { setStatus(event.target.value); setPage(null) }}>
          <option value="ALL">{t('All shelf statuses')}</option>
          <option value="ON_SHELF">{t('On shelf')}</option>
          <option value="OFF_SHELF">{t('Off shelf')}</option>
        </select></label>
        <button type="button" disabled={disabled || !canReadProducts} onClick={() => void loadProducts()}>{t('Read products')}</button>
        {!canReadProducts && <p>{t('Configure product read permission to retrieve listings.')}</p>}
      </div>}
      {page && <>
        <p>{t('Page {page} · {count} SPU groups reported in total', { page: String(page.page_number), count: String(page.total_count) })}</p>
        <p>{t('Each page contains up to 10 SPU groups. SKC variants belong to those groups; the shelf filter applies to SKCs.')}</p>
        {page.products.length ? <div className="marketplace-table"><table>
          <caption>{t('SHEIN product groups · current page')}</caption>
          <thead><tr><th>{t('SPU group')}</th><th>{t('Status')}</th><th>{t('Category ID')}</th><th>{t('SKC variants')}</th></tr></thead>
          <tbody>{page.products.map((product) => <tr key={product.id}>
            <th scope="row">{product.id}</th><td>{shelfStatus(product.status)}</td><td>{product.category_id || '—'}</td>
            <td>{product.variants?.length ? <ul>{product.variants.map((variant) => <li key={variant.id}>
              {variant.title || t('Title not supplied')}<br /><small>{variant.id} · {shelfStatus(variant.status)}</small>
            </li>)}</ul> : t('No variants supplied')}</td>
          </tr>)}</tbody>
        </table></div> : <p>{t('No products on this page.')}</p>}
        {page.next_page_token && <button type="button" disabled={disabled || !canReadProducts} onClick={() => void loadProducts(true)}>{t('Next page')}</button>}
      </>}
      {observation && <p className="marketplace-provenance">
        {t('Retrieved at {time}', { time: new Date(observation.retrieved_at).toLocaleString() })} · {t('Request ID')}: {observation.request_id || t('Not supplied')}<br />
        {observation.source_url}
      </p>}
      <MarketplaceProjectAccess connector={connector} selectedShop={selected ? { id: selected.id, name: selected.name || selected.id } : null} busy={disabled} onBusyChange={(value) => {
        pending.current = value
        if (mounted.current) setBusy(value)
      }} />
    </div>
  </article>
}
