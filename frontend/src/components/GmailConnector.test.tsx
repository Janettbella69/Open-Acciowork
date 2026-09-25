import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { Connector, OpportunityProject } from '../api/types'
import GmailConnector from './GmailConnector'

vi.mock('../api/client', () => ({
  getGmailOAuthStatus: vi.fn(),
  startGmailOAuth: vi.fn(),
  upsertConnector: vi.fn(),
  revokeConnector: vi.fn(),
  listProjects: vi.fn(),
  getGmailProjectAccess: vi.fn(),
  grantGmailProjectAccess: vi.fn(),
  revokeGmailProjectAccess: vi.fn(),
  errorMessage: (cause: unknown) => cause instanceof Error ? cause.message : String(cause),
}))

import { getGmailOAuthStatus, getGmailProjectAccess, grantGmailProjectAccess, listProjects, revokeGmailProjectAccess, startGmailOAuth, upsertConnector } from '../api/client'

const connector: Connector = {
  id: 'gmail', type: 'gmail', display_name: 'Gmail',
  scope: 'https://www.googleapis.com/auth/gmail.readonly',
  secret_env: 'OPHIRA_CONNECTOR_GMAIL_KEY', endpoint: null,
  enabled: true, health: 'unknown', last_checked_at: null,
  created_at: '2026-09-25T00:00:00Z', revision: 1,
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getGmailOAuthStatus).mockResolvedValue({
    authorized: false, email: null,
    callback_url: 'https://events.example.com/api/connectors/gmail/gmail-oauth/callback',
  })
})

it('grants and revokes Agent mailbox access for a selected project', async () => {
  vi.mocked(getGmailOAuthStatus).mockResolvedValue({
    authorized: true, email: 'inbox@example.com', callback_url: 'https://events.example.com/callback',
  })
  vi.mocked(listProjects).mockResolvedValue([{ id: 'project-1', name: 'Research' } as OpportunityProject])
  vi.mocked(getGmailProjectAccess).mockResolvedValue({
    project_id: 'project-1', revision: 0, grants: [],
  })
  const grant = {
    id: 'gmail_grant_0123456789abcdef0123456789abcdef', connector_id: 'gmail',
    connector_revision: 1, mailbox: 'inbox@example.com', created_by: 'owner',
    created_at: '2026-09-25T00:00:00Z', expires_at: '2026-10-02T00:00:00Z',
    revoked_at: null, revoked_by: null, active: true,
  }
  vi.mocked(grantGmailProjectAccess).mockResolvedValue({
    project_id: 'project-1', revision: 1, grants: [grant],
  })
  vi.mocked(revokeGmailProjectAccess).mockResolvedValue({
    project_id: 'project-1', revision: 2,
    grants: [{ ...grant, revoked_at: '2026-09-25T01:00:00Z', revoked_by: 'owner', active: false }],
  })
  render(<GmailConnector connector={connector} onChange={vi.fn()} />)
  await screen.findByText('Mailbox authorized')
  fireEvent.click(screen.getByText('Project access for Agents'))
  await waitFor(() => expect(listProjects).toHaveBeenCalled())
  fireEvent.change(screen.getByLabelText('Project for Gmail access'), {
    target: { value: 'project-1' },
  })
  await waitFor(() => expect(getGmailProjectAccess).toHaveBeenCalledWith('project-1'))
  fireEvent.click(screen.getByRole('button', { name: 'Allow Agent Gmail reads' }))
  await waitFor(() => expect(grantGmailProjectAccess).toHaveBeenCalledWith('project-1', {
    expected_revision: 0, connector_id: 'gmail', expires_in_seconds: 7 * 86400,
  }))
  await screen.findByText('inbox@example.com', { selector: 'td' })
  fireEvent.click(screen.getByRole('button', { name: 'Revoke project access' }))
  await waitFor(() => expect(revokeGmailProjectAccess).toHaveBeenCalledWith(
    'project-1', grant.id, 1,
  ))
})

it('saves read-only OAuth credentials and starts browser authorization', async () => {
  const updated = { ...connector, revision: 2 }
  vi.mocked(upsertConnector).mockResolvedValue(updated)
  vi.mocked(startGmailOAuth).mockResolvedValue({
    authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth?state=example',
    callback_url: 'https://events.example.com/api/connectors/gmail/gmail-oauth/callback',
  })
  const onChange = vi.fn()
  render(<GmailConnector connector={connector} onChange={onChange} />)
  await screen.findByText(/Gmail OAuth callback URL/)
  fireEvent.click(screen.getByText('Configure Gmail OAuth'))
  fireEvent.change(screen.getByLabelText('OAuth web client ID'), {
    target: { value: '123456789012-abcdefgh.apps.googleusercontent.com' },
  })
  fireEvent.change(screen.getByLabelText('OAuth client secret'), {
    target: { value: 'google-client-secret-1234567890' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Save Gmail credentials' }))
  await waitFor(() => expect(upsertConnector).toHaveBeenCalledWith('gmail', expect.objectContaining({
    scope: 'https://www.googleapis.com/auth/gmail.readonly',
    secret: JSON.stringify({
      client_id: '123456789012-abcdefgh.apps.googleusercontent.com',
      client_secret: 'google-client-secret-1234567890',
    }),
  })))
  expect(onChange).toHaveBeenCalledWith(updated)
  expect(screen.getByLabelText('OAuth client secret')).toHaveValue('')
  fireEvent.click(screen.getByRole('button', { name: 'Authorize Gmail mailbox' }))
  await waitFor(() => expect(startGmailOAuth).toHaveBeenCalledWith('gmail'))
  expect(screen.getByRole('link', { name: 'Continue to Google authorization' })).toHaveAttribute(
    'href', 'https://accounts.google.com/o/oauth2/v2/auth?state=example',
  )
})
