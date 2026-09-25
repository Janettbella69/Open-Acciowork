import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Connector } from '../api/types'
import type { MarketplaceRead } from '../api/marketplaces'
import { I18nContext } from '../i18n'
import MarketplaceConnector from './MarketplaceConnector'

vi.mock('../api/client', () => ({
  readMarketplace: vi.fn(), upsertConnector: vi.fn(), revokeConnector: vi.fn(),
  getTikTokShopOAuthStatus: vi.fn(), startTikTokShopOAuth: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))
import { getTikTokShopOAuthStatus, readMarketplace, revokeConnector, startTikTokShopOAuth, upsertConnector } from '../api/client'

const connector: Connector = {
  id: 'tiktok_shop', type: 'tiktok_shop', display_name: 'TikTok Shop',
  scope: 'seller.authorization.info seller.product.basic',
  secret_env: 'OPHIRA_CONNECTOR_TIKTOK_SHOP_KEY',
  endpoint: 'https://open-api.tiktokglobalshop.com', enabled: true, health: 'unknown',
  last_checked_at: null, created_at: '2026-09-09T00:00:00Z',
}
const shops: MarketplaceRead = {
  connector_id: 'tiktok_shop', operation: 'authorized_shops', shop_id: null,
  shops: [{ id: 'shop-1', name: 'My shop', region: 'US', seller_type: 'LOCAL' }],
  products: [], next_page_token: null, source_url: 'https://open-api.tiktokglobalshop.com/authorization/202309/shops',
  retrieved_at: '2026-09-09T10:00:00Z', request_id: 'request-1', data_scope: 'authorized_seller_data',
}
const products: MarketplaceRead = {
  ...shops, operation: 'products', shop_id: 'shop-1', shops: [],
  products: [{ id: 'product-1', title: 'Tea kettle', status: 'ACTIVATE' }],
  next_page_token: 'opaque+/=',
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(readMarketplace).mockResolvedValue(shops)
  vi.mocked(upsertConnector).mockResolvedValue(connector)
  vi.mocked(revokeConnector).mockResolvedValue({ ...connector, enabled: false })
  vi.mocked(getTikTokShopOAuthStatus).mockResolvedValue({
    auth_mode: 'manual_token', authorized: false, callback_url: null,
  })
  vi.mocked(startTikTokShopOAuth).mockResolvedValue({
    authorization_url: 'https://services.tiktokshop.com/open/authorize?service_id=123&state=secret',
    callback_url: 'https://oauth.example.com/api/connectors/tiktok_shop/tiktok-shop-oauth/callback',
  })
})

