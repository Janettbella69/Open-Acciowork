import { useEffect, useRef, useState } from 'react'
import { errorMessage, readMarketplace, revokeConnector, upsertConnector } from '../api/client'
import type { Connector } from '../api/types'
import type { MarketplaceRead } from '../api/marketplaces'
import { useT } from '../i18n'
import MarketplaceProjectAccess from './MarketplaceProjectAccess'

type Props = { connector: Connector; onChange: (connector: Connector) => void }

const PRODUCT_SCOPE = 'read_products'
const ORDER_SCOPE = 'read_orders'

// Accepts "store", "store.myshopify.com", a pasted store URL or an admin.shopify.com/store/
// link. The backend still requires the exact https://<store>.myshopify.com origin.
function normalizeStore(value: string): string | null {
  const text = value.trim().toLowerCase()
  const admin = /^(?:https?:\/\/)?admin\.shopify\.com\/store\/([^/?#]+)/.exec(text)
  const host = admin ? admin[1] : text.replace(/^https?:\/\//, '').split(/[/?#]/)[0]
  const store = host.endsWith('.myshopify.com') ? host.slice(0, -'.myshopify.com'.length) : host
  return /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(store) ? `https://${store}.myshopify.com` : null
}

export default function ShopifyConnector({ connector, onChange }: Props) {
  const t = useT()
  const configured = connector.scope.split(/\s+/)
  const [store, setStore] = useState(connector.endpoint ? connector.endpoint.replace(/^https:\/\//, '') : '')
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  // Older catalog records carry no Shopify scope; product reads are the useful default.
  const [allowProducts, setAllowProducts] = useState(configured.includes(PRODUCT_SCOPE) || !configured.includes(ORDER_SCOPE))
  const [allowOrders, setAllowOrders] = useState(configured.includes(ORDER_SCOPE))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [shop, setShop] = useState<MarketplaceRead | null>(null)
  const [page, setPage] = useState<MarketplaceRead | null>(null)
  const pending = useRef(false)
  const mounted = useRef(true)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const act = async (action: () => Promise<void>) => {
    if (pending.current) return
    pending.current = true
    setBusy(true)
    setError('')
    setNotice('')
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
  const save = () => act(async () => {
    const endpoint = normalizeStore(store)
    if (!endpoint) throw new Error(t('Enter the store address, such as your-store.myshopify.com.'))
    if (!allowProducts && !allowOrders) throw new Error(t('Allow product or order reads.'))
    clearResults()
    const updated = await upsertConnector(connector.id, {
      type: connector.type, display_name: connector.display_name, endpoint,
      secret_env: connector.secret_env,
      scope: [allowProducts && PRODUCT_SCOPE, allowOrders && ORDER_SCOPE].filter(Boolean).join(' '),
      secret: JSON.stringify({ client_id: clientId.trim(), client_secret: clientSecret.trim() }),
    })
    if (!mounted.current) return
    setClientId(''); setClientSecret(''); setStore(endpoint.replace(/^https:\/\//, ''))
    onChange(updated)
    setNotice(t('Credentials saved. Read the store to verify access.'))
  })

  const loadShop = () => act(async () => {
    clearResults()
    const result = await readMarketplace(connector.id, { operation: 'authorized_shops' })
    if (mounted.current) setShop(result)
  })

  const loadPage = (operation: 'products' | 'orders', next = false) => act(async () => {
    const storeShop = shop?.shops[0]
    if (!storeShop) return
    const cursor = next ? page?.next_page_token : null
    setPage(null)
    const result = await readMarketplace(connector.id, {
      operation, shop_id: storeShop.id, page_size: 20, ...(cursor ? { page_token: cursor } : {}),
    })
    if (mounted.current) setPage(result)
  })

  const revoke = () => act(async () => {
    clearResults()
    const updated = await revokeConnector(connector.id)
    if (!mounted.current) return
    setClientId(''); setClientSecret('')
    onChange(updated)
  })

  const storeShop = shop?.shops[0] ?? null
  const granted = shop?.granted_scopes ?? []
  const canRead = (scope: string) => connector.enabled && configured.includes(scope) && granted.includes(scope)
  const missing = [PRODUCT_SCOPE, ORDER_SCOPE].filter((scope) => configured.includes(scope) && shop && !granted.includes(scope))
  const observation = page ?? shop

  return (
    <article className="marketplace-connector" data-enabled={connector.enabled ? 'true' : 'false'}>
      <div className="connector-copy">
        <header><h3>{connector.display_name}</h3><span>{t(connector.enabled ? 'Store read access' : 'Revoked')}</span></header>
        <p>{t('Read your own Shopify store: shop details, products and recent orders. Customer names, emails and addresses are never requested, and nothing is written to the store.')}</p>
        <details>
          <summary>{t(connector.enabled ? 'Configure store access' : 'Reconnect store access')}</summary>
          <ol>
            <li>{t('In the Shopify Dev Dashboard, create an app and release a version with the read_products scope. Add read_orders to read orders.')}</li>
            <li>{t('Install the app on this store. The app and the store must belong to the same Dev Dashboard organization.')}</li>
            <li>{t('Copy the Client ID and Client secret from the app Settings page. OPHIRA requests 24-hour access tokens itself and keeps them only in memory.')}</li>
          </ol>
          <form onSubmit={(event) => { event.preventDefault(); void save() }}>
            <fieldset disabled={busy}>
              <label>{t('Store address')}<input type="text" autoComplete="off" required maxLength={255} placeholder="your-store.myshopify.com" value={store} onChange={(event) => setStore(event.target.value)} /></label>
              <label>{t('Client ID')}<input type="password" autoComplete="off" required maxLength={255} value={clientId} onChange={(event) => setClientId(event.target.value)} /></label>
              <label>{t('Client secret')}<input type="password" autoComplete="new-password" required maxLength={512} value={clientSecret} onChange={(event) => setClientSecret(event.target.value)} /></label>
              <label className="marketplace-checkbox"><input type="checkbox" checked={allowProducts} onChange={(event) => setAllowProducts(event.target.checked)} />{t('Allow product reads (read_products)')}</label>
              <label className="marketplace-checkbox"><input type="checkbox" checked={allowOrders} onChange={(event) => setAllowOrders(event.target.checked)} />{t('Allow recent order reads (read_orders)')}</label>
              <button type="submit">{t('Save store credentials')}</button>
            </fieldset>
          </form>
        </details>
        <div className="marketplace-toolbar">
          <button type="button" disabled={busy || !connector.enabled} onClick={() => void loadShop()}>{t('Read store')}</button>
          <button type="button" disabled={busy || !connector.enabled} onClick={() => void revoke()}>{t('Revoke')}</button>
          {busy && <span role="status">{t('Working…')}</span>}
        </div>
        {error && <p className="connectors-error" role="alert">{error}</p>}
        {notice && <p role="status">{notice}</p>}
        {storeShop && shop && (
          <div className="marketplace-shop-picker">
            <p><strong>{storeShop.name}</strong> · {storeShop.domain} · {storeShop.currency}{storeShop.region ? ` · ${storeShop.region}` : ''}</p>
            <p>{t('Granted scopes: {scopes}', { scopes: granted.join(', ') || '—' })}</p>
            {granted.some((scope) => scope.startsWith('write_')) && <p>{t('This app also holds write scopes. OPHIRA only reads, so you can remove them from the app version.')}</p>}
            {missing.map((scope) => <p key={scope}>{t('Add {scope} to a released app version and approve it on the store.', { scope })}</p>)}
            <button type="button" disabled={busy || !canRead(PRODUCT_SCOPE)} onClick={() => void loadPage('products')}>{t('Read products')}</button>
            <button type="button" disabled={busy || !canRead(ORDER_SCOPE)} onClick={() => void loadPage('orders')}>{t('Read recent orders')}</button>
          </div>
        )}
        {page?.operation === 'products' && (page.products.length ? <div className="marketplace-table"><table>
          <caption>{t('Product listings · current page')}</caption>
          <thead><tr><th>{t('Product')}</th><th>{t('Status')}</th><th>{t('Vendor')}</th><th>{t('Last updated')}</th></tr></thead>
          <tbody>{page.products.map((product) => <tr key={product.id}><td>{product.title}<br /><small>{product.id}</small></td><td>{product.status}</td><td>{product.vendor}</td><td>{product.updated_at ? new Date(product.updated_at).toLocaleString() : ''}</td></tr>)}</tbody>
        </table></div> : <p>{t('No products on this page.')}</p>)}
        {page?.operation === 'orders' && <>
          {page.order_window === 'last_60_days' && <p>{t('Shopify returns only the last 60 days of orders unless the app is granted read_all_orders.')}</p>}
          {page.orders?.length ? <div className="marketplace-table"><table>
            <caption>{t('Orders · current page')}</caption>
            <thead><tr><th>{t('Order')}</th><th>{t('Order created')}</th><th>{t('Payment status')}</th><th>{t('Fulfillment status')}</th><th>{t('Item count')}</th><th>{t('Order total')}</th></tr></thead>
            <tbody>{page.orders.map((order) => <tr key={order.id}>
              <td>{order.name}{order.test && <><br /><small>{t('Test order')}</small></>}{order.cancelled_at && <><br /><small>{t('Cancelled')}</small></>}</td>
              <td>{new Date(order.created_at).toLocaleString()}</td><td>{order.financial_status ?? '—'}</td><td>{order.fulfillment_status}</td>
              <td>{order.item_quantity}</td><td>{order.total_amount} {order.currency}</td>
            </tr>)}</tbody>
          </table></div> : <p>{t('No orders on this page.')}</p>}
        </>}
        {page?.next_page_token && <button type="button" disabled={busy} onClick={() => void loadPage(page.operation === 'orders' ? 'orders' : 'products', true)}>{t('Next page')}</button>}
        {observation && <p className="marketplace-provenance">
          {t('Retrieved at {time}', { time: new Date(observation.retrieved_at).toLocaleString() })} · {t('Request ID')}: {observation.request_id || t('Not supplied')}<br />
          {observation.source_url}
        </p>}
        <MarketplaceProjectAccess connector={connector} selectedShop={storeShop ? { id: storeShop.id, name: storeShop.name } : null} busy={busy} onBusyChange={(value) => {
          pending.current = value
          if (mounted.current) setBusy(value)
        }} />
      </div>
    </article>
  )
}
