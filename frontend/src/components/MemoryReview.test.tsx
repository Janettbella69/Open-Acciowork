import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { correctMemory, decideMemory, listMemory } from '../api/client'
import type { MemoryItem } from '../api/types'
import { I18nContext } from '../i18n'
import MemoryReview from './MemoryReview'
import RunMemoryPanel from './RunMemoryPanel'

vi.mock('../api/client', () => ({
  listMemory: vi.fn(), decideMemory: vi.fn(), correctMemory: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : String(error),
}))

const proposed: MemoryItem = {
  id: 'mem_1', scope: 'project', scope_id: 'project_a', source_run_id: 'run_a',
  text: 'Owner prefers EUR', kind: 'preference', sensitivity: 'normal',
  status: 'proposed', version: 1, created_at: '2026-09-22T00:00:00Z', updated_at: '2026-09-22T00:00:00Z',
}
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((complete) => { resolve = complete })
  return { promise, resolve }
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(listMemory).mockResolvedValue([proposed])
})

describe('Run memory review', () => {
  it('loads only when opened, scopes to the Run, and refreshes on observed proposals', async () => {
    const view = render(<RunMemoryPanel runId="run_a" revision={0} />)
    expect(listMemory).not.toHaveBeenCalled()
    fireEvent.click(screen.getByText('Run memories'))
    await screen.findByText(proposed.text)
    expect(listMemory).toHaveBeenCalledWith('run_a', expect.any(AbortSignal))
    expect(decideMemory).not.toHaveBeenCalled()
    const second = { ...proposed, id: 'mem_2', text: 'Fresh ledger proposal' }
    vi.mocked(listMemory).mockResolvedValue([proposed, second])
    view.rerender(<RunMemoryPanel runId="run_a" revision={4} />)
    await screen.findByText(second.text)
  })

  it('rejects response scope mismatches and distinguishes failures from an empty list', async () => {
    vi.mocked(listMemory).mockResolvedValue([{ ...proposed, source_run_id: 'run_foreign' }])
    render(<MemoryReview sourceRunId="run_a" />)
    await screen.findByText('Memory response did not match this run.')
    expect(screen.queryByText(proposed.text)).not.toBeInTheDocument()
    expect(screen.queryByText(/No memory proposals are waiting/)).not.toBeInTheDocument()
    vi.mocked(listMemory).mockRejectedValue(new Error('Access revoked'))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await screen.findByText('Access revoked')
    vi.mocked(listMemory).mockResolvedValue([])
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await screen.findByText(/No memory proposals are waiting/)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('ignores slow reads from an old Run when selection changes', async () => {
    const old = deferred<MemoryItem[]>()
    vi.mocked(listMemory).mockReturnValueOnce(old.promise)
    const view = render(<MemoryReview sourceRunId="run_a" />)
    const next = { ...proposed, id: 'mem_b', source_run_id: 'run_b', text: 'Other run memory' }
    vi.mocked(listMemory).mockResolvedValue([next])
    view.rerender(<MemoryReview sourceRunId="run_b" />)
    await screen.findByText(next.text)
    await act(async () => { old.resolve([proposed]) })
    expect(screen.queryByText(proposed.text)).not.toBeInTheDocument()
  })

  it('ignores an obsolete refresh and catches proposals received during a decision', async () => {
    const view = render(<MemoryReview sourceRunId="run_a" />)
    await screen.findByText(proposed.text)
    const old = deferred<MemoryItem[]>()
    vi.mocked(listMemory).mockReturnValueOnce(old.promise)
    view.rerender(<MemoryReview sourceRunId="run_a" revision={2} />)
    const decision = deferred<MemoryItem>()
    vi.mocked(decideMemory).mockReturnValueOnce(decision.promise)
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(decideMemory).toHaveBeenCalledTimes(1)
    view.rerender(<MemoryReview sourceRunId="run_a" revision={3} />)
    const approved = { ...proposed, status: 'approved' as const, version: 2 }
    const added = { ...proposed, id: 'mem_2', text: 'Added while deciding' }
    vi.mocked(listMemory).mockResolvedValue([approved, added])
    await act(async () => { decision.resolve(approved); old.resolve([proposed]) })
    await screen.findByText(added.text)
    expect(screen.getAllByRole('button', { name: 'Approve' })).toHaveLength(1)
    expect(screen.getByText('Approved')).toBeInTheDocument()
  })

  it('keeps an unsaved correction when a fresh proposal is observed', async () => {
    const view = render(<MemoryReview sourceRunId="run_a" />)
    await screen.findByText(proposed.text)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByLabelText('Memory text'), { target: { value: 'User draft' } })
    vi.mocked(listMemory).mockResolvedValue([proposed, { ...proposed, id: 'mem_2', text: 'Another proposal' }])
    view.rerender(<MemoryReview sourceRunId="run_a" revision={3} />)
    await screen.findByText('Another proposal')
    expect(screen.getByLabelText('Memory text')).toHaveValue('User draft')
    vi.mocked(correctMemory).mockRejectedValue(new Error('Review denied'))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByText('Review denied')
    await waitFor(() => expect(screen.queryByText('Refreshing memories…')).not.toBeInTheDocument())
    expect(screen.getByLabelText('Memory text')).toHaveValue('User draft')
    expect(screen.getByText('Review denied')).toBeInTheDocument()
  })

  it('localizes the task memory panel and approval boundary', () => {
    render(<I18nContext.Provider value="zh-CN"><RunMemoryPanel runId="run_a" revision={0} /></I18nContext.Provider>)
    expect(screen.getByText('本次任务的记忆')).toBeInTheDocument()
    expect(screen.getByText('审阅本次任务产生的记忆提案，批准后才会生效。')).toBeInTheDocument()
  })
})
