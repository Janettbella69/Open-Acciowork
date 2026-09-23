import { useState } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Connector, OpportunityProject } from '../api/types'
import type { MarketplaceRead } from '../api/marketplaces'
import { I18nContext } from '../i18n'
import ShopeeConnector from './ShopeeConnector'

vi.mock('../api/client', () => ({
  readMarketplace: vi.fn(), upsertConnector: vi.fn(), revokeConnector: vi.fn(), listConnectors: vi.fn(),
  listProjects: vi.fn(), getMarketplaceAccess: vi.fn(), grantMarketplaceAccess: vi.fn(), revokeMarketplaceAccess: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))
import { getMarketplaceAccess, grantMarketplaceAccess, listConnectors, listProjects, readMarketplace, revokeConnector, upsertConnector } from '../api/client'

const shopId = '123456789012345678'
const connector: Connector = {
  id: 'shopee', type: 'shopee', display_name: 'Shopee', scope: 'shopee:shop:read shopee:products:read',
  secret_env: 'OPHIRA_CONNECTOR_SHOPEE_KEY', endpoint: 'https://partner.shopeemobile.com',
  enabled: true, health: 'unknown', last_checked_at: null, created_at: '2026-09-22T00:00:00Z',
}
const shop: MarketplaceRead = {
  connector_id: 'shopee', operation: 'authorized_shops', shop_id: null,
  shops: [{ id: shopId, name: 'Synthetic shop', region: 'SG', seller_type: '' }],
  products: [], next_page_token: null,
  source_url: 'https://partner.shopeemobile.com/api/v2/shop/get_shop_info',
  retrieved_at: '2026-09-22T10:00:00Z', request_id: 'synthetic-request', data_scope: 'authorized_seller_data',
}
const products: MarketplaceRead = {
  ...shop, operation: 'products', shop_id: shopId, shops: [], total_count: 12, next_page_token: '10',
  products: [{ id: '34002', title: 'Kettle', status: 'NORMAL', sku: 'KETTLE', has_variants: true, details_available: true }],
}
const detail: MarketplaceRead = {
  ...products, operation: 'product_details', next_page_token: null,
  products: [{ ...products.products[0], variants_loaded: true, variants: [
    { id: '1002', title: 'Red', sku: 'RED', status: 'MODEL_NORMAL',
      prices: [{ currency: 'SGD', current: '12.35000001', original: '14' }],
      stock: { total_available_stock: 8, total_reserved_stock: null } },
  ] }],
}

function Harness({ initial = connector, onChange = vi.fn() }: { initial?: Connector; onChange?: (value: Connector) => void }) {
  const [value, setValue] = useState(initial)
  return <ShopeeConnector connector={value} onChange={(updated) => { setValue(updated); onChange(updated) }} />
}
function fillCredentials(withRefresh = true) {
  fireEvent.click(screen.getByText('Configure seller access'))
  for (const [label, value] of [
    ['Partner ID', '2001887'], ['Shop ID', shopId], ['Partner key', 'synthetic-key'],
    ['Seller access token', 'synthetic-token'], ['Access token expiry (local time)', '2030-01-01T10:00'],
    ...(withRefresh ? [['Refresh token (optional)', 'synthetic-refresh'], ['Refresh token expiry (local time)', '2030-01-20T10:00']] : []),
  ]) fireEvent.change(screen.getByLabelText(label), { target: { value } })
}
async function readShop() {
  fireEvent.click(screen.getByRole('button', { name: 'Read store' }))
  await screen.findByText('Synthetic shop')
}
async function readProducts() {
  await readShop()
  vi.mocked(readMarketplace).mockResolvedValue(products)
  fireEvent.click(screen.getByRole('button', { name: 'Read products' }))
  await screen.findByText('Kettle')
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(readMarketplace).mockResolvedValue(shop)
  vi.mocked(upsertConnector).mockImplementation(async (_, input) => ({ ...connector, endpoint: input.endpoint ?? null, scope: input.scope ?? '' }))
  vi.mocked(revokeConnector).mockResolvedValue({ ...connector, enabled: false })
  vi.mocked(listConnectors).mockResolvedValue([connector])
  vi.mocked(listProjects).mockResolvedValue([{ id: 'project-1', name: 'Launch' }] as OpportunityProject[])
  vi.mocked(getMarketplaceAccess).mockResolvedValue({ project_id: 'project-1', revision: 0, bindings: [] })
  vi.mocked(grantMarketplaceAccess).mockResolvedValue({ project_id: 'project-1', revision: 1, bindings: [] })
})

