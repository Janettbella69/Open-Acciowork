import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import {
  confirmMaintenance, discardMaintenance, getMaintenanceStatus, prepareMaintenance,
  type MaintenanceOperation, type MaintenanceStatus,
} from '../api/maintenanceConsole'
import { I18nContext } from '../i18n'
import MaintenancePage from './MaintenancePage'

vi.mock('../api/maintenanceConsole', async (original) => ({
  ...await original<typeof import('../api/maintenanceConsole')>(),
  getMaintenanceStatus: vi.fn(), prepareMaintenance: vi.fn(), confirmMaintenance: vi.fn(), discardMaintenance: vi.fn(),
}))
const token = 'a'.repeat(43)
const reviewed: MaintenanceOperation = {
  request_id: '1'.repeat(32), kind: 'backup', backup_id: null, state: 'reviewed', fingerprint: 'b'.repeat(64),
  created_at: '2026-09-22T00:00:00Z', reviewed_at: '2026-09-22T00:01:00Z', destination: '/private/maintenance/backups/copy',
  summary: { files: 12, projects: 2, runs: 4 },
}
let status: MaintenanceStatus
beforeEach(() => {
  vi.resetAllMocks()
  status = { data_directory: '/private/data', plugin_directory: '/private/plugin', backup_directory: '/private/maintenance/backups', restore_directory: '/private/maintenance/restores', backups: [{ id: 'c'.repeat(64), name: 'Trusted backup' }], operations: [] }
  vi.mocked(getMaintenanceStatus).mockImplementation(async () => status)
  vi.mocked(prepareMaintenance).mockImplementation(async (_, body) => {
    const result = { ...reviewed, ...body, backup_id: body.backup_id ?? null }
    status = { ...status, operations: [result] }
    return result
  })
  vi.mocked(confirmMaintenance).mockImplementation(async (_, body) => {
    const result = { ...body, state: 'completed' as const }
    status = { ...status, operations: [result] }
    return result
  })
  vi.mocked(discardMaintenance).mockImplementation(async (_, body) => {
    const result = { ...body, state: 'discarded' as const }
    status = { ...status, operations: [result] }
    return result
  })
})

it('prepares a reviewed complete copy and requires acknowledgement before a single publication', async () => {
  render(<MaintenancePage initialToken={token} />)
  expect(prepareMaintenance).not.toHaveBeenCalled()
  fireEvent.click(await screen.findByRole('button', { name: 'Prepare backup review' }))
  const confirm = await screen.findByRole('button', { name: 'Confirm reviewed copy' })
  expect(confirm).toBeDisabled()
  expect(screen.getByText(/includes credentials, browser logins/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(confirm); fireEvent.click(confirm)
  expect(await screen.findByText('Copy published successfully')).toBeInTheDocument()
  expect(confirmMaintenance).toHaveBeenCalledTimes(1)
  expect(vi.mocked(confirmMaintenance).mock.calls[0][1].fingerprint).toBe(reviewed.fingerprint)
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
})

it('retains the preparation request after a lost response and exposes the exact restore source', async () => {
  vi.mocked(prepareMaintenance).mockRejectedValueOnce(new Error('Network disconnected'))
  render(<MaintenancePage initialToken={token} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Prepare restore review' }))
  expect(await screen.findByRole('alert')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Retry the same preparation' }))
  expect(await screen.findByRole('button', { name: 'Confirm reviewed copy' })).toBeDisabled()
  const calls = vi.mocked(prepareMaintenance).mock.calls
  expect(calls[0]).toEqual(calls[1])
  expect(calls[0][1]).toMatchObject({ kind: 'restore', backup_id: 'c'.repeat(64) })
  expect(screen.getByText('Source backup:', { exact: false })).toHaveTextContent('Trusted backup')
})

it('keeps the same review for a lost confirmation response and never calls it cancelled', async () => {
  status.operations = [reviewed]
  vi.mocked(confirmMaintenance).mockRejectedValueOnce(new Error('Network disconnected'))
  render(<MaintenancePage initialToken={token} />)
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm reviewed copy' }))
  expect(await screen.findByText(/Publication may already be committed/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Retry reviewed publication' }))
  expect(await screen.findByText('Copy published successfully')).toBeInTheDocument()
  expect(vi.mocked(confirmMaintenance).mock.calls).toEqual([[token, reviewed], [token, reviewed]])
})

it('resumes committed publication after a reload and does not offer discard', async () => {
  status.operations = [{ ...reviewed, state: 'publishing', kind: 'restore', backup_id: 'c'.repeat(64) }]
  render(<MaintenancePage initialToken={token} />)
  const button = await screen.findByRole('button', { name: 'Retry reviewed publication' })
  expect(button).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Discard uncommitted review' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('checkbox')); fireEvent.click(button)
  expect(await screen.findByText('Copy published successfully')).toBeInTheDocument()
  expect(screen.getByText('OPHIRA_DATA_DIR')).toBeInTheDocument()
  expect(screen.getByText(/Review restored schedules and external connections/)).toBeInTheDocument()
})

it('discards only a pending copy and returns to preparing another operation', async () => {
  status.operations = [reviewed]
  render(<MaintenancePage initialToken={token} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Discard uncommitted review' }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Prepare backup review' })).toBeEnabled())
  expect(discardMaintenance).toHaveBeenCalledExactlyOnceWith(token, reviewed)
  expect(confirmMaintenance).not.toHaveBeenCalled()
})

it('does not abort or refresh a mutation that finishes after unmount', async () => {
  status.operations = [reviewed]
  let finish!: (value: MaintenanceOperation) => void
  vi.mocked(confirmMaintenance).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
  const view = render(<MaintenancePage initialToken={token} />)
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm reviewed copy' }))
  view.unmount()
  await act(async () => { finish({ ...reviewed, state: 'completed' }) })
  expect(getMaintenanceStatus).toHaveBeenCalledTimes(1)
})

it('rejects mismatched publication responses and preserves the review', async () => {
  status.operations = [reviewed]
  vi.mocked(confirmMaintenance).mockResolvedValueOnce({ ...reviewed, request_id: '2'.repeat(32), state: 'completed' })
  render(<MaintenancePage initialToken={token} />)
  fireEvent.click(await screen.findByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm reviewed copy' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('The response did not match this reviewed operation.')
  expect(screen.queryByText('Copy published successfully')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Retry reviewed publication' })).toBeEnabled()
})

it('requires the private session key before reading data and supports Chinese recovery guidance', async () => {
  render(<I18nContext.Provider value="zh-CN"><MaintenancePage /></I18nContext.Provider>)
  expect(getMaintenanceStatus).not.toHaveBeenCalled()
  expect(screen.getByRole('heading', { name: '完整应用备份与恢复' })).toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('维护会话密钥'), { target: { value: token } })
  fireEvent.click(screen.getByRole('button', { name: '连接维护控制台' }))
  expect(await screen.findByRole('button', { name: '准备恢复核对' })).toBeEnabled()
  expect(getMaintenanceStatus).toHaveBeenCalledExactlyOnceWith(token)
})

it('clears private review state when the maintenance capability expires', async () => {
  status.operations = [reviewed]
  render(<MaintenancePage initialToken={token} />)
  fireEvent.click(await screen.findByRole('checkbox'))
  vi.mocked(getMaintenanceStatus).mockRejectedValueOnce(new Error('maintenance_access_denied'))
  fireEvent.click(screen.getByRole('button', { name: 'Refresh maintenance state' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('Restart the local console')
  expect(screen.queryByText('/private/data')).not.toBeInTheDocument()
  expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
})
