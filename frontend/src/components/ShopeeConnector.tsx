import { useEffect, useRef, useState } from 'react'
import { errorMessage, listConnectors, readMarketplace, revokeConnector, upsertConnector } from '../api/client'
import type { Connector } from '../api/types'
import { PRODUCT_READ_SCOPE, type MarketplaceRead, type MarketplacePriceStock } from '../api/marketplaces'
import { useT } from '../i18n'
import MarketplaceProjectAccess from './MarketplaceProjectAccess'

type Props = { connector: Connector; onChange: (connector: Connector) => void }

const STORE_SCOPE = 'shopee:shop:read'
const ORIGINS = ['https://partner.shopeemobile.com', 'https://openplatform.shopee.cn', 'https://openplatform.shopee.com.br']
const STATUSES: Record<string, string> = {
  NORMAL: 'Listed', BANNED: 'Banned', UNLIST: 'Unlisted', REVIEWING: 'Under review',
  SELLER_DELETE: 'Deleted by seller', SHOPEE_DELETE: 'Deleted by Shopee',
  MODEL_NORMAL: 'Available', MODEL_UNAVAILABLE: 'Unavailable', UNKNOWN: 'Unknown',
}

export default function ShopeeConnector({ connector, onChange }: Props) {
  const t = useT()
  const [origin, setOrigin] = useState(connector.endpoint ?? ORIGINS[0])
  const [partnerId, setPartnerId] = useState('')
  const [shopId, setShopId] = useState('')
  const [partnerKey, setPartnerKey] = useState('')
  const [accessToken, setAccessToken] = useState('')
  const [accessExpiry, setAccessExpiry] = useState('')
  const [refreshToken, setRefreshToken] = useState('')
  const [refreshExpiry, setRefreshExpiry] = useState('')
  const [detail, setDetail] = useState<MarketplaceRead | null>(null)
  const [allowProducts, setAllowProducts] = useState(connector.scope.split(/\s+/).includes(PRODUCT_READ_SCOPE.shopee))
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
  const clearResults = () => { setShop(null); setPage(null); setDetail(null) }
  const clearSecrets = () => { setPartnerKey(''); setAccessToken(''); setRefreshToken(''); setAccessExpiry(''); setRefreshExpiry('') }
  const accept = (updated: Connector) => {
    if (updated.id !== connector.id || updated.type !== 'shopee') throw new Error(t('Refresh the connector status before continuing.'))
    if (!mounted.current) return
    setOrigin(updated.endpoint ?? ORIGINS[0])
    setAllowProducts(updated.scope.split(/\s+/).includes(PRODUCT_READ_SCOPE.shopee))
    onChange(updated)
    setNeedsRefresh(false)
  }
  const save = () => act(async () => {
    const accessExpiresAt = Math.floor(new Date(accessExpiry).getTime() / 1000)
    const refreshExpiresAt = Math.floor(new Date(refreshExpiry).getTime() / 1000)
    if (![partnerId, shopId].every((value) => /^[1-9][0-9]{0,18}$/.test(value) && BigInt(value) < 2n ** 63n)) throw new Error(t('Enter valid numeric Partner ID and Shop ID.'))
    if (!Number.isFinite(accessExpiresAt) || accessExpiresAt <= 0 || (accessExpiresAt <= Date.now() / 1000 && !refreshToken.trim())) throw new Error(t('Enter a future access token expiry.'))
    if (Boolean(refreshToken.trim()) !== Boolean(refreshExpiry) || (refreshToken.trim() && (!Number.isFinite(refreshExpiresAt) || refreshExpiresAt <= Date.now() / 1000))) throw new Error(t('A refresh token needs its future expiry time.'))
    clearResults()
    setNeedsRefresh(true)
    try {
      const updated = await upsertConnector(connector.id, {
        type: connector.type, display_name: connector.display_name,
        endpoint: origin, secret_env: connector.secret_env,
        scope: [STORE_SCOPE, ...(allowProducts ? [PRODUCT_READ_SCOPE.shopee] : [])].join(' '),
        secret: JSON.stringify({
          partner_id: partnerId, shop_id: shopId, partner_key: partnerKey.trim(),
          access_token: accessToken.trim(), access_token_expires_at: accessExpiresAt,
          ...(refreshToken.trim() ? { refresh_token: refreshToken.trim(), refresh_token_expires_at: refreshExpiresAt } : {}),
        }),
      })
      accept(updated)
      if (mounted.current) setNotice(t('Credentials saved. Read the store to verify access.'))
    } finally {
      if (mounted.current) clearSecrets()
    }
  })
  const revoke = () => act(async () => {
    clearResults()
    clearSecrets(); setNeedsRefresh(true)
    accept(await revokeConnector(connector.id))
  })
  const refresh = () => act(async () => {
    clearResults()
    const updated = (await listConnectors()).find((item) => item.id === connector.id && item.type === 'shopee')
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
  const prices = (value: MarketplacePriceStock) => value.prices?.length
    ? value.prices.map((price) => `${price.currency} ${price.current ?? t('Unknown')} (${t('Original price')}: ${price.original ?? t('Unknown')})`).join(' · ')
    : t('Unknown')
  const stock = (value: MarketplacePriceStock) => `${t('Available stock')}: ${value.stock?.total_available_stock ?? t('Unknown')} · ${t('Reserved stock')}: ${value.stock?.total_reserved_stock ?? t('Unknown')}`

  const disabled = busy || needsRefresh
  const selected = shop?.shops[0] ?? null
  const configured = connector.scope.split(/\s+/)
  const canReadStore = connector.enabled && configured.includes(STORE_SCOPE)
  const canReadProducts = canReadStore && configured.includes(PRODUCT_READ_SCOPE.shopee)
  const observation = detail ?? page ?? shop
  const shelfStatus = (value: string) => t(STATUSES[value] ?? 'Unknown')

  return <article className="marketplace-connector" data-enabled={connector.enabled ? 'true' : 'false'}>
    <div className="connector-copy">
      <header><h3>{connector.display_name}</h3><span>{t(connector.enabled ? 'Seller read access' : 'Revoked')}</span></header>
      <p>{t('Read one authorized Shopee shop, its listings and SKU prices and stock. These are seller records, not market demand.')}</p>
      <details>
        <summary>{t(connector.enabled ? 'Configure seller access' : 'Reconnect seller access')}</summary>
        <p>{t('Enter an existing token pair from your authorized Shopee app and its exact Shop ID. An optional refresh token renews short-lived access automatically. After an interrupted refresh, replace the pair.')}</p>
        <p><a href="https://open.shopee.com/developer-guide/20" target="_blank" rel="noreferrer">{t('Official developer guide')}</a></p>
        <form onSubmit={(event) => { event.preventDefault(); void save() }}>
          <fieldset disabled={disabled}>
            <label>{t('Shopee API origin')}<select value={origin} onChange={(event) => setOrigin(event.target.value)}>
              {ORIGINS.map((value) => <option key={value} value={value}>{value}</option>)}
            </select></label>
            <label>{t('Partner ID')}<input required inputMode="numeric" autoComplete="off" maxLength={19} value={partnerId} onChange={(event) => setPartnerId(event.target.value)} /></label>
            <label>{t('Shop ID')}<input required inputMode="numeric" autoComplete="off" maxLength={19} value={shopId} onChange={(event) => setShopId(event.target.value)} /></label>
            <label>{t('Partner key')}<input type="password" required autoComplete="new-password" maxLength={512} value={partnerKey} onChange={(event) => setPartnerKey(event.target.value)} /></label>
            <label>{t('Seller access token')}<input type="password" required autoComplete="off" maxLength={4096} value={accessToken} onChange={(event) => setAccessToken(event.target.value)} /></label>
            <label>{t('Access token expiry (local time)')}<input type="datetime-local" required value={accessExpiry} onChange={(event) => setAccessExpiry(event.target.value)} /></label>
            <label>{t('Refresh token (optional)')}<input type="password" autoComplete="off" maxLength={4096} value={refreshToken} onChange={(event) => setRefreshToken(event.target.value)} /></label>
            <label>{t('Refresh token expiry (local time)')}<input type="datetime-local" required={Boolean(refreshToken)} value={refreshExpiry} onChange={(event) => setRefreshExpiry(event.target.value)} /></label>
            <label className="marketplace-checkbox"><input type="checkbox" checked={allowProducts} onChange={(event) => setAllowProducts(event.target.checked)} />{t('Allow Shopee product reads')}</label>
            <p>{t('This setting controls OPHIRA reads. Shopee must also authorize these APIs for your integration.')}</p>
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
        <label>{t('Product status')}<select disabled={disabled} value={status} onChange={(event) => { setStatus(event.target.value); setPage(null); setDetail(null) }}>
          <option value="ALL">{t('All statuses')}</option>
          {Object.entries(STATUSES).filter(([value]) => value !== 'UNKNOWN' && !value.startsWith('MODEL_')).map(([value, label]) => <option key={value} value={value}>{t(label)}</option>)}
        </select></label>
        <button type="button" disabled={disabled || !canReadProducts} onClick={() => void loadProducts()}>{t('Read products')}</button>
        {!canReadProducts && <p>{t('Configure product read permission to retrieve listings.')}</p>}
      </div>}
      {page && <>
        <p>{t('{count} products reported in total', { count: String(page.total_count) })}</p>
        {page.partial_data && <p role="status">{t('Some requested data was not returned by Shopee. Missing values are unknown.')}</p>}
        {page.products.length ? <div className="marketplace-table"><table>
          <caption>{t('Product listings · current page')}</caption>
          <thead><tr><th>{t('Product')}</th><th>{t('Status')}</th><th>{t('SKU')}</th><th>{t('Price and stock')}</th><th>{t('Action')}</th></tr></thead>
          <tbody>{page.products.map((product) => <tr key={product.id}>
            <th scope="row">{product.title || t('Title not supplied')}<br /><small>{product.id}</small></th>
            <td>{shelfStatus(product.status)}</td><td>{product.sku || t('Not supplied')}</td>
            <td>{product.details_available === false ? t('Details not supplied') : product.has_variants ? t('Read SKU details for variant prices and stock.') : <>{prices(product)}<br />{stock(product)}</>}</td>
            <td><button type="button" disabled={disabled || !canReadProducts} aria-label={t('Read SKU details for {id}', { id: product.id })} onClick={() => void loadDetail(product.id)}>{t('Read SKU details')}</button></td>
          </tr>)}</tbody>
        </table></div> : <p>{t('No products on this page.')}</p>}
        {page.next_page_token && <button type="button" disabled={disabled || !canReadProducts} onClick={() => void loadProducts(true)}>{t('Next page')}</button>}
      </>}
      {detail && <section aria-label={t('SKU details')}>
        {detail.partial_data && <p role="status">{t('Some requested data was not returned by Shopee. Missing values are unknown.')}</p>}
        {detail.products.map((product) => <div key={product.id}>
          <h4>{product.title || product.id}</h4>
          <p>{t('{count} variants reported in total', { count: String(detail.total_count ?? product.variants?.length ?? 0) })}</p>
          <p>{t('Product ID')}: {product.id} · {t('SKU')}: {product.sku || t('Not supplied')}</p>
          <p>{prices(product)}<br />{stock(product)}</p>
          {product.variants?.length ? <div className="marketplace-table"><table>
            <caption>{t('SKU details · current page')}</caption>
            <thead><tr><th>{t('Variant')}</th><th>{t('SKU')}</th><th>{t('Status')}</th><th>{t('Price and stock')}</th></tr></thead>
            <tbody>{product.variants.map((variant) => <tr key={variant.id}>
              <th scope="row">{variant.title || t('Title not supplied')}<br /><small>{variant.id}</small></th>
              <td>{variant.sku || t('Not supplied')}</td><td>{shelfStatus(variant.status)}</td><td>{prices(variant)}<br />{stock(variant)}</td>
            </tr>)}</tbody>
          </table></div> : product.has_variants ? <p>{t('No variants supplied')}</p> : <p>{t('This product has no variants.')}</p>}
          {detail.next_page_token && <button type="button" disabled={disabled || !canReadProducts} onClick={() => void loadDetail(product.id, true)}>{t('Next SKU page')}</button>}
        </div>)}
      </section>}
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