describe('ShopeeConnector', () => {
  it('saves exact int64 shop IDs and masked optional tokens only on submission', async () => {
    render(<Harness />)
    fillCredentials()
    for (const label of ['Partner key', 'Seller access token', 'Refresh token (optional)']) {
      expect(screen.getByLabelText(label)).toHaveAttribute('type', 'password')
    }
    expect(upsertConnector).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Shopee API origin'), { target: { value: 'https://openplatform.shopee.cn' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    await screen.findByText('Credentials saved. Read the store to verify access.')
    const input = vi.mocked(upsertConnector).mock.calls[0][1]
    expect(input.endpoint).toBe('https://openplatform.shopee.cn')
    expect(JSON.parse(input.secret!)).toEqual({
      partner_id: '2001887', shop_id: shopId, partner_key: 'synthetic-key', access_token: 'synthetic-token',
      access_token_expires_at: new Date('2030-01-01T10:00').getTime() / 1000,
      refresh_token: 'synthetic-refresh', refresh_token_expires_at: new Date('2030-01-20T10:00').getTime() / 1000,
    })
    for (const label of ['Partner key', 'Seller access token', 'Refresh token (optional)']) expect(screen.getByLabelText(label)).toHaveValue('')
    expect(readMarketplace).not.toHaveBeenCalled()
  })

  it('supports a token without refresh and validates oversized IDs locally', async () => {
    render(<Harness />)
    fillCredentials(false)
    fireEvent.change(screen.getByLabelText('Shop ID'), { target: { value: '9223372036854775808' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    await screen.findByRole('alert')
    expect(upsertConnector).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Shop ID'), { target: { value: shopId } })
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    await screen.findByText('Credentials saved. Read the store to verify access.')
    expect(JSON.parse(vi.mocked(upsertConnector).mock.calls[0][1].secret!)).not.toHaveProperty('refresh_token')
  })

  it('rejects an expired refresh token before mutation', async () => {
    render(<Harness />)
    fillCredentials()
    fireEvent.change(screen.getByLabelText('Refresh token expiry (local time)'), { target: { value: '2020-01-01T10:00' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('A refresh token needs its future expiry time.')
    expect(upsertConnector).not.toHaveBeenCalled()
  })

  it('requires a status refresh after an ambiguous save and clears secrets', async () => {
    vi.mocked(upsertConnector).mockRejectedValue(new Error('Connection lost'))
    render(<Harness />)
    fillCredentials()
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    await screen.findByRole('alert')
    expect(screen.getByLabelText('Partner key')).toHaveValue('')
    expect(screen.getByLabelText('Refresh token (optional)')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Read store' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh connector status' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Read store' })).toBeEnabled())
    expect(readMarketplace).not.toHaveBeenCalled()
  })

  it('reads details explicitly and keeps exact currency amounts and unknown stock', async () => {
    render(<Harness />)
    await readProducts()
    expect(readMarketplace).toHaveBeenLastCalledWith('shopee', { operation: 'products', shop_id: shopId, page_size: 10, product_status: 'ALL' })
    expect(screen.getByText('Read SKU details for variant prices and stock.')).toBeInTheDocument()
    vi.mocked(readMarketplace).mockResolvedValue(detail)
    fireEvent.click(screen.getByRole('button', { name: 'Read SKU details for 34002' }))
    const region = await screen.findByRole('region', { name: 'SKU details' })
    expect(readMarketplace).toHaveBeenLastCalledWith('shopee', { operation: 'product_details', shop_id: shopId, product_id: '34002', page_size: 10 })
    const row = within(region).getByText('RED').closest('tr')!
    expect(row).toHaveTextContent('SGD 12.35000001 (Original price: 14)')
    expect(row).toHaveTextContent('Available stock: 8 · Reserved stock: Unknown')
    expect(row).not.toHaveTextContent('Reserved stock: 0')
  })

  it('paginates variants with the same product and an explicit returned SKU offset', async () => {
    render(<Harness />)
    await readProducts()
    vi.mocked(readMarketplace).mockResolvedValue({ ...detail, pagination_unit: 'SKU', total_count: 20, next_page_token: '10' })
    fireEvent.click(screen.getByRole('button', { name: 'Read SKU details for 34002' }))
    await screen.findByRole('button', { name: 'Next SKU page' })
    vi.mocked(readMarketplace).mockResolvedValue(detail)
    fireEvent.click(screen.getByRole('button', { name: 'Next SKU page' }))
    await waitFor(() => expect(readMarketplace).toHaveBeenLastCalledWith('shopee', {
      operation: 'product_details', shop_id: shopId, product_id: '34002', page_size: 10, page_token: '10',
    }))
    await screen.findByRole('table', { name: 'SKU details · current page' })
    expect(screen.queryByRole('button', { name: 'Next SKU page' })).not.toBeInTheDocument()
  })

  it('uses returned offsets and resets them when the status filter changes', async () => {
    render(<Harness />)
    await readProducts()
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    await waitFor(() => expect(readMarketplace).toHaveBeenLastCalledWith('shopee', { operation: 'products', shop_id: shopId, page_size: 10, product_status: 'ALL', page_token: '10' }))
    await screen.findByText('Kettle')
    fireEvent.change(screen.getByLabelText('Product status'), { target: { value: 'UNLIST' } })
    expect(screen.queryByText('Kettle')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Read products' }))
    await waitFor(() => expect(readMarketplace).toHaveBeenLastCalledWith('shopee', { operation: 'products', shop_id: shopId, page_size: 10, product_status: 'UNLIST' }))
  })

  it('shows incomplete items without inventing titles or prices', async () => {
    render(<Harness />)
    await readShop()
    vi.mocked(readMarketplace).mockResolvedValue({ ...products, partial_data: true, products: [{ id: '34002', title: '', status: 'NORMAL', details_available: false }] })
    fireEvent.click(screen.getByRole('button', { name: 'Read products' }))
    await screen.findByText('Details not supplied')
    expect(screen.getByText('Title not supplied')).toBeInTheDocument()
    expect(screen.getByText(/Some requested data was not returned/)).toBeInTheDocument()
  })

  it('locks concurrent operations and withholds late results after unmount', async () => {
    let finish!: (value: MarketplaceRead) => void
    vi.mocked(readMarketplace).mockReturnValue(new Promise((resolve) => { finish = resolve }))
    const { unmount } = render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Read store' }))
    expect(screen.getByRole('button', { name: 'Revoke' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Read store' }))
    expect(readMarketplace).toHaveBeenCalledTimes(1)
    unmount()
    await act(async () => finish(shop))
    expect(screen.queryByText('Synthetic shop')).not.toBeInTheDocument()
  })

  it('clears observations and secrets on revoke, retaining explicit reconnect', async () => {
    render(<Harness />)
    await readProducts()
    fillCredentials()
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
    await screen.findByText('Revoked')
    expect(screen.queryByText('Kettle')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Seller access token')).toHaveValue('')
    expect(screen.getByText('Reconnect seller access')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Read store' })).toBeDisabled()
  })

  it('grants the exact selected shop through the existing project access flow', async () => {
    render(<Harness />)
    await readShop()
    fireEvent.click(screen.getByText('Project access for Agents'))
    await screen.findByRole('option', { name: 'Launch' })
    fireEvent.change(screen.getByLabelText('Project for shop access'), { target: { value: 'project-1' } })
    const button = screen.getByRole('button', { name: 'Allow Agent reads for selected shop' })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => expect(grantMarketplaceAccess).toHaveBeenCalledWith('project-1', {
      connector_id: 'shopee', shop_id: shopId, expected_revision: 0, expires_in_seconds: 7 * 86400,
    }))
  })

  it('provides Chinese configuration and observation labels', () => {
    render(<I18nContext.Provider value="zh-CN"><Harness /></I18nContext.Provider>)
    expect(screen.getByText('允许读取 Shopee 商品')).toBeInTheDocument()
    expect(screen.getByLabelText('刷新令牌（选填）')).toHaveAttribute('type', 'password')
    expect(screen.getByRole('button', { name: '读取店铺' })).toBeInTheDocument()
    expect(screen.getByText(/刷新中断后需更换整组令牌/)).toBeInTheDocument()
  })
})
