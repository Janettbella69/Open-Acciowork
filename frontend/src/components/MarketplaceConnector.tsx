import { useEffect, useRef, useState } from 'react'
import { errorMessage, getTikTokShopOAuthStatus, readMarketplace, revokeConnector, startTikTokShopOAuth, upsertConnector } from '../api/client'
import type { TikTokShopOAuthStatus } from '../api/client'
import type { Connector } from '../api/types'
import type { MarketplaceRead } from '../api/marketplaces'
import { useT } from '../i18n'
import MarketplaceProjectAccess from './MarketplaceProjectAccess'

type Props = { connector: Connector; onChange: (connector: Connector) => void }

export default function MarketplaceConnector({ connector, onChange }: Props) {
  const t = useT()
  const [appKey, setAppKey] = useState('')
  const [appSecret, setAppSecret] = useState('')
  const [accessToken, setAccessToken] = useState('')
  const [expiresAt, setExpiresAt] = useState('')
  const [authorizationLink, setAuthorizationLink] = useState('')
  const [authMode, setAuthMode] = useState<TikTokShopOAuthStatus['auth_mode']>('manual_token')
  const [oauthStatus, setOauthStatus] = useState<TikTokShopOAuthStatus | null>(null)
  const [authorizationUrl, setAuthorizationUrl] = useState('')
  const [productPermission, setProductPermission] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [shops, setShops] = useState<MarketplaceRead | null>(null)
  const [products, setProducts] = useState<MarketplaceRead | null>(null)
  const [shopId, setShopId] = useState('')
  const pending = useRef(false)
  const mounted = useRef(true)
  const modeTouched = useRef(false)

  useEffect(() => {
    mounted.current = true
    void getTikTokShopOAuthStatus(connector.id).then((status) => {
      if (mounted.current) {
        setOauthStatus(status)
        if (!modeTouched.current) setAuthMode(status.auth_mode)
      }
    }).catch((reason) => { if (mounted.current) setError(errorMessage(reason)) })
    return () => { mounted.current = false }
  }, [connector.id])

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
    if (authMode === 'manual_token' && (!Number.isFinite(expires) || expires <= Date.now() / 1000 + 30)) {
      throw new Error(t('Enter a future access token expiry.'))
    }
    clearResults()
    const updated = await upsertConnector(connector.id, {
      type: connector.type, display_name: connector.display_name,
      endpoint: connector.endpoint, secret_env: connector.secret_env,
      scope: `seller.authorization.info${productPermission ? ' seller.product.basic' : ''}`,
      secret: JSON.stringify(authMode === 'seller_oauth' ? {
        auth_mode: 'seller_oauth', app_key: appKey.trim(), app_secret: appSecret.trim(),
        authorization_url: authorizationLink.trim(),
      } : {
        app_key: appKey.trim(), app_secret: appSecret.trim(), access_token: accessToken.trim(),
        access_token_expires_at: expires, user_type: 0,
      }),
    })
    if (!mounted.current) return
    setAppKey(''); setAppSecret(''); setAccessToken(''); setExpiresAt(''); setAuthorizationLink('')
    setAuthorizationUrl('')
    setOauthStatus((current) => ({ auth_mode: authMode, authorized: false, callback_url: current?.callback_url ?? null }))
    onChange(updated)
    setNotice(t(authMode === 'seller_oauth'
      ? 'App credentials saved. Authorize the seller next.'
      : 'Credentials saved. Read authorized shops to verify access.'))
  })

  const authorizeSeller = () => act(async () => {
    const result = await startTikTokShopOAuth(connector.id)
    if (mounted.current) setAuthorizationUrl(result.authorization_url)
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
    setAuthorizationUrl('')
    setOauthStatus((current) => current ? { ...current, authorized: false } : null)
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
          <p>{t('The app needs seller.authorization.info; product reads also need seller.product.basic.')}</p>
          {oauthStatus?.callback_url && <p>{t('TikTok Shop OAuth callback URL')}: <code>{oauthStatus.callback_url}</code></p>}
          <form onSubmit={(event) => { event.preventDefault(); void save() }}>
            <fieldset disabled={busy}>
              <label>{t('Seller authorization method')}<select value={authMode} onChange={(event) => { modeTouched.current = true; setAuthMode(event.target.value as TikTokShopOAuthStatus['auth_mode']); setAuthorizationUrl('') }}>
                <option value="manual_token">{t('Existing seller token')}</option>
                <option value="seller_oauth">{t('Seller authorization with refresh')}</option>
              </select></label>
              <label>{t('App key')}<input type="password" autoComplete="off" required maxLength={160} value={appKey} onChange={(event) => setAppKey(event.target.value)} /></label>
              <label>{t('App secret')}<input type="password" autoComplete="new-password" required maxLength={512} value={appSecret} onChange={(event) => setAppSecret(event.target.value)} /></label>
              {authMode === 'seller_oauth' ?
                <label>{t('Seller authorization link from Partner Center')}<input type="url" autoComplete="off" required maxLength={500} placeholder="https://services.tiktokshop.com/open/authorize?service_id=..." value={authorizationLink} onChange={(event) => setAuthorizationLink(event.target.value)} /></label> : <>
                  <label>{t('Seller access token')}<input type="password" autoComplete="new-password" required maxLength={4096} value={accessToken} onChange={(event) => setAccessToken(event.target.value)} /></label>
                  <label>{t('Access token expiry (local time)')}<input type="datetime-local" required value={expiresAt} onChange={(event) => setExpiresAt(event.target.value)} /></label>
                </>}
              <label className="marketplace-checkbox"><input type="checkbox" checked={productPermission} onChange={(event) => setProductPermission(event.target.checked)} />{t('My seller authorization includes product read permission.')}</label>
              <button type="submit">{t('Save seller credentials')}</button>
            </fieldset>
          </form>
        </details>
        <div className="marketplace-toolbar">
          {authMode === 'seller_oauth' && <button type="button" disabled={busy || !connector.enabled || oauthStatus?.auth_mode !== 'seller_oauth'} onClick={() => void authorizeSeller()}>{t('Authorize TikTok Shop seller')}</button>}
          <button type="button" disabled={busy || !connector.enabled} onClick={() => void loadShops()}>{t('Read authorized shops')}</button>
          <button type="button" disabled={busy || !connector.enabled} onClick={() => void revoke()}>{t('Revoke')}</button>
          {busy && <span role="status">{t('Working…')}</span>}
        </div>
        {authMode === 'seller_oauth' && <p>{t(oauthStatus?.authorized ? 'Seller authorization active' : 'Seller authorization required')}</p>}
        {authorizationUrl && <p><a href={authorizationUrl} referrerPolicy="no-referrer">{t('Continue to TikTok Shop authorization')}</a></p>}
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
        <MarketplaceProjectAccess connector={connector} selectedShop={shops?.shops.find((shop) => shop.id === shopId) ?? null} busy={busy} onBusyChange={(value) => {
          pending.current = value
          if (mounted.current) setBusy(value)
        }} />
      </div>
    </article>
  )
}
