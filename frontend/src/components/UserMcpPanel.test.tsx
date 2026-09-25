import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { OpportunityProject, UserMcpConnector } from '../api/types'
import UserMcpPanel from './UserMcpPanel'

vi.mock('../api/client', () => ({
  listProjects: vi.fn(),
  listUserMcpCatalog: vi.fn(),
  listUserMcp: vi.fn(),
  getUserMcpOAuthSetup: vi.fn(),
  addCatalogMcp: vi.fn(),
  saveUserMcp: vi.fn(),
  reviewUserMcp: vi.fn(),
  setUserMcpToolPolicy: vi.fn(),
  startUserMcpOAuth: vi.fn(),
  revokeUserMcp: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))

import {
  addCatalogMcp, getUserMcpOAuthSetup, listProjects, listUserMcp, listUserMcpCatalog, saveUserMcp,
  setUserMcpToolPolicy, startUserMcpOAuth,
} from '../api/client'

const reviewed: UserMcpConnector = {
  id: 'claude_code_docs', project_id: 'project_one', name: 'Claude Code documentation',
  url: 'https://code.claude.com/docs/mcp', revision: 2, enabled: true,
  reviewed_at: '2026-09-24T00:00:00Z', review_error: null,
  auth_mode: 'bearer', oauth_issuer: null,
  oauth_client_configured: false, oauth_client_auth_method: null,
  credential_configured: false, tools: [{ name: 'search_docs', policy: 'ask' }],
}

beforeEach(() => {
  vi.clearAllMocks()
  window.history.replaceState({}, '', '/app/plugins/connectors')
  vi.mocked(listProjects).mockResolvedValue([
    { id: 'project_one', name: 'Kettle' } as OpportunityProject,
  ])
  vi.mocked(listUserMcpCatalog).mockResolvedValue([{
    id: reviewed.id, name: reviewed.name, url: reviewed.url,
    description: 'Search the official Claude Code documentation.',
  }])
  vi.mocked(listUserMcp).mockResolvedValue([])
  vi.mocked(getUserMcpOAuthSetup).mockResolvedValue({
    redirect_uri: 'https://gateway.example.com/api/mcp/oauth/callback',
    client_metadata_url: 'https://gateway.example.com/api/mcp/oauth/client-metadata',
  })
  vi.mocked(addCatalogMcp).mockResolvedValue(reviewed)
  vi.mocked(setUserMcpToolPolicy).mockResolvedValue({
    ...reviewed, revision: 3, tools: [{ name: 'search_docs', policy: 'allow' }],
  })
})

describe('UserMcpPanel', () => {
  it('adds a catalog server through review and sets a single tool policy', async () => {
    render(<UserMcpPanel />)
    fireEvent.click(await screen.findByRole('button', { name: 'Add and review' }))
    await waitFor(() => expect(addCatalogMcp).toHaveBeenCalledWith('project_one', reviewed.id))
    const policy = await screen.findByRole('combobox', { name: 'Policy for search_docs' })
    expect(policy).toHaveValue('ask')
    fireEvent.change(policy, { target: { value: 'allow' } })
    await waitFor(() => expect(setUserMcpToolPolicy).toHaveBeenCalledWith(
      'project_one', reviewed.id, 'search_docs', 'allow',
    ))
    expect(policy).toHaveValue('allow')
  })

  it('saves a custom address disabled pending a separate live review', async () => {
    vi.mocked(saveUserMcp).mockResolvedValue({
      ...reviewed, id: 'sales_mcp', name: 'Sales MCP', url: 'https://mcp.example.com/mcp',
      enabled: false, reviewed_at: null, tools: [],
    })
    render(<UserMcpPanel />)
    await screen.findByText('Connector directory')
    fireEvent.click(screen.getByText('Advanced: add a remote MCP address'))
    fireEvent.change(screen.getByLabelText('Server name'), { target: { value: 'Sales MCP' } })
    fireEvent.change(screen.getByLabelText('Server ID'), { target: { value: 'sales_mcp' } })
    fireEvent.change(screen.getByLabelText('HTTPS MCP address'), { target: { value: 'https://mcp.example.com/mcp' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save for review' }))
    await waitFor(() => expect(saveUserMcp).toHaveBeenCalledWith('project_one', 'sales_mcp', {
      name: 'Sales MCP', url: 'https://mcp.example.com/mcp',
    }))
    expect(await screen.findByText('Disabled · review required')).toBeInTheDocument()
  })

  it('prefills a plugin MCP address without importing its credentials or enabling it', async () => {
    window.history.replaceState({}, '',
      '/app/plugins/connectors?plugin_mcp_name=remote&plugin_mcp_id=plugin_sample_remote&plugin_mcp_url=https%3A%2F%2Fmcp.example.com%2Fmcp')
    render(<UserMcpPanel />)
    await screen.findByText('Connector directory')
    expect(screen.getByLabelText('Server name')).toHaveValue('remote')
    expect(screen.getByLabelText('Server ID')).toHaveValue('plugin_sample_remote')
    expect(screen.getByLabelText('HTTPS MCP address')).toHaveValue('https://mcp.example.com/mcp')
    expect(screen.getByLabelText('Bearer token (optional)')).toHaveValue('')
    expect(saveUserMcp).not.toHaveBeenCalled()
  })

  it('saves an OAuth issuer and offers the returned browser authorization link', async () => {
    const oauthServer: UserMcpConnector = {
      ...reviewed, id: 'sales_mcp', name: 'Sales MCP',
      url: 'https://mcp.example.com/mcp', auth_mode: 'oauth',
      oauth_issuer: 'https://auth.example.com',
      enabled: false, reviewed_at: null, credential_configured: false, tools: [],
    }
    vi.mocked(saveUserMcp).mockResolvedValue(oauthServer)
    vi.mocked(startUserMcpOAuth).mockResolvedValue({
      authorization_url: 'https://auth.example.com/authorize?state=trusted',
    })
    vi.mocked(listUserMcp).mockResolvedValue([oauthServer])
    render(<UserMcpPanel />)
    await screen.findByText('Connector directory')
    fireEvent.click(screen.getByText('Advanced: add a remote MCP address'))
    fireEvent.change(screen.getByLabelText('Server name'), { target: { value: 'Sales MCP' } })
    fireEvent.change(screen.getByLabelText('Server ID'), { target: { value: 'sales_mcp' } })
    fireEvent.change(screen.getByLabelText('HTTPS MCP address'), { target: { value: oauthServer.url } })
    fireEvent.change(screen.getByLabelText('OAuth issuer URL (optional)'), {
      target: { value: oauthServer.oauth_issuer },
    })
    expect(screen.getByLabelText('Bearer token (optional)')).toBeDisabled()
    expect(screen.getByText('https://gateway.example.com/api/mcp/oauth/callback')).toBeInTheDocument()
    expect(screen.getByText('https://gateway.example.com/api/mcp/oauth/client-metadata')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Save for review' }))
    await waitFor(() => expect(saveUserMcp).toHaveBeenCalledWith('project_one', 'sales_mcp', {
      name: 'Sales MCP', url: oauthServer.url, oauth_issuer: oauthServer.oauth_issuer,
    }))
    fireEvent.click(await screen.findByRole('button', { name: 'Connect with OAuth' }))
    await waitFor(() => expect(startUserMcpOAuth).toHaveBeenCalledWith('project_one', 'sales_mcp'))
    expect(await screen.findByRole('link', { name: 'Open OAuth authorization' })).toHaveAttribute(
      'href', 'https://auth.example.com/authorize?state=trusted',
    )
  })

  it('submits a pre-registered OAuth client while keeping its secret off the connector card', async () => {
    const oauthServer: UserMcpConnector = {
      ...reviewed, id: 'sales_mcp', url: 'https://mcp.example.com/mcp',
      auth_mode: 'oauth', oauth_issuer: 'https://auth.example.com',
      oauth_client_configured: true, oauth_client_auth_method: 'client_secret_post',
      enabled: false, reviewed_at: null, credential_configured: false, tools: [],
    }
    vi.mocked(saveUserMcp).mockResolvedValue(oauthServer)
    render(<UserMcpPanel />)
    await screen.findByText('Connector directory')
    fireEvent.click(screen.getByText('Advanced: add a remote MCP address'))
    fireEvent.change(screen.getByLabelText('Server name'), { target: { value: 'Sales MCP' } })
    fireEvent.change(screen.getByLabelText('Server ID'), { target: { value: 'sales_mcp' } })
    fireEvent.change(screen.getByLabelText('HTTPS MCP address'), { target: { value: oauthServer.url } })
    fireEvent.change(screen.getByLabelText('OAuth issuer URL (optional)'), {
      target: { value: oauthServer.oauth_issuer },
    })
    fireEvent.change(screen.getByLabelText('Pre-registered OAuth client ID (optional)'), {
      target: { value: 'registered-client' },
    })
    fireEvent.change(screen.getByLabelText('OAuth client secret (if required)'), {
      target: { value: 'registered-secret' },
    })
    fireEvent.change(screen.getByLabelText('Token endpoint authentication'), {
      target: { value: 'client_secret_post' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Save for review' }))
    await waitFor(() => expect(saveUserMcp).toHaveBeenCalledWith('project_one', 'sales_mcp', {
      name: 'Sales MCP', url: oauthServer.url, oauth_issuer: oauthServer.oauth_issuer,
      oauth_client_id: 'registered-client', oauth_client_secret: 'registered-secret',
      oauth_client_auth_method: 'client_secret_post',
    }))
    expect(await screen.findByText('Pre-registered OAuth client configured')).toBeInTheDocument()
    expect(screen.queryByText('registered-secret')).not.toBeInTheDocument()
  })

  it('does not start OAuth until the operator configures a callback origin', async () => {
    vi.mocked(getUserMcpOAuthSetup).mockResolvedValue({
      redirect_uri: null, client_metadata_url: null,
    })
    vi.mocked(listUserMcp).mockResolvedValue([{
      ...reviewed, auth_mode: 'oauth', oauth_issuer: 'https://auth.example.com',
      enabled: false, reviewed_at: null, credential_configured: false, tools: [],
    }])
    render(<UserMcpPanel />)
    expect(await screen.findByRole('button', { name: 'Connect with OAuth' })).toBeDisabled()
    expect(startUserMcpOAuth).not.toHaveBeenCalled()
  })
})
