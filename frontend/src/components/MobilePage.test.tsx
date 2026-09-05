import { StrictMode } from 'react'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { OpportunityProject, PendingPermissionView, RunRecord } from '../api/types'
import type { CompanionOverview, PairingView } from '../api/companion'
import MobilePage from './MobilePage'

vi.mock('../api/client', () => ({
  apiBaseUrl: '',
  listProjects: vi.fn(async () => []),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))

vi.mock('../api/companion', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api/companion')>(),
  getMobileStatus: vi.fn(), listMobilePairings: vi.fn(), createMobilePairing: vi.fn(),
  decideMobilePairing: vi.fn(), revokeMobilePairing: vi.fn(), getCompanionSession: vi.fn(),
  getCompanionOverview: vi.fn(), pairCompanion: vi.fn(), resolveCompanionPermission: vi.fn(),
  startCompanionRun: vi.fn(), cancelCompanionRun: vi.fn(), logoutCompanion: vi.fn(),
}))

import { listProjects } from '../api/client'
import {
  cancelCompanionRun, companionModeStorageKey, createMobilePairing, decideMobilePairing,
  getCompanionOverview, getCompanionSession, getMobileStatus, listMobilePairings, logoutCompanion,
  pairCompanion, resolveCompanionPermission, revokeMobilePairing, startCompanionRun,
} from '../api/companion'

const project = { id: 'project_1', name: 'Market launch', objective: 'Validate Japan demand' } as OpportunityProject
const device: PairingView = {
  id: 'pair_1', status: 'approved', project_ids: [project.id], allow_run_start: true,
  device_name: 'My phone', verification_code: null, created_at: '2026-09-05T00:00:00Z',
  expires_at: '2026-10-05T00:00:00Z', approved_at: '2026-09-05T00:02:00Z',
}
const pendingDevice: PairingView = { ...device, status: 'pending', verification_code: '123456', approved_at: null }
const questionPermission: PendingPermissionView = {
  id: 'perm_question', run_id: 'run_1', project_id: project.id, tool_name: 'AskUserQuestion',
  tool_input: { questions: [{ question: 'Which market?', options: [{ label: 'Japan' }, { label: 'Germany' }], multiSelect: false }] },
  agent_id: null, timeout_seconds: 120, created_at: '2026-09-05T00:00:00Z', destructive: false, persistable: false, scope: '',
}
const destructivePermission: PendingPermissionView = {
  ...questionPermission, id: 'perm_delete', tool_name: 'Bash', tool_input: { command: 'rm report.csv', token: '[REDACTED]' },
  destructive: true, scope: 'path:/workspace/report.csv',
}
const run = { id: 'run_1', project_id: project.id, prompt: 'Review the market', status: 'running', error: null } as RunRecord
const overview: CompanionOverview = { device, projects: [project], pending_permissions: [], notifications: [], runs: [] }
let intervals: Array<() => void>

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
  const storage = new Map<string, string>()
  Object.defineProperty(window, 'localStorage', { configurable: true, value: {
    get length() { return storage.size },
    getItem: (key: string) => storage.get(key) ?? null,
    setItem: (key: string, value: string) => { storage.set(key, value) },
    removeItem: (key: string) => { storage.delete(key) },
    clear: () => storage.clear(),
  } })
  window.history.replaceState(null, '', '/app/mobile')
  intervals = []
  vi.spyOn(window, 'setInterval').mockImplementation((handler) => {
    if (typeof handler === 'function') intervals.push(handler as () => void)
    return intervals.length as unknown as ReturnType<typeof window.setInterval>
  })
  vi.mocked(listProjects).mockResolvedValue([project])
  vi.mocked(getCompanionSession).mockResolvedValue({ status: 'unpaired', device: null })
  vi.mocked(getMobileStatus).mockResolvedValue({ enabled: true, public_origin: 'https://trade.example', pairing_ttl_seconds: 600, device_max_age_seconds: 2592000 })
  vi.mocked(listMobilePairings).mockResolvedValue([])
  vi.mocked(getCompanionOverview).mockResolvedValue(overview)
  vi.mocked(pairCompanion).mockResolvedValue({ status: 'pending', device: pendingDevice })
  vi.mocked(resolveCompanionPermission).mockResolvedValue(undefined)
  vi.mocked(startCompanionRun).mockResolvedValue(run)
  vi.mocked(cancelCompanionRun).mockResolvedValue({ ...run, status: 'cancelling' })
  vi.mocked(logoutCompanion).mockResolvedValue({})
})

function remote(nextOverview = overview) {
  vi.mocked(getCompanionSession).mockResolvedValue({ status: 'approved', device: nextOverview.device })
  vi.mocked(getCompanionOverview).mockResolvedValue(nextOverview)
}

