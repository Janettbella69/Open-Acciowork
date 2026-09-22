import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Connector } from '../api/types'
import type { MarketplaceRead } from '../api/marketplaces'
import { I18nContext } from '../i18n'
import ShopifyConnector from './ShopifyConnector'

vi.mock('../api/client', () => ({
  readMarketplace: vi.fn(), upsertConnector: vi.fn(), revokeConnector: vi.fn(),
  listProjects: vi.fn(), getMarketplaceAccess: vi.fn(), grantMarketplaceAccess: vi.fn(),
  revokeMarketplaceAccess: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))
import { readMarketplace, revokeConnector, upsertConnector } from '../api/client'

const connector: Connector = {
  id: 'shopify', type: 'shopify', display_name: 'Shopify', scope: 'read_products read_orders',
  secret_env: 'OPHIRA_CONNECTOR_SHOPIFY_KEY', endpoint: 'https://skate-demo.myshopify.com',
  enabled: true, health: 'unknown', last_checked_at: null, created_at: '2026-09-10T00:00:00Z',
}
const shop: MarketplaceRead = {
  connector_id: 'shopify', operation: 'authorized_shops', shop_id: null,
  shops: [{ id: '74185244861', name: 'Skate Demo', region: 'US', seller_type: '', domain: 'skate-demo.myshopify.com', currency: 'USD' }],
  products: [], orders: [], granted_scopes: ['read_products'], next_page_token: null,
  source_url: 'https://skate-demo.myshopify.com/admin/api/2026-07/graphql.json',
  retrieved_at: '2026-09-10T10:00:00Z', request_id: 'req-1', data_scope: 'authorized_seller_data',
}
const products: MarketplaceRead = {
  ...shop, operation: 'products', shop_id: '74185244861', shops: [],
  products: [{ id: '901', title: 'Maple deck', status: 'ACTIVE', vendor: 'Skate Demo', product_type: 'Deck', updated_at: '2026-09-01T10:00:00Z' }],
  next_page_token: 'cursor+/=',
}
const orders: MarketplaceRead = {
  ...shop, operation: 'orders', shop_id: '74185244861', shops: [], order_window: 'last_60_days',
  orders: [{ id: '5001', name: '#1001', created_at: '2026-09-02T08:30:00Z', cancelled_at: null, test: true, financial_status: 'PAID', fulfillment_status: 'UNFULFILLED', item_quantity: 2, total_amount: '129.90', currency: 'USD' }],
}

function fillCredentials(store: string) {
  fireEvent.click(screen.getByText('Configure store access'))
  fireEvent.change(screen.getByLabelText('Store address'), { target: { value: store } })
  fireEvent.change(screen.getByLabelText('Client ID'), { target: { value: 'client-id-0123456789abcdef' } })
  fireEvent.change(screen.getByLabelText('Client secret'), { target: { value: 'shpss_test-secret-0123456789' } })
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(readMarketplace).mockResolvedValue(shop)
  vi.mocked(upsertConnector).mockResolvedValue(connector)
  vi.mocked(revokeConnector).mockResolvedValue({ ...connector, enabled: false })
})

