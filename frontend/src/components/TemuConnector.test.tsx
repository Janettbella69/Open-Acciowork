import { useState } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Connector, OpportunityProject } from '../api/types'
import type { MarketplaceRead } from '../api/marketplaces'
import { I18nContext } from '../i18n'
import TemuConnector from './TemuConnector'

vi.mock('../api/client', () => ({
  readMarketplace: vi.fn(), upsertConnector: vi.fn(), revokeConnector: vi.fn(), listConnectors: vi.fn(),
  listProjects: vi.fn(), getMarketplaceAccess: vi.fn(), grantMarketplaceAccess: vi.fn(), revokeMarketplaceAccess: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))
import { getMarketplaceAccess, grantMarketplaceAccess, listConnectors, listProjects, readMarketplace, revokeConnector, upsertConnector } from '../api/client'

const shopId = '123456789012345678'
const connector: Connector = {
  id: 'temu', type: 'temu', display_name: 'Temu', scope: 'temu:store:read temu:products:read',
  secret_env: 'OPHIRA_CONNECTOR_TEMU_KEY', endpoint: 'https://openapi-b-global.temu.com',
  enabled: true, health: 'unknown', last_checked_at: null, created_at: '2026-09-22T00:00:00Z',
}
const shop: MarketplaceRead = {
  connector_id: 'temu', operation: 'authorized_shops', shop_id: null,
  shops: [{ id: shopId, name: '', region: '', region_id: '211', seller_type: 'LOCAL', product_reads_available: true, sku_reads_available: true }],
  products: [], next_page_token: null,
  source_url: 'https://openapi-b-global.temu.com/openapi/router',
  retrieved_at: '2026-09-22T10:00:00Z', request_id: 'synthetic-request', data_scope: 'authorized_seller_data',
  authorization_expires_at: '2030-01-01T10:00:00Z', api_name: 'bg.open.accesstoken.info.get',
}
const products: MarketplaceRead = {
  ...shop, operation: 'products', shop_id: shopId, shops: [], total_count: 12, next_page_token: 'page/+=2',
  products: [{ id: 'product_alpha', title: 'Kettle', status: 'Active', sku: 'KETTLE', details_available: true }],
}
const detail: MarketplaceRead = {
  ...products, operation: 'product_details', next_page_token: null, total_count: 1,
  supplier_price_access: 'allowed', api_name: 'bg.local.goods.sku.list.price.query',
  products: [{ ...products.products[0], variants_loaded: true, variants: [
    { id: 'sku_beta', title: 'Red', sku: 'RED', status: 'Active',
      supplier_price: { currency: 'USD', amount: '12.35000001' }, reported_stock: null },
  ] }],
}

function Harness({ initial = connector, onChange = vi.fn() }: { initial?: Connector; onChange?: (value: Connector) => void }) {
  const [value, setValue] = useState(initial)
  return <TemuConnector connector={value} onChange={(updated) => { setValue(updated); onChange(updated) }} />
}
function fillCredentials() {
  fireEvent.click(screen.getByText('Configure seller access'))
  for (const [label, value] of [['App key', 'synthetic-key'], ['App secret', 'synthetic-secret'], ['Seller access token', 'synthetic-token']]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
}
async function readShop() {
  fireEvent.click(screen.getByRole('button', { name: 'Read store' }))
  await screen.findByText(`Temu · ${shopId}`)
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

describe('TemuConnector', () => {
  it('saves masked regional credentials and explicitly opted-in supply prices', async () => {
    render(<Harness />)
    fillCredentials()
    for (const label of ['App key', 'App secret', 'Seller access token']) expect(screen.getByLabelText(label)).toHaveAttribute('type', 'password')
    expect(upsertConnector).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Allow Temu supply price reads')).not.toBeChecked()
    fireEvent.change(screen.getByLabelText('Temu API region'), { target: { value: 'https://openapi-b-eu.temu.com' } })
    fireEvent.click(screen.getByLabelText('Allow Temu supply price reads'))
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    await screen.findByText('Credentials saved. Read the store to verify access.')
    const input = vi.mocked(upsertConnector).mock.calls[0][1]
    expect(input.endpoint).toBe('https://openapi-b-eu.temu.com')
    expect(input.scope).toBe('temu:store:read temu:products:read temu:supply-prices:read')
    expect(JSON.parse(input.secret!)).toEqual({ app_key: 'synthetic-key', app_secret: 'synthetic-secret', access_token: 'synthetic-token' })
    for (const label of ['App key', 'App secret', 'Seller access token']) expect(screen.getByLabelText(label)).toHaveValue('')
    expect(readMarketplace).not.toHaveBeenCalled()
  })

  it('omits supply price permission when product reads are disabled', async () => {
    render(<Harness />)
    fillCredentials()
    fireEvent.click(screen.getByLabelText('Allow Temu supply price reads'))
    fireEvent.click(screen.getByLabelText('Allow Temu product reads'))
    expect(screen.getByLabelText('Allow Temu supply price reads')).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    await screen.findByText('Credentials saved. Read the store to verify access.')
    expect(vi.mocked(upsertConnector).mock.calls[0][1].scope).toBe('temu:store:read')
  })

  it('clears secrets and requires status refresh after an uncertain save', async () => {
    vi.mocked(upsertConnector).mockRejectedValue(new Error('Connection interrupted'))
    render(<Harness />)
    fillCredentials()
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    await screen.findByRole('alert')
    expect(screen.getByLabelText('App secret')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Read store' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh connector status' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Read store' })).toBeEnabled())
    expect(readMarketplace).not.toHaveBeenCalled()
  })

  it('retains exact IDs and displays actual expiry without inventing a store name or country', async () => {
    render(<Harness />)
    await readShop()
    expect(screen.getByText('Temu region ID: 211')).toBeInTheDocument()
    expect(screen.getByText(/Authorization expires at/)).toBeInTheDocument()
    expect(screen.getByText(/Active and inactive excludes drafts/)).toBeInTheDocument()
    expect(screen.queryByText('All statuses')).not.toBeInTheDocument()
  })

  it.each(['SEMI', 'missing_scope'])('prevents catalog and project grants for %s authority', async (kind) => {
    vi.mocked(readMarketplace).mockResolvedValue({ ...shop, shops: [{ ...shop.shops[0], seller_type: kind === 'SEMI' ? 'SEMI' : 'LOCAL', product_reads_available: false }] })
    render(<Harness />)
    await readShop()
    expect(screen.getByRole('button', { name: 'Read products' })).toBeDisabled()
    fireEvent.click(screen.getByText('Project access for Agents'))
    await screen.findByRole('option', { name: 'Launch' })
    fireEvent.change(screen.getByLabelText('Project for shop access'), { target: { value: 'project-1' } })
    await waitFor(() => expect(getMarketplaceAccess).toHaveBeenCalled())
    expect(screen.getByRole('button', { name: 'Allow Agent reads for selected shop' })).toBeDisabled()
    expect(grantMarketplaceAccess).not.toHaveBeenCalled()
  })

  it('requires the separately granted SKU API for details', async () => {
    vi.mocked(readMarketplace).mockResolvedValue({ ...shop, shops: [{ ...shop.shops[0], sku_reads_available: false }] })
    render(<Harness />)
    await readProducts()
    expect(screen.getByText('Authorize the SKU API in Temu Seller Center to read variants.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Read SKU details for product_alpha' })).toBeDisabled()
  })

  it('keeps supply prices distinct and missing stock unknown', async () => {
    render(<Harness />)
    await readProducts()
    vi.mocked(readMarketplace).mockResolvedValue(detail)
    fireEvent.click(screen.getByRole('button', { name: 'Read SKU details for product_alpha' }))
    const region = await screen.findByRole('region', { name: 'SKU details' })
    const row = within(region).getByText('RED').closest('tr')!
    expect(row).toHaveTextContent('USD 12.35000001')
    expect(row).toHaveTextContent('Unknown')
    expect(within(region).getByRole('columnheader', { name: 'Supply price' })).toBeInTheDocument()
    expect(within(region).queryByText('Available stock')).not.toBeInTheDocument()
    expect(screen.getByText(/Supply prices are not retail prices. Reported stock/)).toBeInTheDocument()
  })

  it.each(['disabled', 'not_authorized', 'unsupported_identifier'] as const)('explains absent prices when access is %s', async (access) => {
    render(<Harness />)
    await readProducts()
    vi.mocked(readMarketplace).mockResolvedValue({ ...detail, partial_data: true, supplier_price_access: access,
      products: [{ ...detail.products[0], variants: [{ ...detail.products[0].variants![0], supplier_price: null, reported_stock: 0 }] }],
    })
    fireEvent.click(screen.getByRole('button', { name: 'Read SKU details for product_alpha' }))
    const region = await screen.findByRole('region', { name: 'SKU details' })
    expect(within(region).queryByText('USD 12.35000001')).not.toBeInTheDocument()
    const row = within(region).getByText('RED').closest('tr')!
    expect(within(row).getByText('Unknown')).toBeInTheDocument()
    expect(within(row).getByText('0')).toBeInTheDocument()
    expect(within(region).getByText(/Some requested data was not returned by Temu/)).toBeInTheDocument()
    expect(region).toHaveTextContent(access === 'disabled' ? 'Supply price reads are disabled' : access === 'not_authorized' ? 'Temu has not authorized' : 'cannot accept these product or SKU identifiers')
  })

  it('paginates opaque product and SKU cursors and resets on status change', async () => {
    render(<Harness />)
    await readProducts()
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    await waitFor(() => expect(readMarketplace).toHaveBeenLastCalledWith('temu', { operation: 'products', shop_id: shopId, page_size: 10, product_status: 'ALL', page_token: 'page/+=2' }))
    await screen.findByText('Kettle')
    vi.mocked(readMarketplace).mockResolvedValue({ ...detail, next_page_token: 'sku/+=next' })
    fireEvent.click(screen.getByRole('button', { name: 'Read SKU details for product_alpha' }))
    await screen.findByRole('button', { name: 'Next SKU page' })
    vi.mocked(readMarketplace).mockResolvedValue(detail)
    fireEvent.click(screen.getByRole('button', { name: 'Next SKU page' }))
    await waitFor(() => expect(readMarketplace).toHaveBeenLastCalledWith('temu', { operation: 'product_details', shop_id: shopId, product_id: 'product_alpha', page_size: 10, page_token: 'sku/+=next' }))
    await screen.findByRole('table', { name: 'SKU details · current page' })
    expect(screen.queryByRole('button', { name: 'Next SKU page' })).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Product status'), { target: { value: 'INCOMPLETE' } })
    expect(screen.queryByRole('region', { name: 'SKU details' })).not.toBeInTheDocument()
    vi.mocked(readMarketplace).mockResolvedValue(products)
    fireEvent.click(screen.getByRole('button', { name: 'Read products' }))
    await waitFor(() => expect(readMarketplace).toHaveBeenLastCalledWith('temu', { operation: 'products', shop_id: shopId, page_size: 10, product_status: 'INCOMPLETE' }))
  })

  it('locks concurrent operations and discards late responses after unmount', async () => {
    let finish!: (value: MarketplaceRead) => void
    vi.mocked(readMarketplace).mockReturnValue(new Promise((resolve) => { finish = resolve }))
    const { unmount } = render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Read store' }))
    expect(screen.getByRole('button', { name: 'Revoke' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Read store' }))
    expect(readMarketplace).toHaveBeenCalledTimes(1)
    unmount()
    await act(async () => finish(shop))
    expect(screen.queryByText(`Temu · ${shopId}`)).not.toBeInTheDocument()
  })

  it('clears data and credentials on revoke and keeps reconnect available', async () => {
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

  it('grants the exact authorized shop through project access', async () => {
    render(<Harness />)
    await readShop()
    fireEvent.click(screen.getByText('Project access for Agents'))
    await screen.findByRole('option', { name: 'Launch' })
    fireEvent.change(screen.getByLabelText('Project for shop access'), { target: { value: 'project-1' } })
    const button = screen.getByRole('button', { name: 'Allow Agent reads for selected shop' })
    await waitFor(() => expect(button).toBeEnabled())
    fireEvent.click(button)
    await waitFor(() => expect(grantMarketplaceAccess).toHaveBeenCalledWith('project-1', {
      connector_id: 'temu', shop_id: shopId, expected_revision: 0, expires_in_seconds: 7 * 86400,
    }))
  })

  it('provides Chinese configuration, scope and stock explanations', async () => {
    render(<I18nContext.Provider value="zh-CN"><Harness /></I18nContext.Provider>)
    expect(screen.getByText('允许读取 Temu 商品')).toBeInTheDocument()
    expect(screen.getByText('允许读取 Temu 供货价')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '读取店铺' }))
    await screen.findByText(`Temu · ${shopId}`)
    expect(screen.getByRole('option', { name: '在售和非在售' })).toBeInTheDocument()
    expect(screen.getByText(/“在售和非在售”不包含草稿/)).toBeInTheDocument()
  })
})
