import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  presentationComponents,
  presentationSchemaVersions,
  type AgentProfile,
  type ProtocolMetadata,
} from '../api/types'
import AgentsPage from './AgentsPage'

vi.mock('../api/client', () => ({
  getProtocol: vi.fn(),
  listAgents: vi.fn(),
  createAgent: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))

import { createAgent, getProtocol, listAgents } from '../api/client'

const protocol = {
  version: '0.1.0',
  event_types: [],
  run_statuses: [],
  run_modes: [],
  provider_error_codes: [],
  dimensions: [],
  project_stages: [],
  permission_decisions: [],
  permission_modes: [],
  schedule_kinds: [],
  feedback_kinds: [],
  agent_templates: ['general', 'research', 'sourcing', 'marketing', 'docs', 'opportunity'],
  memory_scopes: [],
  memory_statuses: [],
  missed_run_policies: [],
  commercial_action_operations: [],
  commercial_action_statuses: [],
  commercial_review_decisions: [],
  external_execution_statuses: [],
  presentation_schema_versions: [...presentationSchemaVersions],
  presentation_components: [...presentationComponents],
} satisfies ProtocolMetadata

const personalAgent: AgentProfile = {
  id: 'agent_research',
  name: 'Gulf Market Researcher',
  template: 'research',
  role_description: 'Research Gulf hospitality demand with traceable sources.',
  model: null,
  revision: 2,
  created_at: '2026-08-20T08:00:00Z',
  updated_at: '2026-08-21T08:00:00Z',
}

beforeEach(() => {
  vi.mocked(getProtocol).mockResolvedValue(protocol)
  vi.mocked(listAgents).mockResolvedValue([personalAgent])
  vi.mocked(createAgent).mockResolvedValue({
    ...personalAgent,
    id: 'agent_new',
    name: 'Commerce Operator',
    template: 'general',
    revision: 1,
  })
})

describe('AgentsPage', () => {
  it('switches between public experts, expert teams, and personal Agents', async () => {
    render(<AgentsPage />)

    const researchCard = (await screen.findByText('Market Research Expert')).closest('article')!
    expect(within(researchCard).getByText(/traceable citations/i)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: 'Expert Teams' }))
    expect(await screen.findByText('Global Opportunity Team')).toBeInTheDocument()
    expect(screen.getByText(/one accountable lead coordinates demand/i)).toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: /personal/i }))
    fireEvent.click(screen.getByRole('tab', { name: 'Experts' }))
    const personalCard = (await screen.findByText(personalAgent.name)).closest('article')!
    expect(within(personalCard).getByRole('link', { name: /chat/i })).toHaveAttribute(
      'href',
      '/app?agent=agent_research',
    )
  })

  it('creates a personal Agent from a real backend template', async () => {
    render(<AgentsPage />)
    const templateCard = (await screen.findByText('Commerce Assistant')).closest('article')!
    fireEvent.click(within(templateCard).getByRole('button', { name: /use template/i }))

    const dialog = screen.getByRole('dialog', { name: /create an agent/i })
    fireEvent.change(within(dialog).getByLabelText(/agent name/i), { target: { value: 'Commerce Operator' } })
    fireEvent.click(within(dialog).getByRole('button', { name: /^create agent$/i }))

    await waitFor(() => expect(createAgent).toHaveBeenCalledWith(expect.objectContaining({
      name: 'Commerce Operator',
      template: 'general',
    })))
    expect(await screen.findByRole('link', { name: /chat now/i })).toHaveAttribute(
      'href',
      '/app?agent=agent_new',
    )
  })
})
