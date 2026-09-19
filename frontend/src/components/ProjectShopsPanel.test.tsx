import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProjectShop, ShopData } from '../api/shops'
import type { Connector } from '../api/types'
import { I18nContext } from '../i18n'
import ProjectShopsPanel from './ProjectShopsPanel'

vi.mock('../api/client', () => ({
  apiBaseUrl: '', controlProjectShop: vi.fn(), getProjectShopData: vi.fn(), grantProjectShop: vi.fn(),
  listConnectors: vi.fn(), listProjectShops: vi.fn(), saveProjectShop: vi.fn(),
  errorMessage: (reason: unknown) => reason instanceof Error ? reason.message : 'Unknown error',
}))
import { controlProjectShop, getProjectShopData, grantProjectShop, listConnectors, listProjectShops, saveProjectShop } from '../api/client'

const identity = { name: 'US shop', platform: 'tiktok_shop' as const, seller_shop_id: 'seller-1', connector_id: null, entry_url: 'https://seller.example.com/', additional_domains: ['assets.example.com'] }
const shop: ProjectShop = { ...identity, id: 'shop-1', project_id: 'project-1', revision: 2, browser_state: 'open', session_id: 'session-1', agent_access: false, grant_expires_at: null, capture_count: 1 }
const shopifyIdentity = { ...identity, name: 'Shopify store', platform: 'shopify' as const, seller_shop_id: 'my-store', entry_url: 'https://admin.shopify.com/store/my-store', additional_domains: ['accounts.shopify.com'] }
const shopify: Connector = { id: 'shopify-store', type: 'shopify', display_name: 'Shopify store connector', scope: 'read_products', secret_env: null, endpoint: 'https://my-store.myshopify.com', enabled: true, health: 'unknown', last_checked_at: null, created_at: '2026-09-19T00:00:00Z' }
const tiktok: Connector = { ...shopify, id: 'tiktok-shop', type: 'tiktok_shop', display_name: 'TikTok seller connector' }

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(listConnectors).mockResolvedValue([])
  vi.mocked(listProjectShops).mockResolvedValue([shop])
  vi.mocked(grantProjectShop).mockResolvedValue({ ...shop, revision: 3, agent_access: true })
  vi.mocked(saveProjectShop).mockResolvedValue({ ...shop, browser_state: 'closed' })
})

async function open() {
  fireEvent.click(screen.getByRole('button', { name: 'Project shops' }))
  await screen.findByText('US shop')
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh shops' })).toBeEnabled())
}

