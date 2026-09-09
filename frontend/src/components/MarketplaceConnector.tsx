import { useEffect, useRef, useState } from 'react'
import { errorMessage, readMarketplace, revokeConnector, upsertConnector } from '../api/client'
import type { Connector } from '../api/types'
import type { MarketplaceRead } from '../api/marketplaces'
import { useT } from '../i18n'

type Props = { connector: Connector; onChange: (connector: Connector) => void }

export default function MarketplaceConnector({ connector, onChange }: Props) {
  const t = useT()
  const [appKey, setAppKey] = useState('')
  const [appSecret, setAppSecret] = useState('')
  const [accessToken, setAccessToken] = useState('')
  const [expiresAt, setExpiresAt] = useState('')
  const [productPermission, setProductPermission] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [shops, setShops] = useState<MarketplaceRead | null>(null)
  const [products, setProducts] = useState<MarketplaceRead | null>(null)
  const [shopId, setShopId] = useState('')
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

  const clearResults = () => { setShops(null); setProducts(null); setShopId('') }
  const save = () => act(async () => {
    const expires = Math.floor(new Date(expiresAt).getTime() / 1000)
    if (!Number.isFinite(expires) || expires <= Date.now() / 1000 + 30) {
      throw new Error(t('Enter a future access token expiry.'))
    }
    clearResults()
    const updated = await upsertConnector(connector.id, {
      type: connector.type, display_name: connector.display_name,
      endpoint: connector.endpoint, secret_env: connector.secret_env,
      scope: `seller.authorization.info${productPermission ? ' seller.product.basic' : ''}`,
      secret: JSON.stringify({
        app_key: appKey.trim(), app_secret: appSecret.trim(), access_token: accessToken.trim(),
        access_token_expires_at: expires, user_type: 0,
      }),
    })
    if (!mounted.current) return
    setAppKey(''); setAppSecret(''); setAccessToken(''); setExpiresAt('')
    onChange(updated)
    setNotice(t('Credentials saved. Read authorized shops to verify access.'))
  })

  const loadShops = () => act(async () => {
    clearResults()
    const result = await readMarketplace(connector.id, { operation: 'authorized_shops' })
    if (!mounted.current) return
    setShops(result)
    if (result.shops.length === 1) setShopId(result.shops[0].id)
  })

  const loadProducts = (next = false) => act(async () => {
    const cursor = next ? products?.next_page_token : null
    setProducts(null)
    const result = await readMarketplace(connector.id, {
      operation: 'products', shop_id: shopId, page_size: 20,
      ...(cursor ? { page_token: cursor } : {}),
    })
    if (mounted.current) setProducts(result)
  })

  const revoke = () => act(async () => {
    clearResults()
    const updated = await revokeConnector(connector.id)
    if (!mounted.current) return
    setAppKey(''); setAppSecret(''); setAccessToken(''); setExpiresAt('')
    onChange(updated)
  })
  const observation = products ?? shops
  const canReadProducts = connector.scope.split(/\s+/).includes('seller.product.basic')

  return (
    <article className="marketplace-connector" data-enabled={connector.enabled ? 'true' : 'false'}>
      <div className="connector-copy">
        <header><h3>{connector.display_name}</h3><span>{t(connector.enabled ? 'Seller read access' : 'Revoked')}</span></header>
        <p>{t('Read your authorized shops and their product listings. These records do not describe market-wide demand.')}</p>
        <details>
          <summary>{t(connector.enabled ? 'Configure seller access' : 'Reconnect seller access')}</summary>
          <p>{t('Use an existing seller token from your developer app. The app needs seller.authorization.info; product reads also need seller.product.basic.')}</p>
          <form onSubmit={(event) => { event.preventDefault(); void save() }}>
            <fieldset disabled={busy}>
              <label>{t('App key')}<input type="password" autoComplete="off" required maxLength={160} value={appKey} onChange={(event) => setAppKey(event.target.value)} /></label>
              <label>{t('App secret')}<input type="password" autoComplete="new-password" required maxLength={512} value={appSecret} onChange={(event) => setAppSecret(event.target.value)} /></label>
              <label>{t('Seller access token')}<input type="password" autoComplete="new-password" required maxLength={4096} value={accessToken} onChange={(event) => setAccessToken(event.target.value)} /></label>
              <label>{t('Access token expiry (local time)')}<input type="datetime-local" required value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} /></label>
              <label className="marketplace-checkbox"><input type="checkbox" checked={productPermission} onChange={(event) => setProductPermission(event.target.checked)} />{t('My seller authorization includes product read permission.')}</label>
              <button type="submit">{t('Save seller credentials')}</button>
            </fieldset>
          </form>
        </details>
        <div className="marketplace-toolbar">
          <button type="button" disabled={busy || !connector.enabled} onClick={() => void loadShops()}>{t('Read authorized shops')}</button>
          <button type="button" disabled={busy || !connector.enabled} onClick={() => void revoke()}>{t('Revoke')}</button>
          {busy && <span role="status">{t('Working…')}</span>}
        </div>
        {error && <p className="connectors-error" role="alert">{error}</p>}
        {notice && <p role="status">{notice}</p>}
        {shops && (
          <div className="marketplace-shop-picker">
            {shops.shops.length ? <>
              <label>{t('Authorized shop')}<select disabled={busy} value={shopId} onChange={(event) => { setShopId(event.target.value); setProducts(null) }}>
                <option value="">{t('Select a shop')}</option>
                {shops.shops.map((shop) => <option key={shop.id} value={shop.id}>{shop.name} · {shop.region} · {shop.id}</option>)}
              </select></label>
              <button type="button" disabled={busy || !shopId || !canReadProducts} onClick={() => void loadProducts()}>{t('Read products')}</button>
              {!canReadProducts && <p>{t('Configure product read permission to retrieve listings.')}</p>}
            </> : <p>{t('This token returned no authorized shops.')}</p>}
          </div>
        )}
        {products && <>
          {products.products.length ? <div className="marketplace-table"><table>
            <caption>{t('Product listings · current page')}</caption>
            <thead><tr><th>{t('Product')}</th><th>{t('Product ID')}</th><th>{t('Status')}</th></tr></thead>
            <tbody>{products.products.map((product, index) => <tr key={`${product.id}-${index}`}><td>{product.title}</td><td>{product.id}</td><td>{product.status}</td></tr>)}</tbody>
          </table></div> : <p>{t('No products on this page.')}</p>}
          {products.next_page_token && <button type="button" disabled={busy} onClick={() => void loadProducts(true)}>{t('Next product page')}</button>}
        </>}
        {observation && <p className="marketplace-provenance">
          {t('Retrieved at {time}', { time: new Date(observation.retrieved_at).toLocaleString() })} · {t('Request ID')}: {observation.request_id || t('Not supplied')}<br />
          {observation.source_url}
        </p>}
      </div>
    </article>
  )
}
