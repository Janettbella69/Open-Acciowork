import { useState } from 'react'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Connector, OpportunityProject } from '../api/types'
import type { MarketplaceAccess } from '../api/marketplaces'
import MarketplaceProjectAccess from './MarketplaceProjectAccess'

vi.mock('../api/client', () => ({
  listProjects: vi.fn(), getMarketplaceAccess: vi.fn(), grantMarketplaceAccess: vi.fn(),
  revokeMarketplaceAccess: vi.fn(),
  errorMessage: (reason: unknown) => reason instanceof Error ? reason.message : 'unknown error',
}))
import { getMarketplaceAccess, grantMarketplaceAccess, listProjects, revokeMarketplaceAccess } from '../api/client'

const connector: Connector = {
  id: 'tiktok_shop', type: 'tiktok_shop', display_name: 'TikTok Shop',
  scope: 'seller.authorization.info seller.product.basic', endpoint: null, secret_env: null,
  enabled: true, health: 'unknown', created_at: '2026-09-09T00:00:00Z', last_checked_at: null,
}
const empty: MarketplaceAccess = { project_id: 'project-1', revision: 0, bindings: [] }
const granted: MarketplaceAccess = { project_id: 'project-1', revision: 1, bindings: [{
  id: 'binding-1', connector_id: 'tiktok_shop', shop_id: 'shop-1', shop_name: 'My shop',
  region: 'US', created_by: 'owner', created_at: '2026-09-09T00:00:00Z',
  expires_at: '2030-09-09T00:00:00Z', revoked_at: null, revoked_by: null, active: true,
}] }

function Harness({ selected = true, enabled = true }: { selected?: boolean; enabled?: boolean }) {
  const [busy, setBusy] = useState(false)
  return <>
    <button type="button" disabled={busy}>Parent read action</button>
    <MarketplaceProjectAccess connector={{ ...connector, enabled }}
      selectedShop={selected ? { id: 'shop-1', name: 'My shop' } : null}
      busy={busy} onBusyChange={setBusy} />
  </>
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(listProjects).mockResolvedValue([
    { id: 'project-1', name: 'Launch' }, { id: 'project-2', name: 'Private study' },
  ] as OpportunityProject[])
  vi.mocked(getMarketplaceAccess).mockResolvedValue(empty)
  vi.mocked(grantMarketplaceAccess).mockResolvedValue(granted)
  vi.mocked(revokeMarketplaceAccess).mockResolvedValue({ ...granted, revision: 2,
    bindings: [{ ...granted.bindings[0], active: false, revoked_at: '2026-09-09T01:00:00Z', revoked_by: 'owner' }],
  })
})

async function selectProject() {
  fireEvent.click(screen.getByText('Project access for Agents'))
  await screen.findByRole('option', { name: 'Launch' })
  fireEvent.change(screen.getByLabelText('Project for shop access'), { target: { value: 'project-1' } })
  await screen.findByText('No shop grants for this connector in the selected project.')
}

describe('MarketplaceProjectAccess', () => {
  it('requires an explicit project selection and binds the selected shop with revision and expiry', async () => {
    render(<Harness />)
    expect(listProjects).not.toHaveBeenCalled()
    await selectProject()
    expect(grantMarketplaceAccess).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Shop access duration'), { target: { value: '1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Allow Agent reads for selected shop' }))
    await screen.findByText('Agent product reads allowed for My shop in this project.')
    expect(grantMarketplaceAccess).toHaveBeenCalledWith('project-1', {
      expected_revision: 0, connector_id: 'tiktok_shop', shop_id: 'shop-1', expires_in_seconds: 86400,
    })
    expect(within(screen.getByRole('table')).getByText('My shop')).toBeInTheDocument()
    expect(screen.getByText('Active')).toBeInTheDocument()
  })

  it('revokes existing grants even when the connector is disabled and no shop was read', async () => {
    vi.mocked(getMarketplaceAccess).mockResolvedValue(granted)
    render(<Harness selected={false} enabled={false} />)
    fireEvent.click(screen.getByText('Project access for Agents'))
    await screen.findByRole('option', { name: 'Launch' })
    fireEvent.change(screen.getByLabelText('Project for shop access'), { target: { value: 'project-1' } })
    await screen.findByText('Active')
    expect(screen.getByRole('button', { name: 'Allow Agent reads for selected shop' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Revoke project access' }))
    await screen.findByText('Revoked')
    expect(revokeMarketplaceAccess).toHaveBeenCalledWith('project-1', 'binding-1', 1)
    expect(screen.queryByRole('button', { name: 'Revoke project access' })).not.toBeInTheDocument()
  })

  it('locks connector actions during a grant and offers refresh after a revision conflict', async () => {
    render(<Harness />)
    await selectProject()
    let rejectGrant!: (error: Error) => void
    vi.mocked(grantMarketplaceAccess).mockImplementation(() => new Promise((_, reject) => { rejectGrant = reject }))
    const button = screen.getByRole('button', { name: 'Allow Agent reads for selected shop' })
    fireEvent.click(button); fireEvent.click(button)
    expect(grantMarketplaceAccess).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Parent read action' })).toBeDisabled()
    rejectGrant(new Error('Project access changed. Refresh and try again.'))
    await screen.findByRole('alert')
    expect(screen.queryByText('Active')).not.toBeInTheDocument()
    vi.mocked(getMarketplaceAccess).mockResolvedValue(granted)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh shop grants' }))
    await screen.findByText('Active')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('clears the previous project grant list when changing project and surfaces authorization denial', async () => {
    render(<Harness />)
    await selectProject()
    fireEvent.click(screen.getByRole('button', { name: 'Allow Agent reads for selected shop' }))
    await screen.findByText('Active')
    vi.mocked(getMarketplaceAccess).mockRejectedValue(new Error('Not authorized for this project'))
    fireEvent.change(screen.getByLabelText('Project for shop access'), { target: { value: 'project-2' } })
    await screen.findByRole('alert')
    expect(screen.queryByText('Active')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Allow Agent reads for selected shop' })).toBeDisabled()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Parent read action' })).not.toBeDisabled())
  })
})
