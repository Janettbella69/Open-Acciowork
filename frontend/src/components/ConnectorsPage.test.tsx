import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Connector } from '../api/types'
import ConnectorsPage from './ConnectorsPage'

vi.mock('../api/client', () => ({
  listConnectors: vi.fn(),
  listMarketplaceGuides: vi.fn(),
  checkConnectorHealth: vi.fn(),
  upsertConnector: vi.fn(),
  revokeConnector: vi.fn(),
  listProjects: vi.fn(),
  listUserMcpCatalog: vi.fn(),
  listUserMcp: vi.fn(),
  addCatalogMcp: vi.fn(),
  saveUserMcp: vi.fn(),
  reviewUserMcp: vi.fn(),
  setUserMcpToolPolicy: vi.fn(),
  revokeUserMcp: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))

import { checkConnectorHealth, listConnectors, listMarketplaceGuides, listProjects, revokeConnector } from '../api/client'

const connector: Connector = {
  id: 'connector_gmail',
  type: 'gmail',
  display_name: 'Gmail Inbox',
  scope: 'mail:read',
  secret_env: 'OPHIRA_CONNECTOR_GMAIL_KEY',
  endpoint: null,
  enabled: true,
  health: 'unknown',
  last_checked_at: null,
  created_at: '2026-08-20T08:00:00Z',
}
const shopify: Connector = {
  ...connector, id: 'shopify', type: 'shopify', display_name: 'Shopify',
  scope: 'read_products read_orders', secret_env: 'OPHIRA_CONNECTOR_SHOPIFY_KEY',
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listConnectors).mockResolvedValue([connector, shopify])
  vi.mocked(listMarketplaceGuides).mockResolvedValue([])
  vi.mocked(listProjects).mockResolvedValue([])
  vi.mocked(checkConnectorHealth).mockResolvedValue({
    ...connector,
    health: 'ok',
    last_checked_at: '2026-08-21T08:00:00Z',
  })
  vi.mocked(revokeConnector).mockResolvedValue({ ...connector, enabled: false, health: 'ok' })
})

describe('ConnectorsPage', () => {
  it('lists credential-safe connector metadata and performs a health check', async () => {
    render(<ConnectorsPage />)

    const card = (await screen.findByText('Gmail Inbox')).closest('article')!
    expect(screen.getByText('OPHIRA_CONNECTOR_GMAIL_KEY')).toBeInTheDocument()
    expect(screen.getByText('Not verified')).toBeInTheDocument()
    expect(screen.getByText('Configure store access')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /check configuration/i }))
    await waitFor(() => expect(checkConnectorHealth).toHaveBeenCalledWith('connector_gmail'))
    expect(card.querySelector('[data-health="ok"]')).toHaveTextContent('Configured · not verified')
    expect(screen.queryByText('Connected')).not.toBeInTheDocument()
  })

  it('locks a revoked connector and never leaves it looking connected', async () => {
    vi.mocked(listConnectors).mockResolvedValue([connector])
    render(<ConnectorsPage />)

    await screen.findByText('Gmail Inbox')
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))

    await waitFor(() => expect(revokeConnector).toHaveBeenCalledWith('connector_gmail'))
    expect(screen.getByText('Revoked')).toBeInTheDocument()
    expect(screen.getByText(/cannot be restored here/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /check configuration/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /save secret/i })).not.toBeInTheDocument()
  })
})