describe('desktop pairing management', () => {
  it.each([403, 503])('uses the normal authenticated status probe when local companion detection fails with %s', async (status) => {
    vi.mocked(getCompanionSession).mockRejectedValue(new Error(`Request failed (${status})`))
    vi.mocked(getMobileStatus).mockResolvedValue({ enabled: false, public_origin: null, pairing_ttl_seconds: 600, device_max_age_seconds: 2592000 })
    render(<MobilePage />)
    expect(await screen.findByText('Pairing service is disabled')).toBeInTheDocument()
    expect(getMobileStatus).toHaveBeenCalled()
    expect(getCompanionOverview).not.toHaveBeenCalled()
    expect(listProjects).not.toHaveBeenCalled()
  })

  it('remains closed if neither the companion session nor owner authentication accepts the browser', async () => {
    vi.mocked(getCompanionSession).mockRejectedValue(new Error('Companion requires HTTPS'))
    vi.mocked(getMobileStatus).mockRejectedValue(new Error('Desktop authentication required'))
    render(<MobilePage />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Companion requires HTTPS')
    expect(screen.queryByRole('heading', { name: 'Secure pairing' })).not.toBeInTheDocument()
    expect(listProjects).not.toHaveBeenCalled()
    expect(listMobilePairings).not.toHaveBeenCalled()
  })

  it('shows disabled backend truth and never presents local preferences as OS controls', async () => {
    vi.mocked(getMobileStatus).mockResolvedValue({ enabled: false, public_origin: null, pairing_ttl_seconds: 600, device_max_age_seconds: 2592000 })
    render(<MobilePage />)
    expect(await screen.findByText('Pairing service is disabled')).toBeInTheDocument()
    expect(screen.queryByRole('switch')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create pairing link' })).not.toBeInTheDocument()
    expect(listProjects).not.toHaveBeenCalled()
    expect(listMobilePairings).not.toHaveBeenCalled()
    expect(screen.getByText(/background push notifications and os sleep prevention are not enabled/i)).toBeInTheDocument()
  })

  it('creates a one-time link only for explicitly selected projects and capabilities', async () => {
    const offered = { ...device, status: 'offered' as const, device_name: null, approved_at: null }
    const url = 'https://trade.example/app/mobile#pairing=pair_1&secret=one_time_secret_123'
    vi.mocked(createMobilePairing).mockResolvedValue({ pairing: offered, pairing_url: url })
    render(<MobilePage />)
    const create = await screen.findByRole('button', { name: 'Create pairing link' })
    expect(create).toBeDisabled()
    fireEvent.click(await screen.findByRole('checkbox', { name: 'Market launch' }))
    fireEvent.click(create)
    await waitFor(() => expect(createMobilePairing).toHaveBeenCalledWith({ project_ids: [project.id], allow_run_start: false }))
    expect(await screen.findByRole('textbox', { name: 'Pairing link' })).toHaveValue(url)
    expect(window.localStorage.length).toBe(0)
    expect(decideMobilePairing).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Hide link' }))
    expect(screen.queryByRole('textbox', { name: 'Pairing link' })).not.toBeInTheDocument()
  })

  it('requires matching phone code before approval and can revoke the trusted device', async () => {
    vi.mocked(listMobilePairings).mockResolvedValue([pendingDevice])
    vi.mocked(decideMobilePairing).mockResolvedValue(device)
    vi.mocked(revokeMobilePairing).mockResolvedValue({ ...device, status: 'revoked' })
    render(<MobilePage />)
    const approve = await screen.findByRole('button', { name: 'Approve device' })
    expect(approve).toBeDisabled()
    expect(screen.getByLabelText('Desktop verification code')).toHaveTextContent('123456')
    const code = screen.getByRole('textbox', { name: 'Phone verification code' })
    fireEvent.change(code, { target: { value: '111111' } })
    expect(approve).toBeDisabled()
    fireEvent.change(code, { target: { value: '123456' } })
    fireEvent.click(approve)
    fireEvent.click(approve)
    await waitFor(() => expect(decideMobilePairing).toHaveBeenCalledWith('pair_1', 'approve', '123456'))
    expect(decideMobilePairing).toHaveBeenCalledTimes(1)
    fireEvent.click(await screen.findByRole('button', { name: 'Revoke device' }))
    await waitFor(() => expect(revokeMobilePairing).toHaveBeenCalledWith('pair_1'))
    expect(await screen.findByText('revoked')).toBeInTheDocument()
  })

  it('rejects an unfamiliar device without approving or supplying a code', async () => {
    vi.mocked(listMobilePairings).mockResolvedValue([pendingDevice])
    vi.mocked(decideMobilePairing).mockResolvedValue({ ...pendingDevice, status: 'rejected' })
    render(<MobilePage />)
    fireEvent.click(await screen.findByRole('button', { name: 'Reject pairing' }))
    await waitFor(() => expect(decideMobilePairing).toHaveBeenCalledWith('pair_1', 'reject', undefined))
    expect(await screen.findByText('rejected')).toBeInTheDocument()
  })
})

describe('trusted companion', () => {
  it.each([403, 503])('never probes owner APIs for a remembered companion whose session fails with %s', async (status) => {
    window.localStorage.setItem(companionModeStorageKey, 'true')
    vi.mocked(getCompanionSession).mockRejectedValue(new Error(`Request failed (${status})`))
    render(<MobilePage />)
    expect(await screen.findByRole('alert')).toHaveTextContent(`Request failed (${status})`)
    expect(getMobileStatus).not.toHaveBeenCalled()
    expect(listProjects).not.toHaveBeenCalled()
    expect(listMobilePairings).not.toHaveBeenCalled()
    expect(getCompanionOverview).not.toHaveBeenCalled()
  })

  it('consumes and clears the fragment before any request and waits for explicit desktop approval', async () => {
    window.history.replaceState(null, '', '/app/mobile#pairing=pair_1&secret=one_time_secret_123')
    vi.mocked(getCompanionSession).mockResolvedValue({ status: 'pending', device: pendingDevice })
    render(<StrictMode><MobilePage /></StrictMode>)
    expect(window.location.hash).toBe('')
    expect(getMobileStatus).not.toHaveBeenCalled()
    expect(getCompanionOverview).not.toHaveBeenCalled()
    expect(pairCompanion).not.toHaveBeenCalled()
    fireEvent.change(await screen.findByRole('textbox', { name: 'Device name' }), { target: { value: 'My phone' } })
    fireEvent.click(screen.getByRole('button', { name: 'Request pairing' }))
    await waitFor(() => expect(pairCompanion).toHaveBeenCalledWith({ pairing_id: 'pair_1', secret: 'one_time_secret_123' }, 'My phone'))
    expect(pairCompanion).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('Waiting for desktop approval')).toBeInTheDocument()
    expect(screen.getByLabelText('Phone verification code')).toHaveTextContent('123456')
    expect(listProjects).not.toHaveBeenCalled()
    expect(getCompanionOverview).not.toHaveBeenCalled()
    expect(decideMobilePairing).not.toHaveBeenCalled()
    expect(window.localStorage.length).toBe(1)
    expect(window.localStorage.getItem(companionModeStorageKey)).toBe('true')
  })

  it('never falls back to global APIs for an expired or unpaired remote browser', async () => {
    window.localStorage.setItem(companionModeStorageKey, 'true')
    render(<MobilePage />)
    expect(await screen.findByText(/this device is not authorized/i)).toBeInTheDocument()
    expect(getMobileStatus).not.toHaveBeenCalled()
    expect(listProjects).not.toHaveBeenCalled()
    expect(getCompanionOverview).not.toHaveBeenCalled()
  })

  it('explains a rejected pairing without loading project data or falling back to desktop access', async () => {
    vi.mocked(getCompanionSession).mockResolvedValue({ status: 'rejected', device: { ...pendingDevice, status: 'rejected' } })
    render(<MobilePage />)
    expect(await screen.findByText(/the desktop rejected this pairing request/i)).toBeInTheDocument()
    expect(getCompanionOverview).not.toHaveBeenCalled()
    expect(getMobileStatus).not.toHaveBeenCalled()
    expect(listProjects).not.toHaveBeenCalled()
  })

  it('requires structured answers and an explicit destructive confirmation; grants remain single-use', async () => {
    remote({ ...overview, pending_permissions: [questionPermission, destructivePermission] })
    render(<MobilePage />)
    const submit = await screen.findByRole('button', { name: 'Submit answers' })
    expect(submit).toBeDisabled()
    expect(screen.queryByRole('button', { name: /allow for run|allow persistent/i })).not.toBeInTheDocument()
    expect(screen.getByText('No reusable scope')).toBeInTheDocument()
    expect(screen.getByText('path:/workspace/report.csv')).toBeInTheDocument()
    expect(screen.getByText(/\[REDACTED\]/)).toBeInTheDocument()
    expect(screen.getAllByText('2 minutes')).toHaveLength(2)
    fireEvent.click(screen.getByRole('button', { name: 'Japan' }))
    fireEvent.click(submit)
    fireEvent.click(submit)
    await waitFor(() => expect(resolveCompanionPermission).toHaveBeenCalledWith('run_1', 'perm_question', 'allow_once', { 'Which market?': 'Japan' }))
    expect(resolveCompanionPermission).toHaveBeenCalledTimes(1)
    const allow = screen.getByRole('button', { name: 'Allow once' })
    expect(allow).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: /confirm this destructive action/i }))
    fireEvent.click(allow)
    await waitFor(() => expect(resolveCompanionPermission).toHaveBeenCalledWith('run_1', 'perm_delete', 'allow_once', undefined))
    expect(listProjects).not.toHaveBeenCalled()
    expect(getMobileStatus).not.toHaveBeenCalled()
  })

  it('starts and cancels authorized runs, using only the companion control plane', async () => {
    remote()
    render(<MobilePage />)
    fireEvent.change(await screen.findByRole('textbox', { name: 'Prompt' }), { target: { value: 'Review the market' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
    fireEvent.click(screen.getByRole('button', { name: 'Starting…' }))
    await waitFor(() => expect(startCompanionRun).toHaveBeenCalledWith(project.id, 'Review the market'))
    expect(startCompanionRun).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('running')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel run' }))
    await waitFor(() => expect(cancelCompanionRun).toHaveBeenCalledWith(run.id))
    expect(await screen.findByText('cancelling')).toBeInTheDocument()
  })

  it('does not offer run creation when the device was approved without that scope', async () => {
    remote({ ...overview, device: { ...device, allow_run_start: false } })
    render(<MobilePage />)
    expect(await screen.findByText('The desktop owner has not allowed this device to start runs.')).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Prompt' })).not.toBeInTheDocument()
    expect(startCompanionRun).not.toHaveBeenCalled()
  })

  it('polls pending approval, then loads scoped projects only after server approval', async () => {
    window.localStorage.setItem(companionModeStorageKey, 'true')
    vi.mocked(getCompanionSession).mockResolvedValue({ status: 'pending', device: pendingDevice })
    const clearInterval = vi.spyOn(window, 'clearInterval')
    const { unmount } = render(<MobilePage />)
    expect(await screen.findByText('Waiting for desktop approval')).toBeInTheDocument()
    expect(getCompanionOverview).not.toHaveBeenCalled()
    remote()
    await act(async () => { intervals.at(-1)?.() })
    expect(await screen.findByRole('heading', { name: 'Authorized projects' })).toBeInTheDocument()
    expect(getCompanionOverview).toHaveBeenCalledTimes(1)
    expect(listProjects).not.toHaveBeenCalled()
    unmount()
    expect(clearInterval).toHaveBeenCalled()
  })

  it('discards a poll that began before a permission decision, without resurrecting the request', async () => {
    const safePermission = { ...destructivePermission, destructive: false }
    remote({ ...overview, pending_permissions: [safePermission] })
    render(<MobilePage />)
    await screen.findByRole('button', { name: 'Allow once' })
    const stale = deferred<CompanionOverview>()
    vi.mocked(getCompanionOverview).mockReturnValueOnce(stale.promise)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Refresh' })) })
    expect(getCompanionOverview).toHaveBeenCalledTimes(2)
    fireEvent.click(screen.getByRole('button', { name: 'Allow once' }))
    await screen.findByText('Decision sent')
    await act(async () => { stale.resolve({ ...overview, pending_permissions: [safePermission] }) })
    expect(screen.queryByRole('button', { name: 'Allow once' })).not.toBeInTheDocument()
    expect(screen.getByText('No pending permissions.')).toBeInTheDocument()
  })

  it('clears project controls immediately when session polling reports revocation', async () => {
    remote()
    render(<MobilePage />)
    await screen.findByRole('textbox', { name: 'Prompt' })
    vi.mocked(getCompanionSession).mockResolvedValue({ status: 'revoked', device: { ...device, status: 'revoked' } })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText(/this device is not authorized/i)).toBeInTheDocument()
    expect(screen.queryByRole('textbox', { name: 'Prompt' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Authorized projects' })).not.toBeInTheDocument()
  })

  it('disconnects on the server and retains only the untrusted remote-mode flag', async () => {
    remote()
    render(<MobilePage />)
    await screen.findByRole('textbox', { name: 'Prompt' })
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect this device' }))
    await waitFor(() => expect(logoutCompanion).toHaveBeenCalledTimes(1))
    expect(await screen.findByText(/this device is not authorized/i)).toBeInTheDocument()
    expect(window.localStorage.getItem(companionModeStorageKey)).toBe('true')
    expect(getMobileStatus).not.toHaveBeenCalled()
  })

  it('fails closed when overview authorization changes after the session check', async () => {
    remote()
    vi.mocked(getCompanionOverview).mockRejectedValue(new Error('Device revoked'))
    render(<MobilePage />)
    expect(await screen.findByRole('alert')).toHaveTextContent('Device revoked')
    expect(screen.queryByRole('textbox', { name: 'Prompt' })).not.toBeInTheDocument()
    expect(listProjects).not.toHaveBeenCalled()
  })
})
