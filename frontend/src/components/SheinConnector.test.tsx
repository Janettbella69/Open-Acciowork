import { useState } from 'react'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Connector, OpportunityProject } from '../api/types'
import type { MarketplaceRead } from '../api/marketplaces'
import { I18nContext } from '../i18n'
import SheinConnector from './SheinConnector'

vi.mock('../api/client', () => ({
  readMarketplace: vi.fn(), upsertConnector: vi.fn(), revokeConnector: vi.fn(), listConnectors: vi.fn(),
  listProjects: vi.fn(), getMarketplaceAccess: vi.fn(), grantMarketplaceAccess: vi.fn(), revokeMarketplaceAccess: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))
import { getMarketplaceAccess, grantMarketplaceAccess, listConnectors, listProjects, readMarketplace, revokeConnector, upsertConnector } from '../api/client'

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
  })
})
