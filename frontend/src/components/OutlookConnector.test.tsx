import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { Connector, OpportunityProject } from '../api/types'
import OutlookConnector from './OutlookConnector'

vi.mock('../api/client', () => ({
  getOutlookOAuthStatus: vi.fn(),
  startOutlookOAuth: vi.fn(),
  upsertConnector: vi.fn(),
  revokeConnector: vi.fn(),
  listProjects: vi.fn(),
  getOutlookProjectAccess: vi.fn(),
  grantOutlookProjectAccess: vi.fn(),
  revokeOutlookProjectAccess: vi.fn(),
  errorMessage: (cause: unknown) => cause instanceof Error ? cause.message : String(cause),
}))

import { getOutlookOAuthStatus, getOutlookProjectAccess, grantOutlookProjectAccess, listProjects, revokeOutlookProjectAccess, startOutlookOAuth, upsertConnector } from '../api/client'

const connector: Connector = {
  id: 'outlook', type: 'outlook', display_name: 'Outlook',
  scope: 'offline_access User.Read Mail.Read',
  secret_env: 'OPHIRA_CONNECTOR_OUTLOOK_KEY', endpoint: null,
  enabled: true, health: 'unknown', last_checked_at: null,
  created_at: '2026-09-25T00:00:00Z', revision: 1,
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getOutlookOAuthStatus).mockResolvedValue({
    authorized: false, email: null,
    callback_url: 'https://events.example.com/api/connectors/outlook/outlook-oauth/callback',
  })
})

it('grants and revokes Agent mailbox access for a selected project', async () => {
  vi.mocked(getOutlookOAuthStatus).mockResolvedValue({
    authorized: true, email: 'inbox@example.com', callback_url: 'https://events.example.com/callback',
  })
  vi.mocked(listProjects).mockResolvedValue([{ id: 'project-1', name: 'Research' } as OpportunityProject])
  vi.mocked(getOutlookProjectAccess).mockResolvedValue({
    project_id: 'project-1', revision: 0, grants: [],
  })
  const grant = {
    id: 'outlook_grant_0123456789abcdef0123456789abcdef', connector_id: 'outlook',
    connector_revision: 1, user_id: 'user-1', mailbox: 'inbox@example.com', created_by: 'owner',
    created_at: '2026-09-25T00:00:00Z', expires_at: '2026-10-02T00:00:00Z',
    revoked_at: null, revoked_by: null, active: true,
  }
  vi.mocked(grantOutlookProjectAccess).mockResolvedValue({
    project_id: 'project-1', revision: 1, grants: [grant],
  })
  vi.mocked(revokeOutlookProjectAccess).mockResolvedValue({
    project_id: 'project-1', revision: 2,
    grants: [{ ...grant, revoked_at: '2026-09-25T01:00:00Z', revoked_by: 'owner', active: false }],
  })
  render(<OutlookConnector connector={connector} onChange={vi.fn()} />)
  await screen.findByText('Mailbox authorized')
  fireEvent.click(screen.getByText('Project access for Agents'))
  await waitFor(() => expect(listProjects).toHaveBeenCalled())
  fireEvent.change(screen.getByLabelText('Project for Outlook access'), {
    target: { value: 'project-1' },
  })
  await waitFor(() => expect(getOutlookProjectAccess).toHaveBeenCalledWith('project-1'))
  fireEvent.click(screen.getByRole('button', { name: 'Allow Agent Outlook reads' }))
  await waitFor(() => expect(grantOutlookProjectAccess).toHaveBeenCalledWith('project-1', {
    expected_revision: 0, connector_id: 'outlook', expires_in_seconds: 7 * 86400,
  }))
  await screen.findByText('inbox@example.com', { selector: 'td' })
  fireEvent.click(screen.getByRole('button', { name: 'Revoke project access' }))
  await waitFor(() => expect(revokeOutlookProjectAccess).toHaveBeenCalledWith(
    'project-1', grant.id, 1,
  ))
})

it('saves read-only OAuth credentials and starts browser authorization', async () => {
  const updated = { ...connector, revision: 2 }
  vi.mocked(upsertConnector).mockResolvedValue(updated)
  vi.mocked(startOutlookOAuth).mockResolvedValue({
    authorization_url: 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize?state=example',
    callback_url: 'https://events.example.com/api/connectors/outlook/outlook-oauth/callback',
  })
  const onChange = vi.fn()
  render(<OutlookConnector connector={connector} onChange={onChange} />)
  await screen.findByText(/Outlook OAuth callback URL/)
  fireEvent.click(screen.getByText('Configure Outlook OAuth'))
  fireEvent.change(screen.getByLabelText('OAuth web client ID'), {
    target: { value: '11111111-2222-3333-4444-555555555555' },
  })
  fireEvent.change(screen.getByLabelText('OAuth client secret'), {
    target: { value: 'microsoft-client-secret-1234567890' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Save Outlook credentials' }))
  await waitFor(() => expect(upsertConnector).toHaveBeenCalledWith('outlook', expect.objectContaining({
    scope: 'offline_access User.Read Mail.Read',
    secret: JSON.stringify({
      client_id: '11111111-2222-3333-4444-555555555555',
      client_secret: 'microsoft-client-secret-1234567890',
    }),
  })))
  expect(onChange).toHaveBeenCalledWith(updated)
  expect(screen.getByLabelText('OAuth client secret')).toHaveValue('')
  fireEvent.click(screen.getByRole('button', { name: 'Authorize Outlook mailbox' }))
  await waitFor(() => expect(startOutlookOAuth).toHaveBeenCalledWith('outlook'))
  expect(screen.getByRole('link', { name: 'Continue to Microsoft authorization' })).toHaveAttribute(
    'href', 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize?state=example',
  )
})