describe('MarketplaceConnector', () => {
  it('reads authorized shops, then selected shop products and the exact next cursor', async () => {
    render(<MarketplaceConnector connector={connector} onChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Read authorized shops' }))
    await screen.findByRole('option', { name: /My shop/ })
    expect(readMarketplace).toHaveBeenLastCalledWith('tiktok_shop', { operation: 'authorized_shops' })
    vi.mocked(readMarketplace).mockResolvedValue(products)
    fireEvent.click(screen.getByRole('button', { name: 'Read products' }))
    await screen.findByText('Tea kettle')
    expect(readMarketplace).toHaveBeenLastCalledWith('tiktok_shop', {
      operation: 'products', shop_id: 'shop-1', page_size: 20,
    })
    vi.mocked(readMarketplace).mockResolvedValue({ ...products, products: [], next_page_token: null })
    fireEvent.click(screen.getByRole('button', { name: 'Next product page' }))
    await screen.findByText('No products on this page.')
    expect(readMarketplace).toHaveBeenLastCalledWith('tiktok_shop', {
      operation: 'products', shop_id: 'shop-1', page_size: 20, page_token: 'opaque+/=',
    })
    expect(screen.queryByText('Tea kettle')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Next product page' })).not.toBeInTheDocument()
    expect(screen.getByText(/Request ID.*request-1/)).toBeInTheDocument()
  })

  it('stores a seller bundle with absolute expiry and clears inputs without claiming verification', async () => {
    const onChange = vi.fn()
    render(<MarketplaceConnector connector={connector} onChange={onChange} />)
    fireEvent.click(screen.getByText('Configure seller access'))
    fireEvent.change(screen.getByLabelText('App key'), { target: { value: 'app-key' } })
    fireEvent.change(screen.getByLabelText('App secret'), { target: { value: 'test-secret' } })
    fireEvent.change(screen.getByLabelText('Seller access token'), { target: { value: 'seller-token' } })
    const expiry = '2030-01-02T10:30'
    fireEvent.change(screen.getByLabelText('Access token expiry (local time)'), { target: { value: expiry } })
    fireEvent.click(screen.getByLabelText('My seller authorization includes product read permission.'))
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    await screen.findByText('Credentials saved. Read authorized shops to verify access.')
    const [, saved] = vi.mocked(upsertConnector).mock.calls[0]
    expect(JSON.parse(saved.secret!)).toEqual({
      app_key: 'app-key', app_secret: 'test-secret', access_token: 'seller-token',
      access_token_expires_at: new Date(expiry).getTime() / 1000, user_type: 0,
    })
    expect(saved.scope).toBe('seller.authorization.info seller.product.basic')
    expect(screen.getByLabelText('Seller access token')).toHaveValue('')
    expect(screen.getByLabelText('App secret')).toHaveValue('')
    expect(readMarketplace).not.toHaveBeenCalled()
    expect(onChange).toHaveBeenCalledWith(connector)
  })

  it('stores seller OAuth app settings and opens the one-time authorization link', async () => {
    vi.mocked(getTikTokShopOAuthStatus).mockResolvedValue({
      auth_mode: 'seller_oauth', authorized: false,
      callback_url: 'https://oauth.example.com/api/connectors/tiktok_shop/tiktok-shop-oauth/callback',
    })
    render(<MarketplaceConnector connector={connector} onChange={vi.fn()} />)
    await screen.findByText('Seller authorization required')
    fireEvent.click(screen.getByText('Configure seller access'))
    fireEvent.change(screen.getByLabelText('App key'), { target: { value: 'app-key' } })
    fireEvent.change(screen.getByLabelText('App secret'), { target: { value: 'test-secret' } })
    fireEvent.change(screen.getByLabelText('Seller authorization link from Partner Center'), {
      target: { value: 'https://services.tiktokshop.com/open/authorize?service_id=123' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    await screen.findByText('App credentials saved. Authorize the seller next.')
    const [, saved] = vi.mocked(upsertConnector).mock.calls[0]
    expect(JSON.parse(saved.secret!)).toEqual({
      auth_mode: 'seller_oauth', app_key: 'app-key', app_secret: 'test-secret',
      authorization_url: 'https://services.tiktokshop.com/open/authorize?service_id=123',
    })
    expect(screen.queryByLabelText('Seller access token')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Authorize TikTok Shop seller' }))
    expect(await screen.findByRole('link', { name: 'Continue to TikTok Shop authorization' }))
      .toHaveAttribute('href', 'https://services.tiktokshop.com/open/authorize?service_id=123&state=secret')
    expect(startTikTokShopOAuth).toHaveBeenCalledWith('tiktok_shop')
  })

  it('disables product reads without configured permission and clears data on revoke', async () => {
    const onChange = vi.fn()
    render(<MarketplaceConnector connector={{ ...connector, scope: 'seller.authorization.info' }} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Read authorized shops' }))
    await screen.findByRole('option', { name: /My shop/ })
    expect(screen.getByRole('button', { name: 'Read products' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ enabled: false })))
    expect(screen.queryByRole('option', { name: /My shop/ })).not.toBeInTheDocument()
    expect(screen.queryByText(/Request ID.*request-1/)).not.toBeInTheDocument()
  })

  it('prevents duplicate requests and does not leave old products visible after a read failure', async () => {
    render(<MarketplaceConnector connector={connector} onChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Read authorized shops' }))
    await screen.findByRole('option', { name: /My shop/ })
    vi.mocked(readMarketplace).mockResolvedValue(products)
    fireEvent.click(screen.getByRole('button', { name: 'Read products' }))
    await screen.findByText('Tea kettle')
    let rejectRead!: (error: Error) => void
    vi.mocked(readMarketplace).mockImplementation(() => new Promise((_, reject) => { rejectRead = reject }))
    const button = screen.getByRole('button', { name: 'Next product page' })
    fireEvent.click(button)
    fireEvent.click(button)
    expect(readMarketplace).toHaveBeenCalledTimes(3)
    expect(screen.getByRole('button', { name: 'Revoke' })).toBeDisabled()
    rejectRead(new Error('Seller authorization expired'))
    await screen.findByRole('alert')
    expect(screen.queryByText('Tea kettle')).not.toBeInTheDocument()
  })

  it('offers reconnect after revoke and translates the seller flow', async () => {
    render(<I18nContext.Provider value="zh-CN"><MarketplaceConnector connector={{ ...connector, enabled: false }} onChange={vi.fn()} /></I18nContext.Provider>)
    await act(async () => {})
    expect(screen.getByText('重新配置商家授权')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '读取已授权店铺' })).toBeDisabled()
    expect(screen.getByLabelText('商家 Access token')).toHaveAttribute('type', 'password')
  })
})
