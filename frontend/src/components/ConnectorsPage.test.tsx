import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Connector } from '../api/types'
import ConnectorsPage from './ConnectorsPage'

vi.mock('../api/client', () => ({
  listConnectors: vi.fn(),
  checkConnectorHealth: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))

import { checkConnectorHealth, listConnectors } from '../api/client'

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
  vi.mocked(listConnectors).mockResolvedValue([connector])
  vi.mocked(checkConnectorHealth).mockResolvedValue({
    ...connector,
    health: 'ok',
    last_checked_at: '2026-08-21T08:00:00Z',
  })
})

describe('ConnectorsPage', () => {
  it('lists credential-safe connector metadata and performs a health check', async () => {
    render(<ConnectorsPage />)

    const card = (await screen.findByText('Shopify Store')).closest('article')!
    expect(screen.getByText('SHOPIFY_ACCESS_TOKEN')).toBeInTheDocument()
    expect(screen.getByText('Not checked')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /check connection/i }))
    await waitFor(() => expect(checkConnectorHealth).toHaveBeenCalledWith('connector_shopify'))
    expect(card.querySelector('[data-health="ok"]')).toHaveTextContent('Connected')
  })
})
