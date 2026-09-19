import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import MemoryReview from './MemoryReview'
import type { GovernedMemory, MemoryExport, MemoryTombstone } from '../types/MemoryGovernance'
import { I18nContext } from '../i18n'

vi.mock('../api/client', () => ({
  listMemory: vi.fn(), correctMemory: vi.fn(), decideMemory: vi.fn(),
  deleteMemory: vi.fn(), exportMemory: vi.fn(), updateMemoryExpiry: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'Unknown error',
}))
import { correctMemory, decideMemory, deleteMemory, exportMemory, listMemory, updateMemoryExpiry } from '../api/client'

const item: GovernedMemory = {
  id: 'mem_private', scope: 'project', scope_id: 'project_private', text: 'Private supplier preference',
  kind: 'preference', sensitivity: 'sensitive', status: 'proposed', version: 1,
  created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', expires_at: null,
}
const tombstone: MemoryTombstone = {
  schema_version: 1, memory_id: item.id, scope: item.scope, scope_id: item.scope_id,
  last_version: 1, last_status: 'proposed', content_sha256: 'a'.repeat(64),
  deleted_by: 'owner', reason: 'No longer needed', deleted_at: '2026-09-01T00:00:00Z',
}
const exported: MemoryExport = {
  schema_version: 'memory-export-v1', exported_at: '2026-09-01T00:00:00Z', exported_by: 'owner',
  count: 1, items: [item], tombstone_count: 1, tombstones: [tombstone],
}
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}
const denied = (status: number) => Object.assign(new Error('Private backend detail'), { status })
const article = () => screen.getByText(item.text).closest('article')!
async function loaded() { await screen.findByText(item.text) }
function deleteForm() {
  fireEvent.click(within(article()).getByRole('button', { name: /^delete$/i }))
  return screen.getByRole('form', { name: 'Delete memory' })
}
function fillReason(form: HTMLElement, value = 'No longer needed') {
  fireEvent.change(within(form).getByLabelText('Deletion reason'), { target: { value } })
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listMemory).mockResolvedValue([item])
  vi.mocked(exportMemory).mockResolvedValue(exported)
  vi.mocked(deleteMemory).mockResolvedValue(tombstone)
  vi.mocked(decideMemory).mockImplementation(async (_id, decision) => ({ ...item, status: decision === 'disable' ? 'disabled' : decision === 'approve' ? 'approved' : 'rejected' }))
  vi.mocked(correctMemory).mockImplementation(async (_id, input) => ({ ...item, ...input, status: 'approved' }))
  vi.mocked(updateMemoryExpiry).mockResolvedValue({ ...item, expires_at: '2099-01-01T00:00:00Z', expired: false })
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:memory') })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
})

