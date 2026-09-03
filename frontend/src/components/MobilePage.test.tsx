import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { OpportunityProject, PendingPermissionView } from '../api/types'
import { localSettingsStorageKey } from '../localSettings'
import MobilePage from './MobilePage'

vi.mock('../api/client', () => ({
  listPendingPermissions: vi.fn(async () => []),
  listProjects: vi.fn(async () => []),
  resolvePermission: vi.fn(),
  startRun: vi.fn(),
  errorMessage: (error: unknown) => (error instanceof Error ? error.message : 'unknown error'),
}))

import { listPendingPermissions, listProjects, resolvePermission } from '../api/client'

let localStore: Map<string, string>

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listPendingPermissions).mockResolvedValue([])
  vi.mocked(listProjects).mockResolvedValue([])
  vi.mocked(resolvePermission).mockResolvedValue(undefined)
  localStore = new Map<string, string>()
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => localStore.get(key) ?? null,
      setItem: (key: string, value: string) => void localStore.set(key, value),
      removeItem: (key: string) => void localStore.delete(key),
      clear: () => localStore.clear(),
    },
  })
})

const questionPermission: PendingPermissionView = {
  id: 'perm_question',
  run_id: 'run_1',
  project_id: 'project_1',
  tool_name: 'AskUserQuestion',
  tool_input: {
    questions: [{
      question: 'Which market?',
      options: [{ label: 'Japan' }, { label: 'Germany' }],
      multiSelect: false,
    }],
  },
  agent_id: null,
  timeout_seconds: 120,
  created_at: '2026-09-02T00:00:00Z',
  destructive: false,
  persistable: false,
  scope: '',
}

const destructivePermission: PendingPermissionView = {
  ...questionPermission,
  id: 'perm_delete',
  tool_name: 'Bash',
  tool_input: { command: 'rm report.csv', token: '[REDACTED]' },
  destructive: true,
  persistable: false,
  scope: 'path:/workspace/report.csv',
}

const project = {
  id: 'project_1',
  name: 'Market launch',
} as OpportunityProject

describe('MobilePage', () => {
  it('persists companion-device preferences and shows the approval surface', async () => {
    render(<MobilePage />)

    expect(await screen.findByRole('heading', { name: /mobile approval/i })).toBeInTheDocument()
    expect(screen.getByText(/no pending permissions/i)).toBeInTheDocument()
    const discoverable = screen.getByRole('switch', { name: /allow this computer to be discovered/i })
    expect(discoverable).not.toBeChecked()

    fireEvent.click(discoverable)
    await waitFor(() => expect(JSON.parse(localStore.get(localSettingsStorageKey) ?? '{}')).toMatchObject({
      computerDiscoverable: true,
    }))
    await waitFor(() => expect(listPendingPermissions).toHaveBeenCalled())
  })

  it('requires structured answers for AskUserQuestion and never exposes grant choices', async () => {
    localStore.set(localSettingsStorageKey, JSON.stringify({ computerDiscoverable: true }))
    vi.mocked(listPendingPermissions).mockResolvedValue([questionPermission, destructivePermission])
    render(<MobilePage />)

    const submit = await screen.findByRole('button', { name: /submit answers/i })
    expect(submit).toBeDisabled()
    expect(screen.queryByRole('button', { name: /allow for run/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /allow persistent/i })).not.toBeInTheDocument()
    expect(screen.getByText('No reusable scope')).toBeInTheDocument()
    expect(screen.getByText('path:/workspace/report.csv')).toBeInTheDocument()
    expect(screen.getByText(/\[REDACTED\]/)).toBeInTheDocument()
    expect(screen.getAllByText('2 minutes')).toHaveLength(2)

    fireEvent.click(screen.getByRole('button', { name: 'Japan' }))
    fireEvent.click(submit)
    fireEvent.click(submit)
    await waitFor(() => expect(resolvePermission).toHaveBeenCalledWith(
      'run_1',
      'perm_question',
      'allow_once',
      { 'Which market?': 'Japan' },
    ))
    expect(resolvePermission).toHaveBeenCalledTimes(1)
  })

  it('keeps project data when the independent permission refresh fails and cleans up polling', async () => {
    localStore.set(localSettingsStorageKey, JSON.stringify({ computerDiscoverable: true }))
    vi.mocked(listPendingPermissions).mockRejectedValue(new Error('Approvals unavailable'))
    vi.mocked(listProjects).mockResolvedValue([project])
    const clearInterval = vi.spyOn(window, 'clearInterval')
    const { unmount } = render(<MobilePage />)

    expect(await screen.findByRole('alert')).toHaveTextContent('Approvals unavailable')
    expect(screen.getByRole('option', { name: 'Market launch' })).toBeInTheDocument()
    vi.mocked(listPendingPermissions).mockResolvedValueOnce([])
    fireEvent.click(screen.getByRole('button', { name: /^refresh$/i }))
    await waitFor(() => expect(listPendingPermissions).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument())
    unmount()
    expect(clearInterval).toHaveBeenCalled()
    clearInterval.mockRestore()
  })
})
