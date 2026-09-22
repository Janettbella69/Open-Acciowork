import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProjectShop, ShopData } from '../api/shops'
import ProjectShopsPanel from './ProjectShopsPanel'

vi.mock('../api/client', () => ({
  apiBaseUrl: '', controlProjectShop: vi.fn(), getProjectShopData: vi.fn(), grantProjectShop: vi.fn(),
  listConnectors: vi.fn(), listProjectShops: vi.fn(), saveProjectShop: vi.fn(),
  errorMessage: (reason: unknown) => reason instanceof Error ? reason.message : 'Unknown error',
}))
import { controlProjectShop, getProjectShopData, grantProjectShop, listConnectors, listProjectShops, saveProjectShop } from '../api/client'

const identity = { name: 'US shop', platform: 'tiktok_shop' as const, seller_shop_id: 'seller-1', connector_id: null, entry_url: 'https://seller.example.com/', additional_domains: ['assets.example.com'] }
const shop: ProjectShop = { ...identity, id: 'shop-1', project_id: 'project-1', revision: 2, browser_state: 'open', session_id: 'session-1', agent_access: false, grant_expires_at: null, capture_count: 1 }

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
