import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { PluginImportView } from '../api/pluginImports'
import {
  getPluginImport, getSkillRepositoryCapabilities, listPluginImports, submitPluginImport,
} from '../api/client'
import PluginImportsPanel from './PluginImportsPanel'

vi.mock('../api/client', () => ({
  getSkillRepositoryCapabilities: vi.fn(),
  listPluginImports: vi.fn(),
  getPluginImport: vi.fn(),
  submitPluginImport: vi.fn(),
  errorMessage: (cause: unknown) => String(cause),
}))

const view: PluginImportView = {
  import: {
    schema_version: 1,
    id: 'plugin_import_' + 'a'.repeat(64), request_id: 'b'.repeat(32),
    archive_hash: 'c'.repeat(64),
    scope: { auth_mode: 'trusted_gateway', tenant_id: 'tenant_one' },
    uploaded_by: { kind: 'principal', id: 'owner@example.com', role: 'owner' },
    name: 'sample-plugin', version: '1.2.0', description: 'Sample plugin',
    skills: [{ path: 'skills/research', import_id: 'skill_import_' + 'd'.repeat(64),
      skill_id: 'ophira-tenant:research', content_hash: 'e'.repeat(64), scan_ok: true }],
    mcp_servers: [{ name: 'remote', url: 'https://mcp.example.com/mcp', credentials_required: true }],
    ignored_agents: ['agents/reviewer.md'], ignored_components: ['hooks'],
    created_at: '2026-09-24T00:00:00Z',
  },
  skill_states: [{ path: 'skills/research', import_id: 'skill_import_' + 'd'.repeat(64),
    skill_id: 'ophira-tenant:research', state: 'pending', scan_ok: true }],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getSkillRepositoryCapabilities).mockResolvedValue({
    identity_mode: 'trusted_gateway', tenant_id: 'tenant_one',
    actor_id: 'owner@example.com', can_manage: true,
  })
  vi.mocked(listPluginImports).mockResolvedValue([])
  vi.mocked(getPluginImport).mockResolvedValue(view)
  vi.mocked(submitPluginImport).mockResolvedValue(view)
})

it('stages a plugin ZIP and directs each component through its own review', async () => {
  render(<PluginImportsPanel />)
  const input = await screen.findByLabelText('Choose plugin ZIP')
  fireEvent.change(input, { target: { files: [new File(['zip'], 'plugin.zip', { type: 'application/zip' })] } })
  fireEvent.click(screen.getByRole('button', { name: 'Inspect plugin package' }))
  await waitFor(() => expect(submitPluginImport).toHaveBeenCalled())
  expect(await screen.findByText('agents/reviewer.md', { exact: false })).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Review Skill files and decision' })).toHaveAttribute(
    'href', `/app/plugins/skills?import=${view.import.skills[0].import_id}`,
  )
  const mcp = screen.getByRole('link', { name: 'Set up in a project' })
  expect(mcp.getAttribute('href')).toContain('/app/plugins/connectors?')
  expect(mcp.getAttribute('href')).toContain('plugin_mcp_url=https%3A%2F%2Fmcp.example.com%2Fmcp')
  expect(screen.getByText('Enter credentials separately; the ZIP credentials were not imported.')).toBeInTheDocument()
})

it('keeps the import controls hidden from a viewer', async () => {
  vi.mocked(getSkillRepositoryCapabilities).mockResolvedValue({
    identity_mode: 'trusted_gateway', tenant_id: 'tenant_one',
    actor_id: 'viewer@example.com', can_manage: false,
  })
  render(<PluginImportsPanel />)
  await waitFor(() => expect(getSkillRepositoryCapabilities).toHaveBeenCalled())
  expect(screen.queryByLabelText('Choose plugin ZIP')).not.toBeInTheDocument()
  expect(listPluginImports).not.toHaveBeenCalled()
})
