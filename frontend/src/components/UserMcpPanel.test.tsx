import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { OpportunityProject, UserMcpConnector } from '../api/types'
import UserMcpPanel from './UserMcpPanel'

vi.mock('../api/client', () => ({
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

import {
  addCatalogMcp, listProjects, listUserMcp, listUserMcpCatalog, saveUserMcp,
  setUserMcpToolPolicy,
} from '../api/client'

const reviewed: UserMcpConnector = {
  id: 'claude_code_docs', project_id: 'project_one', name: 'Claude Code documentation',
  url: 'https://code.claude.com/docs/mcp', revision: 2, enabled: true,
  reviewed_at: '2026-09-24T00:00:00Z', review_error: null,
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
})
