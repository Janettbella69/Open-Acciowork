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
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))

import { checkConnectorHealth, listConnectors, listMarketplaceGuides, revokeConnector } from '../api/client'

const connector: Connector = {
  id: 'connector_shopify',
  type: 'shopify',
  display_name: 'Shopify Store',
  scope: 'workspace',
  secret_env: 'SHOPIFY_ACCESS_TOKEN',
  endpoint: 'https://example.myshopify.com',
  enabled: true,
  health: 'unknown',
  last_checked_at: null,
  created_at: '2026-08-20T08:00:00Z',
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listConnectors).mockResolvedValue([connector])
  vi.mocked(listMarketplaceGuides).mockResolvedValue([])
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

    const card = (await screen.findByText('Shopify Store')).closest('article')!
    expect(screen.getByText('SHOPIFY_ACCESS_TOKEN')).toBeInTheDocument()
    expect(screen.getByText('Not verified')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /check configuration/i }))
    await waitFor(() => expect(checkConnectorHealth).toHaveBeenCalledWith('connector_shopify'))
    expect(card.querySelector('[data-health="ok"]')).toHaveTextContent('Configured · not verified')
    expect(screen.queryByText('Connected')).not.toBeInTheDocument()
  })

  it('locks a revoked connector and never leaves it looking connected', async () => {
    render(<ConnectorsPage />)

    await screen.findByText('Shopify Store')
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))

    await waitFor(() => expect(revokeConnector).toHaveBeenCalledWith('connector_shopify'))
    expect(screen.getByText('Revoked')).toBeInTheDocument()
    expect(screen.getByText(/cannot be restored here/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /check configuration/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /save secret/i })).not.toBeInTheDocument()
  })
})
