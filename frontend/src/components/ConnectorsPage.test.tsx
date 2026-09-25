import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Connector } from '../api/types'
import ConnectorsPage from './ConnectorsPage'

vi.mock('../utils/credentialDownload', () => ({ saveCredentialDownload: vi.fn() }))

vi.mock('../api/client', () => ({
  listConnectors: vi.fn(),
  listMarketplaceGuides: vi.fn(),
  checkConnectorHealth: vi.fn(),
  upsertConnector: vi.fn(),
  getShopifyOAuthStatus: vi.fn(),
  startShopifyOAuth: vi.fn(),
  revokeConnector: vi.fn(),
  downloadConnectorCredential: vi.fn(),
  listProjects: vi.fn(),
  listUserMcpCatalog: vi.fn(),
  listUserMcp: vi.fn(),
  addCatalogMcp: vi.fn(),
  saveUserMcp: vi.fn(),
  reviewUserMcp: vi.fn(),
  setUserMcpToolPolicy: vi.fn(),
  revokeUserMcp: vi.fn(),
  downloadUserMcpCredential: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))

import { checkConnectorHealth, downloadConnectorCredential, getShopifyOAuthStatus, listConnectors, listMarketplaceGuides, listProjects, revokeConnector } from '../api/client'
import { saveCredentialDownload } from '../utils/credentialDownload'

const connector: Connector = {
  id: 'connector_search',
  type: 'search',
  display_name: 'Public web search',
  scope: 'web:search',
  secret_env: 'OPHIRA_SEARCH_API_KEY',
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
  vi.mocked(getShopifyOAuthStatus).mockResolvedValue({
    auth_mode: 'client_credentials', authorized: false, callback_url: null,
  })
  vi.mocked(checkConnectorHealth).mockResolvedValue({
    ...connector,
    health: 'ok',
    last_checked_at: '2026-08-21T08:00:00Z',
  })
  vi.mocked(revokeConnector).mockResolvedValue({ ...connector, enabled: false, health: 'ok' })
})

describe('ConnectorsPage', () => {
  it('requires a second action before downloading a raw connector credential', async () => {
    vi.mocked(listConnectors).mockResolvedValue([connector])
    vi.mocked(downloadConnectorCredential).mockResolvedValue(new Blob(['private']))
    render(<ConnectorsPage />)
    await screen.findByText('Connector credential downloads')
    fireEvent.click(screen.getByRole('button', { name: 'Export credential…' }))
    expect(downloadConnectorCredential).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Download credential file' }))
    await waitFor(() => expect(downloadConnectorCredential).toHaveBeenCalledWith('connector_search'))
    expect(saveCredentialDownload).toHaveBeenCalledWith(expect.any(Blob), 'ophira-connector-connector_search.json')
  })

  it('lists credential-safe connector metadata and performs a health check', async () => {
    render(<ConnectorsPage />)

    const card = (await screen.findByRole('heading', { name: 'Public web search' })).closest('article')!
    expect(screen.getByText('OPHIRA_SEARCH_API_KEY')).toBeInTheDocument()
    expect(screen.getByText('Not verified')).toBeInTheDocument()
    expect(screen.getByText('Configure store access')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /check configuration/i }))
    await waitFor(() => expect(checkConnectorHealth).toHaveBeenCalledWith('connector_search'))
    expect(card.querySelector('[data-health="ok"]')).toHaveTextContent('Configured · not verified')
    expect(screen.queryByText('Connected')).not.toBeInTheDocument()
  })

  it('locks a revoked connector and never leaves it looking connected', async () => {
    vi.mocked(listConnectors).mockResolvedValue([connector])
    render(<ConnectorsPage />)

    await screen.findByRole('heading', { name: 'Public web search' })
    fireEvent.click(screen.getByRole('button', { name: 'Revoke' }))

    await waitFor(() => expect(revokeConnector).toHaveBeenCalledWith('connector_search'))
    expect(screen.getByText('Revoked')).toBeInTheDocument()
    expect(screen.getByText(/cannot be restored here/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /check configuration/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /save secret/i })).not.toBeInTheDocument()
  })
})