describe('ShopifyConnector', () => {
  it('normalizes the store address and saves client credentials without keeping them in the form', async () => {
    const onChange = vi.fn()
    render(<ShopifyConnector connector={{ ...connector, endpoint: null }} onChange={onChange} />)
    fillCredentials('https://admin.shopify.com/store/Skate-Demo/products')
    fireEvent.click(screen.getByLabelText('Allow recent order reads (read_orders)'))
    fireEvent.click(screen.getByRole('button', { name: 'Save store credentials' }))
    await screen.findByText('Credentials saved. Read the store to verify access.')
    const [id, saved] = vi.mocked(upsertConnector).mock.calls[0]
    expect(id).toBe('shopify')
    expect(saved.endpoint).toBe('https://skate-demo.myshopify.com')
    expect(saved.scope).toBe('read_products')
    expect(JSON.parse(saved.secret!)).toEqual({
      client_id: 'client-id-0123456789abcdef', client_secret: 'shpss_test-secret-0123456789',
    })
    expect(screen.getByLabelText('Client secret')).toHaveValue('')
    expect(screen.getByLabelText('Client ID')).toHaveValue('')
    expect(screen.getByLabelText('Store address')).toHaveValue('skate-demo.myshopify.com')
    expect(readMarketplace).not.toHaveBeenCalled()
    expect(onChange).toHaveBeenCalledWith(connector)
  })

  it('rejects an address that is not a Shopify store before calling the API', async () => {
    render(<ShopifyConnector connector={connector} onChange={vi.fn()} />)
    fillCredentials('evil.test/store')
    fireEvent.click(screen.getByRole('button', { name: 'Save store credentials' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter the store address, such as your-store.myshopify.com.')
    expect(upsertConnector).not.toHaveBeenCalled()
  })

  it('reads the store, enables only granted reads and pages products with the exact cursor', async () => {
    render(<ShopifyConnector connector={connector} onChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Read store' }))
    await screen.findByText('Skate Demo')
    expect(readMarketplace).toHaveBeenLastCalledWith('shopify', { operation: 'authorized_shops' })
    expect(screen.getByText('Granted scopes: read_products')).toBeInTheDocument()
    expect(screen.getByText('Add read_orders to a released app version and approve it on the store.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Read recent orders' })).toBeDisabled()
    vi.mocked(readMarketplace).mockResolvedValue(products)
    fireEvent.click(screen.getByRole('button', { name: 'Read products' }))
    await screen.findByText('Maple deck')
    expect(readMarketplace).toHaveBeenLastCalledWith('shopify', {
      operation: 'products', shop_id: '74185244861', page_size: 20,
    })
    vi.mocked(readMarketplace).mockResolvedValue({ ...products, products: [], next_page_token: null })
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    await screen.findByText('No products on this page.')
    expect(readMarketplace).toHaveBeenLastCalledWith('shopify', {
      operation: 'products', shop_id: '74185244861', page_size: 20, page_token: 'cursor+/=',
    })
    expect(screen.queryByText('Maple deck')).not.toBeInTheDocument()
  })

  it('shows order pages with the 60-day window and warns about unused write scopes', async () => {
    vi.mocked(readMarketplace).mockResolvedValue({
      ...shop, granted_scopes: ['read_orders', 'read_products', 'write_products'],
    })
    render(<ShopifyConnector connector={connector} onChange={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Read store' }))
    await screen.findByText(/also holds write scopes/)
    vi.mocked(readMarketplace).mockResolvedValue(orders)
    fireEvent.click(screen.getByRole('button', { name: 'Read recent orders' }))
    await screen.findByText('#1001')
    expect(readMarketplace).toHaveBeenLastCalledWith('shopify', {
      operation: 'orders', shop_id: '74185244861', page_size: 20,
    })
    expect(screen.getByText('Shopify returns only the last 60 days of orders unless the app is granted read_all_orders.')).toBeInTheDocument()
    expect(screen.getByText('Test order')).toBeInTheDocument()
    expect(screen.getByText('129.90 USD')).toBeInTheDocument()
  })

  it('clears store data on revoke', async () => {
    const onChange = vi.fn()
    render(<ShopifyConnector connector={connector} onChange={onChange} />)
    fireEvent.click(screen.getByRole('button', { name: 'Read store' }))
    await screen.findByText('Skate Demo')
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ enabled: false })))
    expect(screen.queryByText('Skate Demo')).not.toBeInTheDocument()
    expect(screen.queryByText(/Request ID/)).not.toBeInTheDocument()
  })

  it('offers reconnect after revoke and translates the store flow', () => {
    render(<I18nContext.Provider value="zh-CN"><ShopifyConnector connector={{ ...connector, enabled: false }} onChange={vi.fn()} /></I18nContext.Provider>)
    expect(screen.getByText('重新配置店铺授权')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '读取店铺' })).toBeDisabled()
    expect(screen.getByLabelText('应用 Client secret')).toHaveAttribute('type', 'password')
  })
})