describe('ProjectShopsPanel', () => {
  it.each([null, 'shopify-store'])('creates a Shopify identity with optional matching connector %s', async (connectorId) => {
    vi.mocked(listConnectors).mockResolvedValue([shopify, tiktok])
    vi.mocked(saveProjectShop).mockResolvedValue({ ...shop, ...shopifyIdentity, id: 'shopify-1', connector_id: connectorId, browser_state: 'closed', session_id: null })
    render(<ProjectShopsPanel projectId="project-1" />)
    await open()
    fireEvent.change(screen.getByLabelText('Shop display name'), { target: { value: shopifyIdentity.name } })
    fireEvent.change(screen.getByLabelText('Shop platform'), { target: { value: 'shopify' } })
    expect(screen.getByLabelText('Shop platform')).toHaveValue('shopify')
    const connector = screen.getByLabelText('Platform Connector')
    expect(within(connector).getByRole('option', { name: 'Shopify store connector' })).toBeInTheDocument()
    expect(within(connector).queryByRole('option', { name: 'TikTok seller connector' })).not.toBeInTheDocument()
    if (connectorId) fireEvent.change(connector, { target: { value: connectorId } })
    fireEvent.change(screen.getByLabelText('Seller shop identity'), { target: { value: shopifyIdentity.seller_shop_id } })
    fireEvent.change(screen.getByLabelText('Seller portal URL'), { target: { value: shopifyIdentity.entry_url } })
    fireEvent.change(screen.getByLabelText('Additional login and resource domains'), { target: { value: 'accounts.shopify.com' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save shop identity' }))
    await screen.findByText('Shop identity saved. Open its browser and sign in.')
    expect(saveProjectShop).toHaveBeenCalledWith('project-1', { ...shopifyIdentity, connector_id: connectorId }, undefined)
    expect(screen.getByText('Shopify · my-store')).toBeInTheDocument()
    expect(controlProjectShop).not.toHaveBeenCalled()
    expect(grantProjectShop).not.toHaveBeenCalled()
  })

  it('clears the linked connector when switching from Shopify to a different platform', async () => {
    vi.mocked(listConnectors).mockResolvedValue([shopify, tiktok])
    render(<ProjectShopsPanel projectId="project-1" />)
    await open()
    const platform = screen.getByLabelText('Shop platform')
    const connector = screen.getByLabelText('Platform Connector')
    fireEvent.change(platform, { target: { value: 'shopify' } })
    fireEvent.change(connector, { target: { value: 'shopify-store' } })
    expect(connector).toHaveValue('shopify-store')
    fireEvent.change(platform, { target: { value: 'tiktok_shop' } })
    expect(connector).toHaveValue('')
    expect(within(connector).queryByRole('option', { name: 'Shopify store connector' })).not.toBeInTheDocument()
    expect(within(connector).getByRole('option', { name: 'TikTok seller connector' })).toBeInTheDocument()
  })

  it.each(['en', 'zh-CN'] as const)('edits a persisted Shopify identity in %s without losing its platform or revision', async (language) => {
    const saved: ProjectShop = { ...shop, ...shopifyIdentity, connector_id: shopify.id, browser_state: 'closed', session_id: null }
    vi.mocked(listProjectShops).mockResolvedValue([saved])
    vi.mocked(listConnectors).mockResolvedValue([shopify, tiktok])
    vi.mocked(saveProjectShop).mockResolvedValue({ ...saved, name: 'Renamed Shopify store', revision: saved.revision + 1 })
    render(<I18nContext.Provider value={language}><ProjectShopsPanel projectId="project-1" /></I18nContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: language === 'en' ? 'Project shops' : '项目店铺' }))
    const card = (await screen.findByText('Shopify · my-store')).closest('article')!
    await waitFor(() => expect(screen.getByRole('button', { name: language === 'en' ? 'Refresh shops' : '刷新店铺' })).toBeEnabled())
    fireEvent.click(within(card).getByRole('button', { name: language === 'en' ? 'Edit shop identity' : '编辑店铺身份' }))
    expect(screen.getByLabelText(language === 'en' ? 'Shop platform' : '店铺平台')).toHaveValue('shopify')
    expect(screen.getByLabelText(language === 'en' ? 'Platform Connector' : '平台 Connector')).toHaveValue(shopify.id)
    fireEvent.change(screen.getByLabelText(language === 'en' ? 'Shop display name' : '店铺显示名称'), { target: { value: 'Renamed Shopify store' } })
    fireEvent.click(screen.getByRole('button', { name: language === 'en' ? 'Save shop identity' : '保存店铺身份' }))
    await screen.findByText('Renamed Shopify store')
    expect(saveProjectShop).toHaveBeenCalledWith('project-1', { ...shopifyIdentity, name: 'Renamed Shopify store', connector_id: shopify.id }, saved)
  })

  it('requires human identity confirmation for the current session, then prepares a task', async () => {
    const onTask = vi.fn()
    render(<ProjectShopsPanel projectId="project-1" onTask={onTask} />)
    expect(listProjectShops).not.toHaveBeenCalled()
    await open()
    const allow = screen.getByRole('button', { name: 'Confirm identity and allow Agent reads' })
    expect(allow).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Prepare shop task' })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.change(screen.getByLabelText('Shop access duration'), { target: { value: '1' } })
    fireEvent.click(allow)
    await screen.findByText('This browser session is available to project Agents until expiry.')
    expect(grantProjectShop).toHaveBeenCalledWith('project-1', shop, 1)
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    expect(onTask).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Prepare shop task' }))
    expect(onTask).toHaveBeenCalledWith(expect.stringContaining('shop_id: shop-1'))
  })

  it('edits only identity fields while retaining the server revision for compare-and-swap', async () => {
    const closed = { ...shop, browser_state: 'closed' as const }
    vi.mocked(listProjectShops).mockResolvedValue([closed])
    render(<ProjectShopsPanel projectId="project-1" />)
    await open()
    fireEvent.click(screen.getByRole('button', { name: 'Edit shop identity' }))
    fireEvent.change(screen.getByLabelText('Shop display name'), { target: { value: 'Renamed shop' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save shop identity' }))
    await screen.findByText('Shop identity saved. Open its browser and sign in.')
    expect(saveProjectShop).toHaveBeenCalledWith('project-1', { ...identity, name: 'Renamed shop' }, closed)
  })

  it('serializes browser controls and surfaces a conflict without claiming that a window opened', async () => {
    vi.mocked(listProjectShops).mockResolvedValue([{ ...shop, browser_state: 'closed' }])
    let reject!: (reason: Error) => void
    vi.mocked(controlProjectShop).mockImplementation(() => new Promise((_, fail) => { reject = fail }))
    render(<ProjectShopsPanel projectId="project-1" />)
    await open()
    const button = screen.getByRole('button', { name: 'Open shop login browser' })
    fireEvent.click(button); fireEvent.click(button)
    expect(controlProjectShop).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Save shop identity' })).toBeDisabled()
    reject(new Error('Shop access changed. Refresh and try again.'))
    await screen.findByRole('alert')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh shops' })).toBeEnabled()
  })

  it('shows registered reports and sources, then clears shop details when access is removed', async () => {
    vi.mocked(getProjectShopData).mockResolvedValue([{
      evidence: { id: 'ev-1', source_title: 'Seller report', retrieved_at: '2026-09-09T00:00:00Z', source_url: 'https://seller.example.com/' },
      artifacts: [{ id: 'a-1', name: 'sales.csv', download_url: '/api/projects/project-1/artifacts/a-1/download' }],
    }] as ShopData[])
    render(<ProjectShopsPanel projectId="project-1" />)
    await open()
    fireEvent.click(screen.getByRole('button', { name: /View collected shop data/ }))
    expect(await screen.findByRole('link', { name: 'sales.csv' })).toHaveAttribute('href', '/api/projects/project-1/artifacts/a-1/download')
    expect(screen.getByText('https://seller.example.com/')).toBeInTheDocument()
    vi.mocked(listProjectShops).mockRejectedValue(Object.assign(new Error('Access removed'), { status: 403 }))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh shops' }))
    await screen.findByRole('alert')
    expect(screen.queryByRole('link', { name: 'sales.csv' })).not.toBeInTheDocument()
    expect(screen.queryByText('US shop')).not.toBeInTheDocument()
  })

  it('drops a previous project response after its keyed panel is replaced', async () => {
    let resolve!: (items: ProjectShop[]) => void
    vi.mocked(listProjectShops).mockImplementationOnce(() => new Promise((done) => { resolve = done }))
    const { rerender } = render(<ProjectShopsPanel key="project-1" projectId="project-1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Project shops' }))
    rerender(<ProjectShopsPanel key="project-2" projectId="project-2" />)
    resolve([shop])
    vi.mocked(listProjectShops).mockResolvedValue([])
    fireEvent.click(screen.getByRole('button', { name: 'Project shops' }))
    await screen.findByText('No shop identities in this project yet.')
    expect(screen.queryByText('US shop')).not.toBeInTheDocument()
  })
})
