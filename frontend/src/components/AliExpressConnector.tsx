import { useEffect, useRef, useState } from 'react'
import { errorMessage, listConnectors, readMarketplace, revokeConnector, upsertConnector } from '../api/client'
import type { Connector } from '../api/types'
import { PRODUCT_READ_SCOPE, type MarketplaceRead, type MarketplaceQuote } from '../api/marketplaces'
import { useT } from '../i18n'
import MarketplaceProjectAccess from './MarketplaceProjectAccess'

type Props = { connector: Connector; onChange: (connector: Connector) => void }
const STORE_SCOPE = 'aliexpress:seller:read'
const ORIGIN = 'https://api-sg.aliexpress.com'
const STATUSES: Record<string, string> = {
  onSelling: 'Listed', offline: 'Unlisted', auditing: 'Under review',
  editingRequired: 'Changes required', service_delete: 'Deleted by customer service', deleted: 'Deleted',
}

export default function AliExpressConnector({ connector, onChange }: Props) {
  const t = useT()
  const [appKey, setAppKey] = useState('')
  const [appSecret, setAppSecret] = useState('')
  const [accessToken, setAccessToken] = useState('')
  const [sellerId, setSellerId] = useState('')
  const [expiry, setExpiry] = useState('')
  const [allowProducts, setAllowProducts] = useState(connector.scope.split(/\s+/).includes(PRODUCT_READ_SCOPE.aliexpress))
  const [status, setStatus] = useState('onSelling')
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
  const clearSecrets = () => { setAppKey(''); setAppSecret(''); setAccessToken(''); setExpiry('') }
  const accept = (updated: Connector) => {
    if (updated.id !== connector.id || updated.type !== 'aliexpress') throw new Error(t('Refresh the connector status before continuing.'))
    if (!mounted.current) return
    setAllowProducts(updated.scope.split(/\s+/).includes(PRODUCT_READ_SCOPE.aliexpress))
    onChange(updated)
    setNeedsRefresh(false)
  }
  const save = () => act(async () => {
    if (!/^[1-9][0-9]{0,18}$/.test(sellerId) || BigInt(sellerId) >= 2n ** 63n) throw new Error(t('Enter a valid numeric Seller ID.'))
    const expiresAt = Math.floor(new Date(expiry).getTime() / 1000)
    if (!Number.isFinite(expiresAt) || expiresAt <= Date.now() / 1000) throw new Error(t('Enter a future access token expiry.'))
    clearResults(); setNeedsRefresh(true)
    try {
      accept(await upsertConnector(connector.id, {
        type: connector.type, display_name: connector.display_name, endpoint: ORIGIN,
        secret_env: connector.secret_env,
        scope: [STORE_SCOPE, ...(allowProducts ? [PRODUCT_READ_SCOPE.aliexpress] : [])].join(' '),
        secret: JSON.stringify({ app_key: appKey.trim(), app_secret: appSecret.trim(), access_token: accessToken.trim(), seller_id: sellerId, access_token_expires_at: expiresAt }),
      }))
      if (mounted.current) setNotice(t('Credentials saved. Read the seller account to verify access.'))
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
    const updated = (await listConnectors()).find((item) => item.id === connector.id && item.type === 'aliexpress')
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
  const quote = (value?: MarketplaceQuote | null) => value ? `${value.currency ?? t('Currency unknown')} ${value.amount}` : t('Unknown')
  const disabled = busy || needsRefresh
  const selected = shop?.shops[0] ?? null
  const configured = connector.scope.split(/\s+/)
  const canReadStore = connector.enabled && configured.includes(STORE_SCOPE)
  const canReadProducts = canReadStore && configured.includes(PRODUCT_READ_SCOPE.aliexpress)
  const observation = detail ?? page ?? shop

  return <article className="marketplace-connector" data-enabled={connector.enabled ? 'true' : 'false'}>
    <div className="connector-copy">
      <header><h3>{connector.display_name}</h3><span>{t(connector.enabled ? 'Seller read access' : 'Revoked')}</span></header>
      <p>{t('Read an authorized AliExpress seller account, product listings and standard POP SKU details. Subaccounts see only their permitted products.')}</p>
      <details>
        <summary>{t(connector.enabled ? 'Configure seller access' : 'Reconnect seller access')}</summary>
        <p>{t('Use your authorized app credentials and existing seller token. Copy seller_id from the authorization response and enter the actual token expiry. Replace expired tokens here.')}</p>
        <p><a href="https://open.aliexpress.com/doc/doc.htm#/?docId=989" target="_blank" rel="noreferrer">{t('Official developer guide')}</a></p>
        <form onSubmit={(event) => { event.preventDefault(); void save() }}>
          <fieldset disabled={disabled}>
            <label>{t('App key')}<input type="password" required autoComplete="off" maxLength={160} value={appKey} onChange={(event) => setAppKey(event.target.value)} /></label>
            <label>{t('App secret')}<input type="password" required autoComplete="new-password" maxLength={512} value={appSecret} onChange={(event) => setAppSecret(event.target.value)} /></label>
            <label>{t('Seller access token')}<input type="password" required autoComplete="off" maxLength={4096} value={accessToken} onChange={(event) => setAccessToken(event.target.value)} /></label>
            <label>{t('Seller ID')}<input required inputMode="numeric" autoComplete="off" maxLength={19} value={sellerId} onChange={(event) => setSellerId(event.target.value)} /></label>
            <label>{t('Access token expiry (local time)')}<input type="datetime-local" required value={expiry} onChange={(event) => setExpiry(event.target.value)} /></label>
            <label className="marketplace-checkbox"><input type="checkbox" checked={allowProducts} onChange={(event) => setAllowProducts(event.target.checked)} />{t('Allow AliExpress product reads')}</label>
            <p>{t('AliExpress must also authorize the seller profile, product list and product detail APIs for this app.')}</p>
            <button type="submit">{t('Save seller credentials')}</button>
          </fieldset>
        </form>
      </details>
      <div className="marketplace-toolbar">
        <button type="button" disabled={disabled || !canReadStore} onClick={() => void loadShop()}>{t('Read seller account')}</button>
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
        <p><strong>{t('Seller ID')}: {selected.id}</strong>{selected.region ? ` · ${selected.region}` : ''}</p>
        <p>{t('Project access binds this seller account. Seller ID is not a storefront URL or store ID.')}</p>
        {shop?.authorization_expires_at && <p>{t('Configured token expiry: {time}', { time: new Date(shop.authorization_expires_at).toLocaleString() })}</p>}
        <label>{t('Product status')}<select disabled={disabled} value={status} onChange={(event) => { setStatus(event.target.value); setPage(null); setDetail(null) }}>
          {Object.entries(STATUSES).map(([value, label]) => <option key={value} value={value}>{t(label)}</option>)}
        </select></label>
        <p>{t('Read each status separately. Product pages show the requested status, not all seller products.')}</p>
        <button type="button" disabled={disabled || !canReadProducts} onClick={() => void loadProducts()}>{t('Read products')}</button>
        {!canReadProducts && <p>{t('Configure product read permission to retrieve listings.')}</p>}
      </div>}
      {page && <>
        <p>{t('{count} products reported in total', { count: String(page.total_count) })} · {t('Page {page} of {total}', { page: String(page.page_number), total: String(page.total_pages) })}</p>
        {page.partial_data && <p role="status">{t('Some requested data was not returned by AliExpress. Missing values are unknown.')}</p>}
        {page.products.length ? <div className="marketplace-table"><table>
          <caption>{t('Product listings · current page')}</caption>
          <thead><tr><th>{t('Product')}</th><th>{t('Product type')}</th><th>{t('Reported price range')}</th><th>{t('Action')}</th></tr></thead>
          <tbody>{page.products.map((product) => <tr key={product.id}>
            <th scope="row">{product.title || t('Title not supplied')}<br /><small>{product.id}</small></th>
            <td>{product.product_type || t('Unknown')}</td>
            <td>{product.price_range?.currency ?? t('Currency unknown')} {product.price_range?.min ?? t('Unknown')} – {product.price_range?.max ?? t('Unknown')}</td>
            <td>{product.details_available ? <button type="button" disabled={disabled || !canReadProducts} aria-label={t('Read SKU details for {id}', { id: product.id })} onClick={() => void loadDetail(product.id)}>{t('Read SKU details')}</button> : t('SKU details currently support standard POP products.')}</td>
          </tr>)}</tbody>
        </table></div> : <p>{t('No products on this page.')}</p>}
        {page.next_page_token && <button type="button" disabled={disabled || !canReadProducts} onClick={() => void loadProducts(true)}>{t('Next page')}</button>}
      </>}
      {detail && <section aria-label={t('SKU details')}>
        <p>{t('Listed, daily discount and country discount prices are separate quotes. They do not establish the final checkout price. A stock flag is not a quantity.')}</p>
        {detail.partial_data && <p role="status">{t('Some requested data was not returned by AliExpress. Missing values are unknown.')}</p>}
        {detail.products.map((product) => <div key={product.id}>
          <h4>{product.title || product.id}</h4>
          <p>{t('{count} variants reported in total', { count: String(detail.total_count ?? t('Unknown')) })}</p>
          {product.has_country_pricing && <p>{t('This product has country pricing rules. Displayed base quotes do not resolve those rules.')}</p>}
          {product.variants?.length ? <div className="marketplace-table"><table>
            <caption>{t('SKU details · current page')}</caption>
            <thead><tr><th>{t('Variant')}</th><th>{t('SKU')}</th><th>{t('Listed price')}</th><th>{t('Daily discount price')}</th><th>{t('Country discounts')}</th><th>{t('Stock')}</th></tr></thead>
            <tbody>{product.variants.map((variant) => <tr key={variant.id}>
              <th scope="row">{variant.title || t('Title not supplied')}<br /><small>{variant.id_kind === 'properties' ? t('Property combination') : t('SKU ID')}: {variant.id}</small></th>
              <td>{variant.sku || t('Not supplied')}</td><td>{quote(variant.listed_price)}</td><td>{quote(variant.daily_discount_price)}</td>
              <td>{variant.country_discount_prices?.length ? variant.country_discount_prices.map((value) => `${value.country}: ${quote(value.quote)}`).join(' · ') : t('Not supplied')}</td>
              <td>{t('Available stock')}: {variant.stock?.total_available_stock ?? t('Unknown')}<br />{t('In-stock flag')}: {t(variant.in_stock == null ? 'Unknown' : variant.in_stock ? 'In stock' : 'Out of stock')}</td>
            </tr>)}</tbody>
          </table></div> : <p>{t('No variants supplied')}</p>}
          {detail.next_page_token && <button type="button" disabled={disabled || !canReadProducts} onClick={() => void loadDetail(product.id, true)}>{t('Next SKU page')}</button>}
        </div>)}
      </section>}
      {observation && <p className="marketplace-provenance">
        {t('Retrieved at {time}', { time: new Date(observation.retrieved_at).toLocaleString() })} · {t('Request ID')}: {observation.request_id || t('Not supplied')}<br />
        {observation.source_url}{observation.api_name ? ` · ${observation.api_name}` : ''}
      </p>}
      <MarketplaceProjectAccess connector={connector} selectedShop={selected ? { id: selected.id, name: `AliExpress · ${selected.id}` } : null} busy={disabled} onBusyChange={(value) => {
        pending.current = value
        if (mounted.current) setBusy(value)
      }} />
    </div>
  </article>
}