describe('memory governance', () => {
  it('exports the complete authorized response and reports a browser handoff', async () => {
    render(<MemoryReview />)
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Export memories (JSON)' }))
    await screen.findByText(/Check your downloads to confirm it was saved/)
    expect(exportMemory).toHaveBeenCalledOnce()
    expect(exportMemory).toHaveBeenCalledWith(expect.any(AbortSignal))
    const blob = vi.mocked(URL.createObjectURL).mock.calls[0][0] as Blob
    const content = await new Promise<string>((resolve) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.readAsText(blob)
    })
    expect(JSON.parse(content)).toEqual(exported)
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledOnce()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
  })

  it.each(['cancel', 'unmount'])('does not download a late export after %s', async (operation) => {
    const pending = deferred<MemoryExport>()
    vi.mocked(exportMemory).mockReturnValueOnce(pending.promise)
    const view = render(<MemoryReview />)
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Export memories (JSON)' }))
    const signal = vi.mocked(exportMemory).mock.calls[0][0]!
    if (operation === 'cancel') fireEvent.click(screen.getByRole('button', { name: 'Cancel export' }))
    else view.unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => { pending.resolve(exported) })
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })

  it('keeps a newer export busy when a cancelled request resolves late', async () => {
    const first = deferred<MemoryExport>(), second = deferred<MemoryExport>()
    vi.mocked(exportMemory).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    render(<MemoryReview />)
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Export memories (JSON)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Cancel export' }))
    fireEvent.click(screen.getByRole('button', { name: 'Export memories (JSON)' }))
    await act(async () => { first.resolve(exported) })
    expect(screen.getByRole('button', { name: 'Exporting…' })).toBeDisabled()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
    await act(async () => { second.resolve(exported) })
    expect(URL.createObjectURL).toHaveBeenCalledOnce()
  })

  it('requires confirmation with a nonempty reason, sends one delete and removes content', async () => {
    const pending = deferred<MemoryTombstone>()
    vi.mocked(deleteMemory).mockReturnValueOnce(pending.promise)
    render(<MemoryReview />)
    await loaded()
    const form = deleteForm()
    expect(deleteMemory).not.toHaveBeenCalled()
    expect(within(form).getByText(/Backups and downloaded copies/)).toBeInTheDocument()
    fillReason(form, '   ')
    expect(within(form).getByRole('button', { name: 'Permanently delete' })).toBeDisabled()
    fireEvent.submit(form)
    expect(deleteMemory).not.toHaveBeenCalled()
    fillReason(form, '  No longer needed  ')
    fireEvent.submit(form)
    fireEvent.submit(form)
    expect(deleteMemory).toHaveBeenCalledExactlyOnceWith(item.id, 'No longer needed')
    await act(async () => { pending.resolve(tombstone) })
    expect(screen.queryByText(item.text)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Deletion reason')).not.toBeInTheDocument()
  })

  it('cancels deletion review without issuing a delete', async () => {
    render(<MemoryReview />)
    await loaded()
    const form = deleteForm()
    fillReason(form)
    fireEvent.click(within(form).getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByRole('form', { name: 'Delete memory' })).not.toBeInTheDocument()
    expect(deleteMemory).not.toHaveBeenCalled()
  })

  it.each([403, 404])('clears the private item and draft on mutation %s', async (status) => {
    vi.mocked(correctMemory).mockRejectedValueOnce(denied(status))
    render(<MemoryReview />)
    await loaded()
    fireEvent.click(within(article()).getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Memory text'), { target: { value: 'Private edited draft' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByText(/Its local content has been cleared/)
    expect(screen.queryByText(item.text)).not.toBeInTheDocument()
    expect(screen.queryByDisplayValue('Private edited draft')).not.toBeInTheDocument()
    expect(screen.queryByText('Private backend detail')).not.toBeInTheDocument()
  })

  it('keeps a failed deletion and its reason available for retry on 5xx', async () => {
    vi.mocked(deleteMemory).mockRejectedValueOnce(Object.assign(new Error('Temporarily unavailable'), { status: 503 }))
    render(<MemoryReview />)
    await loaded()
    const form = deleteForm()
    fillReason(form)
    fireEvent.submit(form)
    await screen.findByText('Temporarily unavailable')
    expect(screen.getByDisplayValue('No longer needed')).toBeInTheDocument()
    expect(screen.getByText(item.text)).toBeInTheDocument()
    fireEvent.submit(form)
    await waitFor(() => expect(screen.queryByText(item.text)).not.toBeInTheDocument())
    expect(deleteMemory).toHaveBeenCalledTimes(2)
  })

  it.each(['1', '7', '30', '90', 'permanent'])('renews expired memory using the explicit %s duration', async (duration) => {
    vi.mocked(listMemory).mockResolvedValueOnce([{ ...item, expires_at: '2020-01-01T00:00:00Z', expired: true }])
    render(<MemoryReview />)
    await loaded()
    expect(within(article()).getByRole('button', { name: 'Approve' })).toBeDisabled()
    expect(within(article()).getByRole('button', { name: 'Edit' })).toBeDisabled()
    fireEvent.click(within(article()).getByRole('button', { name: 'Renew' }))
    fireEvent.change(screen.getByLabelText('Keep memory for'), { target: { value: duration } })
    fireEvent.click(screen.getByRole('button', { name: 'Save expiry' }))
    await waitFor(() => expect(updateMemoryExpiry).toHaveBeenCalledWith(item.id, duration === 'permanent' ? { clear_expiry: true } : { ttl_seconds: Number(duration) * 86400 }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Approve' })).toBeEnabled())
    expect(decideMemory).not.toHaveBeenCalled()
  })

  it('respects a server expiry decision even when the client clock is behind', async () => {
    vi.mocked(listMemory).mockResolvedValueOnce([{ ...item, expires_at: '2099-01-01T00:00:00Z', expired: true }])
    render(<MemoryReview />)
    await loaded()
    expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Renew' })).toBeEnabled()
  })

  it('disables an approved memory without deleting its content', async () => {
    vi.mocked(listMemory).mockResolvedValueOnce([{ ...item, status: 'approved' }])
    render(<MemoryReview />)
    await loaded()
    fireEvent.click(screen.getByText('Reviewed memories (1)'))
    fireEvent.click(within(article()).getByRole('button', { name: 'Disable' }))
    await waitFor(() => expect(decideMemory).toHaveBeenCalledWith(item.id, 'disable'))
    expect(await screen.findByText('Disabled ·')).toBeInTheDocument()
    expect(screen.getByText(item.text)).toBeInTheDocument()
    expect(deleteMemory).not.toHaveBeenCalled()
  })

  it('clears all visible content and drafts when export access is denied', async () => {
    vi.mocked(exportMemory).mockRejectedValueOnce(denied(403))
    render(<MemoryReview />)
    await loaded()
    fireEvent.click(within(article()).getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Memory text'), { target: { value: 'Private unsaved copy' } })
    fireEvent.click(screen.getByRole('button', { name: 'Export memories (JSON)' }))
    await screen.findByText(/Your memory access has changed/)
    expect(screen.queryByText(item.text)).not.toBeInTheDocument()
    expect(screen.queryByDisplayValue('Private unsaved copy')).not.toBeInTheDocument()
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })

  it('blocks export while initial list data is pending', async () => {
    const pending = deferred<GovernedMemory[]>()
    vi.mocked(listMemory).mockReturnValueOnce(pending.promise)
    render(<MemoryReview />)
    expect(screen.getByRole('button', { name: 'Export memories (JSON)' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Export memories (JSON)' }))
    expect(exportMemory).not.toHaveBeenCalled()
    await act(async () => { pending.resolve([item]) })
    expect(screen.getByRole('button', { name: 'Export memories (JSON)' })).toBeEnabled()
  })

  it('blocks mutations during a refresh so stale list data cannot restore a deletion', async () => {
    const pending = deferred<GovernedMemory[]>()
    vi.mocked(listMemory).mockResolvedValueOnce([item]).mockReturnValueOnce(pending.promise)
    vi.mocked(deleteMemory).mockRejectedValueOnce(new Error('Retry later'))
    render(<MemoryReview />)
    await loaded()
    const form = deleteForm()
    fillReason(form)
    fireEvent.submit(form)
    await screen.findByText('Retry later')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh memories' }))
    expect(within(article()).getByRole('button', { name: 'Delete' })).toBeDisabled()
    await act(async () => { pending.resolve([item]) })
    const refreshedForm = deleteForm()
    fillReason(refreshedForm)
    fireEvent.submit(refreshedForm)
    await waitFor(() => expect(screen.queryByText(item.text)).not.toBeInTheDocument())
  })

  it('does not apply a previous mount mutation to a new view', async () => {
    const pending = deferred<GovernedMemory>()
    vi.mocked(correctMemory).mockReturnValueOnce(pending.promise)
    const view = render(<MemoryReview />)
    await loaded()
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    view.unmount()
    vi.mocked(listMemory).mockResolvedValueOnce([])
    render(<MemoryReview />)
    await act(async () => { pending.resolve({ ...item, text: 'Stale private response' }) })
    expect(screen.queryByText('Stale private response')).not.toBeInTheDocument()
    expect(screen.queryByText(item.text)).not.toBeInTheDocument()
  })

  it('localizes governance confirmation and duration choices in Chinese', async () => {
    render(<I18nContext.Provider value="zh-CN"><MemoryReview /></I18nContext.Provider>)
    await loaded()
    expect(screen.getByRole('button', { name: '导出记忆（JSON）' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '删除' }))
    expect(screen.getByLabelText('删除原因')).toBeInTheDocument()
    expect(screen.getByText(/备份和已下载的副本需要另行管理/)).toBeInTheDocument()
  })
})
