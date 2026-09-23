import { useState } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Connector, OpportunityProject } from '../api/types'
import type { MarketplaceRead } from '../api/marketplaces'
import { I18nContext } from '../i18n'
import AliExpressConnector from './AliExpressConnector'

vi.mock('../api/client', () => ({
  readMarketplace: vi.fn(), upsertConnector: vi.fn(), revokeConnector: vi.fn(), listConnectors: vi.fn(),
  listProjects: vi.fn(), getMarketplaceAccess: vi.fn(), grantMarketplaceAccess: vi.fn(), revokeMarketplaceAccess: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))
import { getMarketplaceAccess, grantMarketplaceAccess, listConnectors, listProjects, readMarketplace, revokeConnector, upsertConnector } from '../api/client'

const sellerId = '123456789012345678'
const connector: Connector = {
  id: 'aliexpress', type: 'aliexpress', display_name: 'AliExpress', scope: 'aliexpress:seller:read aliexpress:products:read',
  secret_env: 'OPHIRA_CONNECTOR_ALIEXPRESS_KEY', endpoint: 'https://api-sg.aliexpress.com',
  enabled: true, health: 'unknown', last_checked_at: null, created_at: '2026-09-22T00:00:00Z',
}
const shop: MarketplaceRead = {
  connector_id: 'aliexpress', operation: 'authorized_shops', shop_id: null,
  shops: [{ id: sellerId, name: '', region: 'CN', seller_type: '', identity_kind: 'seller_account' }],
  products: [], next_page_token: null, source_url: 'https://api-sg.aliexpress.com/sync',
  retrieved_at: '2026-09-22T10:00:00Z', request_id: '', data_scope: 'authorized_seller_data',
  authorization_expires_at: '2030-01-01T10:00:00Z', api_name: 'aliexpress.merchant.profile.get',
}
const products: MarketplaceRead = {
  ...shop, operation: 'products', shop_id: sellerId, shops: [], total_count: 12, page_number: 1, total_pages: 2, next_page_token: '2',
  products: [{ id: '1005004616872521', title: 'Kettle', status: 'onSelling', product_type: 'POP', details_available: true,
    price_range: { currency: 'USD', min: '10.23000001', max: '20.07' } }],
}
const detail: MarketplaceRead = {
  ...products, operation: 'product_details', next_page_token: null, total_count: 1,
  products: [{ ...products.products[0], variants_loaded: true, has_country_pricing: true, variants: [
    { id: '123123123123123123', id_kind: 'platform', title: 'Red', sku: 'RED', status: 'UNKNOWN',
      listed_price: { currency: 'USD', amount: '20.07' }, daily_discount_price: { currency: 'USD', amount: '15.00000001' },
      country_discount_prices: [{ country: 'RU', quote: { currency: 'USD', amount: '14' } }],
      stock: { total_available_stock: null, total_reserved_stock: null }, in_stock: false },
  ] }],
}

function Harness({ initial = connector, onChange = vi.fn() }: { initial?: Connector; onChange?: (value: Connector) => void }) {
  const [value, setValue] = useState(initial)
  return <AliExpressConnector connector={value} onChange={(updated) => { setValue(updated); onChange(updated) }} />
}
function fillCredentials() {
  fireEvent.click(screen.getByText('Configure seller access'))
  for (const [label, value] of [
    ['App key', 'synthetic-key'], ['App secret', 'synthetic-secret'], ['Seller access token', 'synthetic-token'],
    ['Seller ID', sellerId], ['Access token expiry (local time)', '2030-01-01T10:00'],
  ]) fireEvent.change(screen.getByLabelText(label), { target: { value } })
}
async function readShop() {
  fireEvent.click(screen.getByRole('button', { name: 'Read seller account' }))
  await screen.findByText(`Seller ID: ${sellerId}`)
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
  vi.mocked(upsertConnector).mockImplementation(async (_, input) => ({ ...connector, scope: input.scope ?? '' }))
  vi.mocked(revokeConnector).mockResolvedValue({ ...connector, enabled: false })
  vi.mocked(listConnectors).mockResolvedValue([connector])
  vi.mocked(listProjects).mockResolvedValue([{ id: 'project-1', name: 'Launch' }] as OpportunityProject[])
  vi.mocked(getMarketplaceAccess).mockResolvedValue({ project_id: 'project-1', revision: 0, bindings: [] })
  vi.mocked(grantMarketplaceAccess).mockResolvedValue({ project_id: 'project-1', revision: 1, bindings: [] })
})

