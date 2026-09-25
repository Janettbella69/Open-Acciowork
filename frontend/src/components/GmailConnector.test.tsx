import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { Connector } from '../api/types'
import GmailConnector from './GmailConnector'

vi.mock('../api/client', () => ({
  getGmailOAuthStatus: vi.fn(),
  startGmailOAuth: vi.fn(),
  upsertConnector: vi.fn(),
  revokeConnector: vi.fn(),
  errorMessage: (cause: unknown) => cause instanceof Error ? cause.message : String(cause),
}))

import { getGmailOAuthStatus, startGmailOAuth, upsertConnector } from '../api/client'

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
