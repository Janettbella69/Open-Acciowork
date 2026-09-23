import { useEffect, useRef, useState } from 'react'
import { errorMessage, listConnectors, readMarketplace, revokeConnector, upsertConnector } from '../api/client'
import type { Connector } from '../api/types'
import { PRODUCT_READ_SCOPE, type MarketplaceRead } from '../api/marketplaces'
import { useT } from '../i18n'
import MarketplaceProjectAccess from './MarketplaceProjectAccess'

type Props = { connector: Connector; onChange: (connector: Connector) => void }
const STORE_SCOPE = 'temu:store:read'
const PRICE_SCOPE = 'temu:supply-prices:read'
const ORIGINS = ['https://openapi-b-global.temu.com', 'https://openapi-b-us.temu.com', 'https://openapi-b-eu.temu.com']
const STATUSES: Record<string, string> = {
  ALL: 'Active and inactive', ACTIVE: 'Active', INACTIVE: 'Inactive',
  INCOMPLETE: 'Incomplete', DRAFT: 'Draft', DELETED: 'Deleted',
}
const RETURNED_STATUSES: Record<string, string> = {
  Active: 'Active', InActive: 'Inactive', Inactive: 'Inactive',
  Incomplete: 'Incomplete', Draft: 'Draft', Deleted: 'Deleted',
}
const PRICE_ACCESS: Record<string, string> = {
  disabled: 'Supply price reads are disabled in this connector.',
  not_authorized: 'Temu has not authorized supply price reads for this token.',
  unsupported_identifier: 'The supply price API cannot accept these product or SKU identifiers.',
}

