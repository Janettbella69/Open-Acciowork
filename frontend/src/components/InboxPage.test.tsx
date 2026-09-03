import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Notification, OpportunityProject, StandingIntent } from '../api/types'
import InboxPage from './InboxPage'

vi.mock('../api/client', () => ({
  createIntent: vi.fn(),
  listIntents: vi.fn(),
  listNotifications: vi.fn(),
  listProjects: vi.fn(),
  markAllNotificationsRead: vi.fn(),
  markNotificationRead: vi.fn(),
  pauseIntent: vi.fn(),
  resumeIntent: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))

import { listIntents, listNotifications, listProjects } from '../api/client'

const notification: Notification = {
  id: 'notification_1',
  project_id: 'project_1',
  run_id: null,
  schedule_id: null,
  intent_id: null,
  severity: 'info',
  title: 'Price changed',
  body: 'The monitored SKU dropped by 8%.',
  read: false,
  created_at: '2026-09-02T00:00:00Z',
}

const intent: StandingIntent = {
  id: 'intent_1',
  project_id: 'project_1',
  intent: 'Watch supplier price changes',
  cooldown_hours: 12,
  max_triggers: 5,
  expires_at: '2026-10-02T00:00:00Z',
  triggered_count: 1,
  last_triggered_at: null,
  enabled: true,
  created_at: '2026-09-02T00:00:00Z',
}

const project = { id: 'project_1', name: 'Supplier watch' } as OpportunityProject

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listNotifications).mockResolvedValue([])
  vi.mocked(listIntents).mockResolvedValue([])
  vi.mocked(listProjects).mockResolvedValue([])
})

describe('InboxPage refresh', () => {
  it('keeps successful resources when another request fails and supports manual refresh', async () => {
    vi.mocked(listNotifications).mockRejectedValueOnce(new Error('Notifications unavailable'))
    vi.mocked(listIntents).mockResolvedValue([intent])
    vi.mocked(listProjects).mockResolvedValue([project])
    const setInterval = vi.spyOn(window, 'setInterval')
    const clearInterval = vi.spyOn(window, 'clearInterval')
    const { unmount } = render(<InboxPage />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Notifications unavailable')
    expect(screen.getByText('Watch supplier price changes')).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Supplier watch' })).toBeInTheDocument()
    expect(setInterval).toHaveBeenCalledWith(expect.any(Function), 30_000)

    vi.mocked(listNotifications).mockResolvedValueOnce([notification])
    fireEvent.click(screen.getByRole('button', { name: /^refresh$/i }))
    expect(await screen.findByText('Price changed')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())

    unmount()
    expect(clearInterval).toHaveBeenCalled()
    setInterval.mockRestore()
    clearInterval.mockRestore()
  })
})
