import { useState } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Connector, OpportunityProject } from '../api/types'
import { SHEIN_INVENTORY_LABELS, type MarketplaceRead, type SheinInventoryType } from '../api/marketplaces'
import { I18nContext } from '../i18n'
import SheinConnector from './SheinConnector'

vi.mock('../api/client', () => ({
  beginSheinAuthorization: vi.fn(),
  readMarketplace: vi.fn(), upsertConnector: vi.fn(), revokeConnector: vi.fn(), listConnectors: vi.fn(),
  listProjects: vi.fn(), getMarketplaceAccess: vi.fn(), grantMarketplaceAccess: vi.fn(), revokeMarketplaceAccess: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))
import { beginSheinAuthorization, getMarketplaceAccess, grantMarketplaceAccess, listConnectors, listProjects, readMarketplace, revokeConnector, upsertConnector } from '../api/client'

const connector: Connector = {
  id: 'shein', type: 'shein', display_name: 'SHEIN', scope: 'shein:store:read shein:products:read',
  secret_env: 'OPHIRA_CONNECTOR_SHEIN_KEY', endpoint: 'https://openapi.sheincorp.com',
  enabled: true, health: 'unknown', last_checked_at: null, created_at: '2026-09-22T00:00:00Z',
}
const shop: MarketplaceRead = {
  connector_id: 'shein', operation: 'authorized_shops', shop_id: null,
  shops: [{ id: '21871704', name: 'Synthetic shop', region: '', seller_type: 'POP-GLOBAL' }],
  products: [], next_page_token: null,
  source_url: 'https://openapi.sheincorp.com/open-api/openapi-business-backend/query-store-info',
  retrieved_at: '2026-09-22T10:00:00Z', request_id: 'synthetic-request', data_scope: 'authorized_seller_data',
}
const products: MarketplaceRead = {
  ...shop, operation: 'products', shop_id: '21871704', shops: [],
  source_url: 'https://openapi.sheincorp.com/open-api/goods/searchProduct',
  products: [{ id: 'spu_1', title: '', status: 'ON_SHELF', category_id: '2000', variants: [
    { id: 'skc_red', title: 'Red kettle', status: 'ON_SHELF' },
    { id: 'skc_blue', title: 'Blue kettle', status: 'OFF_SHELF' },
  ] }],
  pagination_unit: 'SPU', page_number: 1, total_count: 21, next_page_token: '2',
}
const skuDetails: MarketplaceRead = {
  ...products, operation: 'product_details', pagination_unit: 'SKU', total_count: 2,
  inventory_type: null, next_page_token: `1.${'a'.repeat(64)}`,
  source_url: 'https://openapi.sheincorp.com/open-api/goods/spu-info', request_id: 'sku-request',
  source_requests: [
    { source_url: shop.source_url, request_id: 'store-request' },
    { source_url: products.source_url, request_id: 'catalog-request' },
    { source_url: 'https://openapi.sheincorp.com/open-api/goods/spu-info', request_id: 'sku-request' },
  ],
  products: [{ ...products.products[0], title: 'Kettle collection', variants_offset: 0, variants_complete: false, variants: [{
    id: 'sku_1', title: '', sku: 'seller-large', skc_id: 'skc_red', skc_title: 'Red kettle',
    status: 'SELLING', purchase_status: 'STOPPED',
    attributes: [{ property_id: '87', value_id: '9007199254740993', value: 'Large' }],
    site_prices: [
      { site: 'shein-us', currency: 'USD', base_price: '12.35', special_price: '0' },
      { site: null, currency: null, base_price: null, special_price: null },
    ],
    supply_prices: [{ currency: 'CNY', amount: '4.25' }], inventory: null,
  }] }],
}
function withInventory(kind: SheinInventoryType): MarketplaceRead {
  return {
    ...skuDetails, inventory_type: kind, partial_data: true,
    source_url: 'https://openapi.sheincorp.com/open-api/stock/stock-query', request_id: 'stock-request',
    source_requests: [...skuDetails.source_requests!, { source_url: 'https://openapi.sheincorp.com/open-api/stock/stock-query', request_id: 'stock-request' }],
    products: [{ ...skuDetails.products[0], variants: [{ ...skuDetails.products[0].variants![0], inventory: {
      totals: { total: 100, locked: 7, temporary_locked: 3, usable: 90, in_transit: null, stockout_demand: 0 },
      warehouses: [{ code: 'warehouse-1', warehouse_type: '2', total: 100, locked: 7, temporary_locked: 3, usable: 90, in_transit: null, stockout_demand: 0 }],
    } }] }],
  }
}

function Harness({ initial = connector, onChange = vi.fn() }: { initial?: Connector; onChange?: (value: Connector) => void }) {
  const [value, setValue] = useState(initial)
  return <SheinConnector connector={value} onChange={(updated) => { setValue(updated); onChange(updated) }} />
}
function fillCredentials() {
  fireEvent.click(screen.getByText('Configure seller access'))
  fireEvent.change(screen.getByLabelText('Seller openKeyId'), { target: { value: 'synthetic-openkey' } })
  fireEvent.change(screen.getByLabelText('Decrypted seller secretKey'), { target: { value: 'synthetic-secret' } })
}
async function readShop() {
  fireEvent.click(screen.getByRole('button', { name: 'Read store' }))
  await screen.findByText('Synthetic shop')
}
async function readProducts() {
  await readShop()
  vi.mocked(readMarketplace).mockResolvedValue(products)
  fireEvent.click(screen.getByRole('button', { name: 'Read products' }))
  await screen.findByText('Red kettle')
}
async function readSkuDetails(value = skuDetails) {
  vi.mocked(readMarketplace).mockResolvedValue(value)
  fireEvent.click(screen.getByRole('button', { name: 'Read SKU details for spu_1' }))
  return screen.findByRole('region', { name: 'SHEIN SKU details' })
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

describe('SheinConnector', () => {
  it('starts consent with app identity and chosen scope without collecting seller keys', async () => {
    const url = 'https://openapi-sem.sheincorp.com/#/empower?appid=synthetic-app'
    vi.mocked(beginSheinAuthorization).mockResolvedValue({ authorization_url: url, expires_at: 9999999999 })
    render(<Harness />)
    fireEvent.click(screen.getByText('Authorize through SHEIN'))
    fireEvent.change(screen.getByLabelText('SHEIN app ID'), { target: { value: 'synthetic-app' } })
    fireEvent.change(screen.getByLabelText('Authorization API region'), { target: { value: 'https://openapi.sheincorp.cn' } })
    fireEvent.click(screen.getByLabelText('Read products after authorization'))
    fireEvent.click(screen.getByRole('button', { name: 'Generate SHEIN authorization link' }))
    expect(await screen.findByRole('link', { name: 'Continue to SHEIN authorization' })).toHaveAttribute('href', url)
    expect(beginSheinAuthorization).toHaveBeenCalledWith('shein', {
      app_id: 'synthetic-app', endpoint: 'https://openapi.sheincorp.cn', allow_products: false, redirect_origin: window.location.origin,
    })
    expect(upsertConnector).not.toHaveBeenCalled()
    expect(readMarketplace).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('SHEIN app ID'), { target: { value: 'changed-app' } })
    expect(screen.queryByRole('link', { name: 'Continue to SHEIN authorization' })).not.toBeInTheDocument()
  })

  it('does not render an authorization URL to a different host', async () => {
    vi.mocked(beginSheinAuthorization).mockResolvedValue({ authorization_url: 'https://evil.test/#/empower', expires_at: 9999999999 })
    render(<Harness />)
    fireEvent.change(screen.getByLabelText('SHEIN app ID'), { target: { value: 'synthetic-app' } })
    fireEvent.click(screen.getByRole('button', { name: 'Generate SHEIN authorization link' }))
    await screen.findByRole('alert')
    expect(screen.queryByRole('link', { name: 'Continue to SHEIN authorization' })).not.toBeInTheDocument()
  })

  it('saves masked seller credentials to the selected official origin only after submission', async () => {
    const onChange = vi.fn()
    render(<Harness onChange={onChange} />)
    fillCredentials()
    expect(screen.getByLabelText('Seller openKeyId')).toHaveAttribute('type', 'password')
    expect(screen.getByLabelText('Decrypted seller secretKey')).toHaveAttribute('type', 'password')
    expect(screen.getByText(/encrypted secretKey returned/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('SHEIN API origin'), { target: { value: 'https://openapi.sheincorp.cn' } })
    fireEvent.click(screen.getByLabelText('Allow SHEIN product reads'))
    expect(upsertConnector).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Save seller credentials' }))
    await screen.findByText('Credentials saved. Read the store to verify access.')
    expect(upsertConnector).toHaveBeenCalledWith('shein', {
      type: 'shein', display_name: 'SHEIN', endpoint: 'https://openapi.sheincorp.cn',
      secret_env: 'OPHIRA_CONNECTOR_SHEIN_KEY', scope: 'shein:store:read',
      secret: JSON.stringify({ open_key_id: 'synthetic-openkey', secret_key: 'synthetic-secret' }),
    })
    expect(screen.getByLabelText('Seller openKeyId')).toHaveValue('')
    expect(screen.getByLabelText('Decrypted seller secretKey')).toHaveValue('')
    expect(readMarketplace).not.toHaveBeenCalled()
    expect(onChange).toHaveBeenCalledTimes(1)
  })

  it('keeps SPU pagination and SKC variants distinct and follows the returned page token', async () => {
    render(<Harness />)
    await readProducts()
    expect(readMarketplace).toHaveBeenLastCalledWith('shein', {
      operation: 'products', shop_id: '21871704', page_size: 10, product_status: 'ALL',
    })
    const table = screen.getByRole('table', { name: 'SHEIN product groups · current page' })
    expect(within(table).getAllByRole('row')).toHaveLength(2)
    expect(within(table).getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByText('Page 1 · 21 SPU groups reported in total')).toBeInTheDocument()
    expect(screen.getByText(/synthetic-request/)).toHaveTextContent(products.source_url)
    vi.mocked(readMarketplace).mockResolvedValue({ ...products, page_number: 2, products: [], next_page_token: null })
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    await screen.findByText('No products on this page.')
    expect(readMarketplace).toHaveBeenLastCalledWith('shein', {
      operation: 'products', shop_id: '21871704', page_size: 10, product_status: 'ALL', page_token: '2',
    })
    expect(screen.queryByText('Red kettle')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Next page' })).not.toBeInTheDocument()
  })

  it('resets pagination on a shelf filter change and clears stale pages after failed reads', async () => {
    render(<Harness />)
    await readProducts()
    fireEvent.change(screen.getByLabelText('SKC shelf status'), { target: { value: 'OFF_SHELF' } })
    expect(screen.queryByText('Red kettle')).not.toBeInTheDocument()
    expect(readMarketplace).toHaveBeenCalledTimes(2)
    vi.mocked(readMarketplace).mockRejectedValue(new Error('Seller authorization expired'))
    fireEvent.click(screen.getByRole('button', { name: 'Read products' }))
    await screen.findByRole('alert')
    expect(readMarketplace).toHaveBeenLastCalledWith('shein', {
      operation: 'products', shop_id: '21871704', page_size: 10, product_status: 'OFF_SHELF',
    })
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('reads SKU quotes without silently selecting inventory or interpreting zero as a sale price', async () => {
    render(<Harness />)
    await readProducts()
    expect(screen.getByLabelText('Inventory to read with SKU details')).toHaveValue('')
    const region = await readSkuDetails()
    expect(readMarketplace).toHaveBeenLastCalledWith('shein', {
      operation: 'product_details', shop_id: '21871704', product_id: 'spu_1', page_size: 10,
    })
    expect(within(region).getByText('SKU rows 1–1 of 2')).toBeInTheDocument()
    expect(within(region).getByText(/Base price: 12.35/)).toHaveTextContent('Special price: 0')
    expect(within(region).getByText(/Base price: Unknown/)).toHaveTextContent('Special price: Unknown')
    expect(within(region).getByText('CNY 4.25')).toBeInTheDocument()
    expect(within(region).getByText('87: Large (9007199254740993)')).toBeInTheDocument()
    expect(within(region).getByText(/Sales status: Selling/)).toHaveTextContent('Purchase status: Stopped')
    expect(within(region).getByText('Not requested')).toBeInTheDocument()
    expect(within(region).getByText(/zero special price does not establish/)).toBeInTheDocument()
    fireEvent.click(within(region).getByText('Source requests'))
    for (const source of skuDetails.source_requests!) expect(within(region).getByText(new RegExp(source.request_id))).toHaveTextContent(source.source_url)
  })

  it.each(['PI', 'VI', 'JI'] as const)('reads explicitly selected %s stock with separate totals and warehouse quantities', async (kind) => {
    render(<Harness />)
    await readProducts()
    fireEvent.change(screen.getByLabelText('Inventory to read with SKU details'), { target: { value: kind } })
    const region = await readSkuDetails(withInventory(kind))
    expect(readMarketplace).toHaveBeenLastCalledWith('shein', {
      operation: 'product_details', shop_id: '21871704', product_id: 'spu_1', page_size: 10, inventory_type: kind,
    })
    expect(within(region).getByText(SHEIN_INVENTORY_LABELS[kind])).toBeInTheDocument()
    expect(within(region).getByRole('status')).toHaveTextContent('Missing values remain unknown.')
    expect(within(region).getByText('Total stock: 100')).toBeInTheDocument()
    expect(within(region).getByText('In-transit stock: Unknown')).toBeInTheDocument()
    expect(within(region).getByText('Stockout demand: 0')).toBeInTheDocument()
    fireEvent.click(within(region).getByText('Warehouse inventory (1)'))
    const warehouse = within(region).getByRole('table', { name: 'Reported warehouse inventory' })
    expect(within(warehouse).getAllByRole('row')).toHaveLength(2)
    expect(within(warehouse).getAllByRole('cell').map((cell) => cell.textContent)).toEqual(['2', '100', '7', '3', '90', 'Unknown', '0'])
    fireEvent.click(within(region).getByText('Source requests'))
    expect(within(region).getByText(/stock-request/)).toHaveTextContent('stock-query')
  })

  it('clears SKU observations when inventory selection changes and waits for an explicit new read', async () => {
    render(<Harness />)
    await readProducts()
    await readSkuDetails()
    fireEvent.change(screen.getByLabelText('Inventory to read with SKU details'), { target: { value: 'VI' } })
    expect(screen.queryByRole('region', { name: 'SHEIN SKU details' })).not.toBeInTheDocument()
    expect(readMarketplace).toHaveBeenCalledTimes(3)
    await readSkuDetails(withInventory('VI'))
    fireEvent.change(screen.getByLabelText('Inventory to read with SKU details'), { target: { value: '' } })
    expect(screen.queryByRole('region', { name: 'SHEIN SKU details' })).not.toBeInTheDocument()
    expect(readMarketplace).toHaveBeenCalledTimes(4)
  })

  it('keeps SKU pagination separate from SPU pagination and preserves the selected inventory', async () => {
    render(<Harness />)
    await readProducts()
    fireEvent.change(screen.getByLabelText('Inventory to read with SKU details'), { target: { value: 'JI' } })
    const first = withInventory('JI')
    await readSkuDetails(first)
    vi.mocked(readMarketplace).mockResolvedValue({ ...first, next_page_token: null, products: [{
      ...first.products[0], variants_offset: 1, variants: [{ ...first.products[0].variants![0], id: 'sku_2', inventory: null }],
    }] })
    fireEvent.click(screen.getByRole('button', { name: 'Next SKU page' }))
    await screen.findByText('SKU rows 2–2 of 2')
    expect(readMarketplace).toHaveBeenLastCalledWith('shein', {
      operation: 'product_details', shop_id: '21871704', product_id: 'spu_1', page_size: 10,
      inventory_type: 'JI', page_token: first.next_page_token,
    })
    const region = screen.getByRole('region', { name: 'SHEIN SKU details' })
    expect(within(region).getByText('sku_2')).toBeInTheDocument()
    expect(within(region).queryByText('sku_1')).not.toBeInTheDocument()
    expect(within(region).getByText('Inventory not supplied')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Next SKU page' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next page' })).toBeEnabled()
  })

  it('clears a changed snapshot and starts a fresh SKU read without the old cursor', async () => {
    render(<Harness />)
    await readProducts()
    await readSkuDetails()
    vi.mocked(readMarketplace).mockRejectedValue(new Error('The SHEIN product, prices or inventory changed. Read SKU details from the first page.'))
    fireEvent.click(screen.getByRole('button', { name: 'Next SKU page' }))
    await screen.findByRole('alert')
    expect(screen.queryByRole('region', { name: 'SHEIN SKU details' })).not.toBeInTheDocument()
    await readSkuDetails()
    expect(readMarketplace).toHaveBeenLastCalledWith('shein', {
      operation: 'product_details', shop_id: '21871704', product_id: 'spu_1', page_size: 10,
    })
  })

  it.each(['shop', 'product', 'operation', 'inventory'] as const)('withholds a SKU response with mismatched %s', async (field) => {
    render(<Harness />)
    await readProducts()
    const mismatched: MarketplaceRead = { ...skuDetails }
    if (field === 'shop') mismatched.shop_id = 'other-shop'
    if (field === 'product') mismatched.products = [{ ...skuDetails.products[0], id: 'other-product' }]
    if (field === 'operation') mismatched.operation = 'products'
    if (field === 'inventory') mismatched.inventory_type = 'VI'
    vi.mocked(readMarketplace).mockResolvedValue(mismatched)
    fireEvent.click(screen.getByRole('button', { name: 'Read SKU details for spu_1' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The SKU response does not match this request.')
    expect(screen.queryByRole('region', { name: 'SHEIN SKU details' })).not.toBeInTheDocument()
  })

  it('serializes SKU reads and locks the filters and mutations until the read completes', async () => {
    render(<Harness />)
    await readProducts()
    let complete!: (value: MarketplaceRead) => void
    vi.mocked(readMarketplace).mockImplementation(() => new Promise((resolve) => { complete = resolve }))
    const button = screen.getByRole('button', { name: 'Read SKU details for spu_1' })
    fireEvent.click(button); fireEvent.click(button)
    expect(readMarketplace).toHaveBeenCalledTimes(3)
    expect(button).toBeDisabled()
    expect(screen.getByLabelText('Inventory to read with SKU details')).toBeDisabled()
    expect(screen.getByLabelText('SKC shelf status')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Revoke' })).toBeDisabled()
    await act(async () => complete(skuDetails))
    expect(await screen.findByRole('region', { name: 'SHEIN SKU details' })).toBeInTheDocument()
    expect(button).toBeEnabled()
  })

  it.each(['shelf filter', 'SPU page', 'revoke'])('clears SKU observations after changing %s', async (change) => {
    render(<Harness />)
    await readProducts()
    await readSkuDetails()
    if (change === 'shelf filter') fireEvent.change(screen.getByLabelText('SKC shelf status'), { target: { value: 'OFF_SHELF' } })
    if (change === 'SPU page') {
      vi.mocked(readMarketplace).mockResolvedValue({ ...products, products: [], next_page_token: null })
      fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
      await screen.findByText('No products on this page.')
    }
    if (change === 'revoke') {
      fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
      await screen.findByText('Reconnect seller access')
    }
    expect(screen.queryByRole('region', { name: 'SHEIN SKU details' })).not.toBeInTheDocument()
    expect(screen.queryByText(/sku-request/)).not.toBeInTheDocument()
  })

  it('uses saved permissions, so a draft checkbox cannot enable product reads or grants', async () => {
    render(<Harness initial={{ ...connector, scope: 'shein:store:read' }} />)
    await readShop()
    expect(screen.getByRole('button', { name: 'Read products' })).toBeDisabled()
    fillCredentials()
    fireEvent.click(screen.getByLabelText('Allow SHEIN product reads'))
    expect(screen.getByRole('button', { name: 'Read products' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Allow Agent reads for selected shop', hidden: true })).toBeDisabled()
  })

  it('revokes access, clears observations and offers explicit reconnect', async () => {
    render(<Harness />)
    await readProducts()
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
    await screen.findByText('Reconnect seller access')
    expect(screen.getByRole('button', { name: 'Read store' })).toBeDisabled()
    expect(screen.queryByText('Synthetic shop')).not.toBeInTheDocument()
    expect(screen.queryByText('Red kettle')).not.toBeInTheDocument()
    expect(screen.queryByText(/synthetic-request/)).not.toBeInTheDocument()
  })

  it.each(['save', 'revoke'])('requires a fresh status after an ambiguous %s and never resubmits it', async (operation) => {
    render(<Harness />)
    await readShop()
    if (operation === 'save') {
      fillCredentials()
      vi.mocked(upsertConnector).mockRejectedValue(new Error('Connection lost'))
    } else {
      vi.mocked(revokeConnector).mockRejectedValue(new Error('Connection lost'))
    }
    fireEvent.click(screen.getByRole('button', { name: operation === 'save' ? 'Save seller credentials' : 'Revoke' }))
    await screen.findByText('The change may have reached the server. Refresh its status before continuing.')
    expect(screen.getByRole('button', { name: 'Read store' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Revoke' })).toBeDisabled()
    expect(screen.getByLabelText('Decrypted seller secretKey')).toHaveValue('')
    expect(screen.queryByText('Synthetic shop')).not.toBeInTheDocument()
    vi.mocked(listConnectors).mockRejectedValueOnce(new Error('Still offline'))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh connector status' }))
    await screen.findByText('Still offline')
    expect(screen.getByRole('button', { name: 'Read store' })).toBeDisabled()
    vi.mocked(listConnectors).mockResolvedValue([{ ...connector, enabled: false }])
    fireEvent.click(screen.getByRole('button', { name: 'Refresh connector status' }))
    await screen.findByText('Reconnect seller access')
    expect(screen.getByRole('button', { name: 'Read store' })).toBeDisabled()
    expect(operation === 'save' ? upsertConnector : revokeConnector).toHaveBeenCalledTimes(1)
  })

  it('keeps actions locked if the connector disappeared during refresh', async () => {
    render(<Harness />)
    vi.mocked(revokeConnector).mockRejectedValue(new Error('Connection lost'))
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))
    await screen.findByRole('button', { name: 'Refresh connector status' })
    vi.mocked(listConnectors).mockResolvedValue([{ ...connector, type: 'shopify' }])
    fireEvent.click(screen.getByRole('button', { name: 'Refresh connector status' }))
    await screen.findByText('This connector is no longer available. Reload Connectors.')
    expect(screen.getByRole('button', { name: 'Read store' })).toBeDisabled()
  })

  it('serializes duplicate clicks and does not publish late mutation results after unmount', async () => {
    let complete!: (value: Connector) => void
    vi.mocked(upsertConnector).mockImplementation(() => new Promise((resolve) => { complete = resolve }))
    const onChange = vi.fn()
    const view = render(<Harness onChange={onChange} />)
    fillCredentials()
    const button = screen.getByRole('button', { name: 'Save seller credentials' })
    fireEvent.click(button); fireEvent.click(button)
    expect(upsertConnector).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Read store' })).toBeDisabled()
    view.unmount()
    await act(async () => complete(connector))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('grants the selected shop to a project explicitly and shares the busy state', async () => {
    render(<Harness />)
    await readShop()
    fireEvent.click(screen.getByText('Project access for Agents'))
    await screen.findByRole('option', { name: 'Launch' })
    fireEvent.change(screen.getByLabelText('Project for shop access'), { target: { value: 'project-1' } })
    await screen.findByText('No shop grants for this connector in the selected project.')
    expect(grantMarketplaceAccess).not.toHaveBeenCalled()
    let complete!: (value: Awaited<ReturnType<typeof grantMarketplaceAccess>>) => void
    vi.mocked(grantMarketplaceAccess).mockImplementation(() => new Promise((resolve) => { complete = resolve }))
    const button = screen.getByRole('button', { name: 'Allow Agent reads for selected shop' })
    fireEvent.click(button); fireEvent.click(button)
    expect(grantMarketplaceAccess).toHaveBeenCalledTimes(1)
    expect(grantMarketplaceAccess).toHaveBeenCalledWith('project-1', {
      connector_id: 'shein', shop_id: '21871704', expected_revision: 0, expires_in_seconds: 604800,
    })
    expect(screen.getByRole('button', { name: 'Read products' })).toBeDisabled()
    complete({ project_id: 'project-1', revision: 1, bindings: [] })
    await screen.findByText('Agent product reads allowed for Synthetic shop in this project.')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Read products' })).not.toBeDisabled())
  })

  it('uses the supplier ID when SHEIN omits a store name and does not invent its region', async () => {
    vi.mocked(readMarketplace).mockResolvedValue({ ...shop, shops: [{ ...shop.shops[0], name: '', seller_type: '' }] })
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Read store' }))
    await screen.findByText('21871704')
    expect(screen.getByText('Selected shop: 21871704')).toBeInTheDocument()
    expect(screen.queryByText('US')).not.toBeInTheDocument()
  })

  it('translates configuration and product semantics into Chinese', async () => {
    render(<I18nContext.Provider value="zh-CN"><Harness /></I18nContext.Provider>)
    expect(screen.getByLabelText('已解密的商家 secretKey')).toHaveAttribute('type', 'password')
    expect(screen.getByText(/授权交换返回的加密 secretKey 不能直接使用/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '读取店铺' }))
    await screen.findByText('Synthetic shop')
    vi.mocked(readMarketplace).mockResolvedValue(products)
    fireEvent.click(screen.getByRole('button', { name: '读取商品' }))
    await screen.findByText('第 1 页 · 平台报告共 21 个 SPU 分组')
    expect(screen.getByRole('table', { name: 'SHEIN 商品分组 · 当前页' })).toBeInTheDocument()
    expect(screen.getByLabelText('SKC 上架状态')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('读取 SKU 详情时查询的库存'), { target: { value: 'PI' } })
    vi.mocked(readMarketplace).mockResolvedValue(withInventory('PI'))
    fireEvent.click(screen.getByRole('button', { name: '读取商品 spu_1 的 SKU 详情' }))
    const region = await screen.findByRole('region', { name: 'SHEIN SKU 详情' })
    expect(within(region).getByRole('table', { name: 'SHEIN SKU 报价与库存 · 当前页' })).toBeInTheDocument()
    expect(within(region).getByText('SHEIN 仓实物库存（PI）')).toBeInTheDocument()
    expect(within(region).getByText('在途库存: 未知')).toBeInTheDocument()
    expect(within(region).getByRole('status')).toHaveTextContent('缺失值保留为未知。')
    expect(within(region).getByText(/销售状态: 在售/)).toHaveTextContent('采购状态: 已停止')
  })
})