export default function TemuConnector({ connector, onChange }: Props) {
  const t = useT()
  const [origin, setOrigin] = useState(connector.endpoint ?? ORIGINS[0])
  const [appKey, setAppKey] = useState('')
  const [appSecret, setAppSecret] = useState('')
  const [accessToken, setAccessToken] = useState('')
  const [allowProducts, setAllowProducts] = useState(connector.scope.split(/\s+/).includes(PRODUCT_READ_SCOPE.temu))
  const [allowPrices, setAllowPrices] = useState(connector.scope.split(/\s+/).includes(PRICE_SCOPE))
  const [status, setStatus] = useState('ALL')
  const [shop, setShop] = useState<MarketplaceRead | null>(null)
  const [page, setPage] = useState<MarketplaceRead | null>(null)
  const [detail, setDetail] = useState<MarketplaceRead | null>(null)
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
  const clearResults = () => { setShop(null); setPage(null); setDetail(null) }
  const clearSecrets = () => { setAppKey(''); setAppSecret(''); setAccessToken('') }
  const accept = (updated: Connector) => {
    if (updated.id !== connector.id || updated.type !== 'temu') throw new Error(t('Refresh the connector status before continuing.'))
    if (!mounted.current) return
    setOrigin(updated.endpoint ?? ORIGINS[0])
    setAllowProducts(updated.scope.split(/\s+/).includes(PRODUCT_READ_SCOPE.temu))
    setAllowPrices(updated.scope.split(/\s+/).includes(PRICE_SCOPE))
    onChange(updated)
    setNeedsRefresh(false)
  }
  const save = () => act(async () => {
    clearResults(); setNeedsRefresh(true)
    try {
      accept(await upsertConnector(connector.id, {
        type: connector.type, display_name: connector.display_name, endpoint: origin,
        secret_env: connector.secret_env,
        scope: [STORE_SCOPE, ...(allowProducts ? [PRODUCT_READ_SCOPE.temu] : []), ...(allowProducts && allowPrices ? [PRICE_SCOPE] : [])].join(' '),
        secret: JSON.stringify({ app_key: appKey.trim(), app_secret: appSecret.trim(), access_token: accessToken.trim() }),
      }))
      if (mounted.current) setNotice(t('Credentials saved. Read the store to verify access.'))
    } finally {
      if (mounted.current) clearSecrets()
    }
  })
  const revoke = () => act(async () => {
    clearResults(); clearSecrets(); setNeedsRefresh(true)
    accept(await revokeConnector(connector.id))
  })
  const refresh = () => act(async () => {
    clearResults()
    const updated = (await listConnectors()).find((item) => item.id === connector.id && item.type === 'temu')
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
    setPage(null); setDetail(null)
    const result = await readMarketplace(connector.id, {
      operation: 'products', shop_id: selected.id, page_size: 10, product_status: status,
      ...(cursor ? { page_token: cursor } : {}),
    })
    if (mounted.current) setPage(result)
  })
  const loadDetail = (productId: string, next = false) => act(async () => {
    const selected = shop?.shops[0]
    if (!selected) return
    const cursor = next ? detail?.next_page_token : null
    setDetail(null)
    const result = await readMarketplace(connector.id, {
      operation: 'product_details', shop_id: selected.id, product_id: productId, page_size: 10,
      ...(cursor ? { page_token: cursor } : {}),
    })
    if (mounted.current) setDetail(result)
  })
  const disabled = busy || needsRefresh
  const selected = shop?.shops[0] ?? null
  const configured = connector.scope.split(/\s+/)
  const canReadStore = connector.enabled && configured.includes(STORE_SCOPE)
  const canReadProducts = canReadStore && configured.includes(PRODUCT_READ_SCOPE.temu) && selected?.product_reads_available === true
  const canReadSkus = canReadProducts && selected?.sku_reads_available === true
  const observation = detail ?? page ?? shop
  const shelfStatus = (value: string) => t(RETURNED_STATUSES[value] ?? 'Unknown')

  return <article className="marketplace-connector" data-enabled={connector.enabled ? 'true' : 'false'}>
    <div className="connector-copy">
      <header><h3>{connector.display_name}</h3><span>{t(connector.enabled ? 'Seller read access' : 'Revoked')}</span></header>
      <p>{t('Read an authorized Temu local shop, its products and SKUs. Supply prices require separate permission. These are seller records, not market demand.')}</p>
      <details>
        <summary>{t(connector.enabled ? 'Configure seller access' : 'Reconnect seller access')}</summary>
        <p>{t('Use the app key, app secret and existing seller token for the selected Temu region. Read the store to verify its identity, permissions and expiry. Replace expired tokens here.')}</p>
        <p><a href="https://partner.temu.com/documentation?menu_code=52ef88bdef1d4527b15f6d303b173e48" target="_blank" rel="noreferrer">{t('Official developer guide')}</a></p>
        <form onSubmit={(event) => { event.preventDefault(); void save() }}>
          <fieldset disabled={disabled}>
            <label>{t('Temu API region')}<select value={origin} onChange={(event) => setOrigin(event.target.value)}>
              {ORIGINS.map((value, index) => <option key={value} value={value}>{t(['Other regions', 'United States', 'Europe'][index])}</option>)}
            </select></label>
            <label>{t('App key')}<input type="password" required autoComplete="off" maxLength={160} value={appKey} onChange={(event) => setAppKey(event.target.value)} /></label>
            <label>{t('App secret')}<input type="password" required autoComplete="new-password" maxLength={512} value={appSecret} onChange={(event) => setAppSecret(event.target.value)} /></label>
            <label>{t('Seller access token')}<input type="password" required autoComplete="off" maxLength={4096} value={accessToken} onChange={(event) => setAccessToken(event.target.value)} /></label>
            <label className="marketplace-checkbox"><input type="checkbox" checked={allowProducts} onChange={(event) => setAllowProducts(event.target.checked)} />{t('Allow Temu product reads')}</label>
            <label className="marketplace-checkbox"><input type="checkbox" disabled={!allowProducts} checked={allowProducts && allowPrices} onChange={(event) => setAllowPrices(event.target.checked)} />{t('Allow Temu supply price reads')}</label>
            <p>{t('Temu must also authorize each API for this token. Supply prices are not retail prices.')}</p>
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
        <p><strong>Temu · {selected.id}</strong> · {t(selected.seller_type === 'LOCAL' ? 'Local seller' : 'Semi-managed seller')}</p>
        {selected.region_id && <p>{t('Temu region ID')}: {selected.region_id}</p>}
        {shop?.authorization_expires_at && <p>{t('Authorization expires at {time}', { time: new Date(shop.authorization_expires_at).toLocaleString() })}</p>}
        {selected.seller_type !== 'LOCAL' && <p>{t('Product reads currently support local sellers. Semi-managed product reads are not available.')}</p>}
        <label>{t('Product status')}<select disabled={disabled} value={status} onChange={(event) => { setStatus(event.target.value); setPage(null); setDetail(null) }}>
          {Object.entries(STATUSES).map(([value, label]) => <option key={value} value={value}>{t(label)}</option>)}
        </select></label>
        <p>{t('Active and inactive excludes drafts, incomplete and deleted products. Select those statuses separately.')}</p>
        <button type="button" disabled={disabled || !canReadProducts} onClick={() => void loadProducts()}>{t('Read products')}</button>
        {!canReadProducts && selected.seller_type === 'LOCAL' && <p>{t('Allow product reads here and authorize the product API in Temu Seller Center.')}</p>}
        {canReadProducts && !canReadSkus && <p>{t('Authorize the SKU API in Temu Seller Center to read variants.')}</p>}
      </div>}
      {page && <>
        <p>{t('{count} products reported in total', { count: String(page.total_count) })}</p>
        {page.partial_data && <p role="status">{t('Some requested data was not returned by Temu. Missing values are unknown.')}</p>}
        {page.products.length ? <div className="marketplace-table"><table>
          <caption>{t('Product listings · current page')}</caption>
          <thead><tr><th>{t('Product')}</th><th>{t('Status')}</th><th>{t('SKU')}</th><th>{t('Action')}</th></tr></thead>
          <tbody>{page.products.map((product) => <tr key={product.id}>
            <th scope="row">{product.title || t('Title not supplied')}<br /><small>{product.id}</small></th>
            <td>{shelfStatus(product.status)}</td><td>{product.sku || t('Not supplied')}</td>
            <td><button type="button" disabled={disabled || !canReadSkus} aria-label={t('Read SKU details for {id}', { id: product.id })} onClick={() => void loadDetail(product.id)}>{t('Read SKU details')}</button></td>
          </tr>)}</tbody>
        </table></div> : <p>{t('No products on this page.')}</p>}
        {page.next_page_token && <button type="button" disabled={disabled || !canReadProducts} onClick={() => void loadProducts(true)}>{t('Next page')}</button>}
      </>}
      {detail && <section aria-label={t('SKU details')}>
        <p>{t('Supply prices are not retail prices. Reported stock has no available or reserved breakdown.')}</p>
        {detail.supplier_price_access && PRICE_ACCESS[detail.supplier_price_access] && <p>{t(PRICE_ACCESS[detail.supplier_price_access])}</p>}
        {detail.partial_data && <p role="status">{t('Some requested data was not returned by Temu. Missing values are unknown.')}</p>}
        {detail.products.map((product) => <div key={product.id}>
          <h4>{product.title || product.id}</h4>
          <p>{t('{count} variants reported in total', { count: String(detail.total_count ?? product.reported_sku_count ?? t('Unknown')) })}</p>
          {product.variants?.length ? <div className="marketplace-table"><table>
            <caption>{t('SKU details · current page')}</caption>
            <thead><tr><th>{t('Variant')}</th><th>{t('SKU')}</th><th>{t('Status')}</th><th>{t('Supply price')}</th><th>{t('Reported stock')}</th></tr></thead>
            <tbody>{product.variants.map((variant) => <tr key={variant.id}>
              <th scope="row">{variant.title || t('Title not supplied')}<br /><small>{variant.id}</small></th>
              <td>{variant.sku || t('Not supplied')}</td><td>{shelfStatus(variant.status)}</td>
              <td>{variant.supplier_price ? `${variant.supplier_price.currency} ${variant.supplier_price.amount}` : t('Unknown')}</td>
              <td>{variant.reported_stock ?? t('Unknown')}</td>
            </tr>)}</tbody>
          </table></div> : <p>{t('No variants supplied')}</p>}
          {detail.next_page_token && <button type="button" disabled={disabled || !canReadSkus} onClick={() => void loadDetail(product.id, true)}>{t('Next SKU page')}</button>}
        </div>)}
      </section>}
      {observation && <p className="marketplace-provenance">
        {t('Retrieved at {time}', { time: new Date(observation.retrieved_at).toLocaleString() })} · {t('Request ID')}: {observation.request_id || t('Not supplied')}<br />
        {observation.source_url}{observation.api_name ? ` · ${observation.api_name}` : ''}
      </p>}
      <MarketplaceProjectAccess connector={connector} selectedShop={selected && canReadProducts ? { id: selected.id, name: selected.name || `Temu · ${selected.id}` } : null} busy={disabled} onBusyChange={(value) => {
        pending.current = value
        if (mounted.current) setBusy(value)
      }} />
    </div>
  </article>
}