describe('AliExpressConnector', () => {
  it('saves masked credentials, exact seller ID and configured expiry on explicit submission', async () => {
    render(<Harness />)
    fillCredentials()
    for (const label of ['App key', 'App secret', 'Seller access token']) expect(screen.getByLabelText(label)).toHaveAttribute('type', 'password')
    expect(upsertConnector).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    await screen.findByText('Credentials saved. Read the seller account to verify access.')
    const input = vi.mocked(upsertConnector).mock.calls[0][1]
    expect(input.endpoint).toBe('https://api-sg.aliexpress.com')
    expect(input.scope).toBe('aliexpress:seller:read aliexpress:products:read')
    expect(JSON.parse(input.secret!)).toEqual({ app_key: 'synthetic-key', app_secret: 'synthetic-secret', access_token: 'synthetic-token', seller_id: sellerId, access_token_expires_at: new Date('2030-01-01T10:00').getTime() / 1000 })
    for (const label of ['App key', 'App secret', 'Seller access token']) expect(screen.getByLabelText(label)).toHaveValue('')
    expect(readMarketplace).not.toHaveBeenCalled()
  })

  it.each([['Seller ID', '9223372036854775808'], ['Access token expiry (local time)', '2000-01-01T10:00']])('rejects invalid %s before saving', async (label, value) => {
    render(<Harness />)
    fillCredentials()
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    await screen.findByRole('alert')
    expect(upsertConnector).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Read seller account' })).toBeEnabled()
  })

  it('saves seller-only permission and prevents product reads', async () => {
    render(<Harness />)
    fillCredentials()
    fireEvent.click(screen.getByLabelText('Allow AliExpress product reads'))
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    await screen.findByText('Credentials saved. Read the seller account to verify access.')
    expect(vi.mocked(upsertConnector).mock.calls[0][1].scope).toBe('aliexpress:seller:read')
    await readShop()
    expect(screen.getByRole('button', { name: 'Read products' })).toBeDisabled()
  })

  it('clears secrets and requires refresh after a save with an uncertain outcome', async () => {
    vi.mocked(upsertConnector).mockRejectedValue(new Error('Connection interrupted'))
    render(<Harness />)
    fillCredentials()
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    await screen.findByRole('alert')
    expect(screen.getByLabelText('App secret')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Read seller account' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh connector status' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Read seller account' })).toBeEnabled())
    expect(readMarketplace).not.toHaveBeenCalled()
  })

  it('distinguishes seller identity and reads only the explicitly selected status', async () => {
    render(<Harness />)
    await readProducts()
    expect(screen.getByText(/Seller ID is not a storefront URL or store ID/)).toBeInTheDocument()
    expect(screen.getByText(/Configured token expiry/)).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'All statuses' })).not.toBeInTheDocument()
    expect(readMarketplace).toHaveBeenLastCalledWith('aliexpress', { operation: 'products', shop_id: sellerId, page_size: 10, product_status: 'onSelling' })
    expect(screen.getByText('USD 10.23000001 – 20.07')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    await waitFor(() => expect(readMarketplace).toHaveBeenLastCalledWith('aliexpress', { operation: 'products', shop_id: sellerId, page_size: 10, product_status: 'onSelling', page_token: '2' }))
    await screen.findByText('Kettle')
    fireEvent.change(screen.getByLabelText('Product status'), { target: { value: 'editingRequired' } })
    expect(screen.queryByText('Kettle')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Read products' }))
    await waitFor(() => expect(readMarketplace).toHaveBeenLastCalledWith('aliexpress', { operation: 'products', shop_id: sellerId, page_size: 10, product_status: 'editingRequired' }))
  })

  it('keeps listing, daily and country prices separate and does not turn a false stock flag into zero', async () => {
    render(<Harness />)
    await readProducts()
    vi.mocked(readMarketplace).mockResolvedValue(detail)
    fireEvent.click(screen.getByRole('button', { name: 'Read SKU details for 1005004616872521' }))
    const region = await screen.findByRole('region', { name: 'SKU details' })
    const row = within(region).getByText('RED').closest('tr')!
    expect(row).toHaveTextContent('USD 20.07')
    expect(row).toHaveTextContent('USD 15.00000001')
    expect(row).toHaveTextContent('RU: USD 14')
    expect(row).toHaveTextContent('Available stock: Unknown')
    expect(row).toHaveTextContent('In-stock flag: Out of stock')
    expect(row).not.toHaveTextContent('Available stock: 0')
    expect(screen.getByText(/Displayed base quotes do not resolve those rules/)).toBeInTheDocument()
  })

  it('displays unknown currency and property combinations without inventing a platform SKU ID', async () => {
    render(<Harness />)
    await readProducts()
    vi.mocked(readMarketplace).mockResolvedValue({ ...detail, products: [{ ...detail.products[0], variants: [{
      ...detail.products[0].variants![0], id: '14:193', id_kind: 'properties', listed_price: { amount: '20.07', currency: null }, daily_discount_price: null,
    }] }] })
    fireEvent.click(screen.getByRole('button', { name: 'Read SKU details for 1005004616872521' }))
    const region = await screen.findByRole('region', { name: 'SKU details' })
    expect(within(region).getByText('Property combination: 14:193')).toBeInTheDocument()
    expect(within(region).getByText('Currency unknown 20.07')).toBeInTheDocument()
    expect(within(region).queryByText('USD 20.07')).not.toBeInTheDocument()
  })

  it('limits SKU details to supported POP products', async () => {
    render(<Harness />)
    await readShop()
    vi.mocked(readMarketplace).mockResolvedValue({ ...products, products: [{ ...products.products[0], product_type: 'POP_CHOICE', details_available: false }] })
    fireEvent.click(screen.getByRole('button', { name: 'Read products' }))
    await screen.findByText('POP_CHOICE')
    expect(screen.getByText('SKU details currently support standard POP products.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Read SKU details for 1005004616872521' })).not.toBeInTheDocument()
  })

  it('passes the exact SKU cursor and restarts explicitly after a changed-product error', async () => {
    render(<Harness />)
    await readProducts()
    const cursor = '10.' + 'a'.repeat(64)
    vi.mocked(readMarketplace).mockResolvedValue({ ...detail, next_page_token: cursor, total_count: 20 })
    fireEvent.click(screen.getByRole('button', { name: 'Read SKU details for 1005004616872521' }))
    await screen.findByRole('button', { name: 'Next SKU page' })
    vi.mocked(readMarketplace).mockRejectedValue(new Error('The product or SKUs changed. Read SKU details from the first page.'))
    fireEvent.click(screen.getByRole('button', { name: 'Next SKU page' }))
    await screen.findByRole('alert')
    expect(readMarketplace).toHaveBeenLastCalledWith('aliexpress', { operation: 'product_details', shop_id: sellerId, product_id: '1005004616872521', page_size: 10, page_token: cursor })
    expect(screen.queryByRole('region', { name: 'SKU details' })).not.toBeInTheDocument()
    vi.mocked(readMarketplace).mockResolvedValue(detail)
    fireEvent.click(screen.getByRole('button', { name: 'Read SKU details for 1005004616872521' }))
    await screen.findByRole('region', { name: 'SKU details' })
    expect(readMarketplace).toHaveBeenLastCalledWith('aliexpress', { operation: 'product_details', shop_id: sellerId, product_id: '1005004616872521', page_size: 10 })
  })

  it('locks concurrent operations and discards responses after unmount', async () => {
    let finish!: (value: MarketplaceRead) => void
    vi.mocked(readMarketplace).mockReturnValue(new Promise((resolve) => { finish = resolve }))
    const { unmount } = render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Read seller account' }))
    expect(screen.getByRole('button', { name: 'Revoke' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Read seller account' }))
    expect(readMarketplace).toHaveBeenCalledTimes(1)
    unmount()
    await act(async () => finish(shop))
    expect(screen.queryByText(`Seller ID: ${sellerId}`)).not.toBeInTheDocument()
  })

  it('clears records and secrets on revoke and leaves reconnect available', async () => {
    render(<Harness />)
    await readProducts()
    fillCredentials()
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
    await screen.findByText('Revoked')
    expect(screen.queryByText('Kettle')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Seller access token')).toHaveValue('')
    expect(screen.getByText('Reconnect seller access')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Read seller account' })).toBeDisabled()
  })

  it('grants the exact seller account to the selected project', async () => {
    render(<Harness />)
    await readShop()
    fireEvent.click(screen.getByText('Project access for Agents'))
    await screen.findByRole('option', { name: 'Launch' })
    fireEvent.change(screen.getByLabelText('Project for shop access'), { target: { value: 'project-1' } })
    const button = screen.getByRole('button', { name: 'Allow Agent reads for selected shop' })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => expect(grantMarketplaceAccess).toHaveBeenCalledWith('project-1', {
      connector_id: 'aliexpress', shop_id: sellerId, expected_revision: 0, expires_in_seconds: 7 * 86400,
    }))
  })

  it('provides Chinese identity and status labels', async () => {
    render(<I18nContext.Provider value="zh-CN"><Harness /></I18nContext.Provider>)
    expect(screen.getByText('允许读取速卖通商品')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '读取卖家账号' }))
    await screen.findByText(`卖家 ID: ${sellerId}`)
    expect(screen.getByRole('option', { name: '审核不通过' })).toBeInTheDocument()
    expect(screen.getByText(/项目授权绑定此卖家账号/)).toBeInTheDocument()
  })
})
