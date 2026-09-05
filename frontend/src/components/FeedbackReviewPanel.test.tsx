import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { FeedbackItem } from '../api/types'
import FeedbackReviewPanel from './FeedbackReviewPanel'

vi.mock('../api/client', () => ({
  listFeedback: vi.fn(), reviewFeedback: vi.fn(),
  runEventUrl: (id: string) => `/api/runs/${id}/events`,
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'Unknown error',
}))
import { listFeedback, reviewFeedback } from '../api/client'

const item: FeedbackItem = {
  id: 'feedback_1', project_id: 'project_1', run_id: 'run_1', evidence_id: null,
  message_id: 'run_1:3', kind: 'conclusion_wrong', user_note: 'Synthetic quote is 20, not 25.',
  status: 'open', revision: 1, created_at: '2026-09-05T00:00:00Z', source: 'user',
  context: {
    trace_id: 'trace_1', run_status: 'completed', event_id: 'run_1:3',
    event_type: 'assistant.message.completed', message_text: 'Synthetic quote is 25 USD.',
    evidence: null, captured_at: '2026-09-05T00:00:00Z',
  },
}

async function openPanel() {
  render(<FeedbackReviewPanel projectId="project_1" />)
  fireEvent.click(screen.getByText('Feedback review and evaluation candidates'))
  return await screen.findByText(item.user_note)
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

describe('FeedbackReviewPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(listFeedback).mockResolvedValue([item])
  })

  it('loads only on request, displays exact anchors and saves a human review reason', async () => {
    await openPanel()
    expect(listFeedback).toHaveBeenCalledWith('project_1', undefined, 0)
    expect(screen.getByText('trace_1')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /open recorded event trace/i })).toHaveAttribute('href', '/api/runs/run_1/events')
    const article = screen.getByText(item.user_note).closest('article')!
    fireEvent.click(within(article).getByText('Review this report'))
    expect(within(article).getByRole('button', { name: /save review/i })).toBeDisabled()
    fireEvent.change(within(article).getByRole('textbox', { name: /review reason/i }), { target: { value: 'Checked the synthetic quote fixture.' } })
    vi.mocked(reviewFeedback).mockResolvedValue({ ...item, status: 'reviewed', revision: 2 })
    fireEvent.click(within(article).getByRole('button', { name: /save review/i }))
    await waitFor(() => expect(reviewFeedback).toHaveBeenCalledWith(item.id, {
      expected_revision: 1, status: 'reviewed', note: 'Checked the synthetic quote fixture.',
    }))
    expect(await screen.findByText('Reviewed · conclusion_wrong')).toBeInTheDocument()
  })

  it('shows authorization/conflict errors without pretending review succeeded', async () => {
    await openPanel()
    fireEvent.click(screen.getByText('Review this report'))
    fireEvent.change(screen.getByRole('textbox', { name: /review reason/i }), { target: { value: 'Review note.' } })
    vi.mocked(reviewFeedback).mockRejectedValue(new Error('Feedback changed; reload before reviewing'))
    fireEvent.click(screen.getByRole('button', { name: /save review/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Feedback changed; reload')
    expect(screen.getByText('Open · conclusion_wrong')).toBeInTheDocument()
  })

  it('requires exact version and manual L2 attestations for folding; never changes Skills', async () => {
    vi.mocked(listFeedback).mockResolvedValue([{ ...item, status: 'reviewed', revision: 2 }])
    await openPanel()
    fireEvent.click(screen.getByText('Review this report'))
    fireEvent.change(screen.getByRole('combobox', { name: /review decision/i }), { target: { value: 'folded_into_skill' } })
    expect(screen.getByText(/does not modify a Skill or certify/i)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: /skill content hash/i })).toBeRequired()
    expect(screen.getByRole('textbox', { name: /l2 regression report reference/i })).toBeRequired()
    expect(screen.getByRole('checkbox', { name: /manually updated/i })).toBeRequired()
    expect(screen.getByRole('checkbox', { name: /full L2 regression report/i })).toBeRequired()
    expect(reviewFeedback).not.toHaveBeenCalled()
  })

  it('keeps a successful review revision when an older list refresh resolves afterward', async () => {
    await openPanel()
    const staleList = deferred<FeedbackItem[]>()
    vi.mocked(listFeedback).mockReturnValueOnce(staleList.promise)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh feedback' }))
    fireEvent.click(screen.getByText('Review this report'))
    fireEvent.change(screen.getByRole('textbox', { name: 'Review reason' }), { target: { value: 'Checked the quote.' } })
    vi.mocked(reviewFeedback).mockResolvedValueOnce({ ...item, status: 'reviewed', revision: 2 })
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }))
    await screen.findByText('Reviewed · conclusion_wrong')
    await act(async () => staleList.resolve([item]))
    expect(screen.getByText('Reviewed · conclusion_wrong')).toBeInTheDocument()
    expect(screen.queryByText('Open · conclusion_wrong')).not.toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox', { name: 'Review reason' }), { target: { value: 'Reopen after new evidence.' } })
    vi.mocked(reviewFeedback).mockResolvedValueOnce({ ...item, status: 'open', revision: 3 })
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }))
    await waitFor(() => expect(reviewFeedback).toHaveBeenLastCalledWith(item.id, {
      expected_revision: 2, status: 'open', note: 'Reopen after new evidence.',
    }))
  })

  it('does not resurrect a reviewed candidate when a newer list reports it rejected', async () => {
    await openPanel()
    const staleReview = deferred<FeedbackItem>()
    vi.mocked(reviewFeedback).mockReturnValueOnce(staleReview.promise)
    fireEvent.click(screen.getByText('Review this report'))
    fireEvent.change(screen.getByRole('textbox', { name: 'Review reason' }), { target: { value: 'Checked the quote.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }))
    vi.mocked(listFeedback).mockResolvedValueOnce([{ ...item, status: 'rejected', revision: 3 }])
    fireEvent.click(screen.getByRole('button', { name: 'Refresh feedback' }))
    await screen.findByText('Rejected · conclusion_wrong')
    await act(async () => staleReview.resolve({ ...item, status: 'reviewed', revision: 2 }))
    expect(screen.getByText('Rejected · conclusion_wrong')).toBeInTheDocument()
    expect(screen.queryByText('Reviewed · conclusion_wrong')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Export loaded unlabeled candidates' })).toBeDisabled()
  })

  it('advances by consumed server rows even when an entire appended page duplicates earlier records', async () => {
    const oldRows = Array.from({ length: 201 }, (_,index) => ({ ...item, id: `old_${index}`, user_note: `Old report ${index}` }))
    let records = oldRows
    vi.mocked(listFeedback).mockImplementation(async (_project, _status, offset = 0) => records.slice(offset, offset + 200))
    const { container } = render(<FeedbackReviewPanel projectId="project_1" />)
    fireEvent.click(screen.getByText('Feedback review and evaluation candidates'))
    await screen.findByText('Old report 199')
    records = [...Array.from({ length: 200 }, (_,index) => ({ ...item, id: `new_${index}`, user_note: `New report ${index}` })), ...oldRows]
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Load more feedback' })))
    expect(container.querySelectorAll('.feedback-review article')).toHaveLength(200)
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Load more feedback' })))
    expect(screen.getByText('Old report 200')).toBeInTheDocument()
    expect(container.querySelectorAll('.feedback-review article')).toHaveLength(201)
    expect(screen.queryByRole('button', { name: 'Load more feedback' })).not.toBeInTheDocument()
    expect(vi.mocked(listFeedback).mock.calls.map((args) => args[2])).toEqual([0, 200, 400])
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Refresh feedback' })))
    expect(screen.getByText('New report 0')).toBeInTheDocument()
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Load more feedback' })))
    expect(vi.mocked(listFeedback).mock.calls.map((args) => args[2])).toEqual([0, 200, 400, 0, 200])
  })

  it('merges a newer revision from a duplicate page instead of discarding that record', async () => {
    const rows = [item, ...Array.from({ length: 199 }, (_,index) => ({ ...item, id: `other_${index}`, user_note: `Other report ${index}` }))]
    vi.mocked(listFeedback).mockResolvedValueOnce(rows)
    await openPanel()
    vi.mocked(listFeedback).mockResolvedValueOnce([{ ...item, status: 'reviewed', revision: 2 }])
    fireEvent.click(screen.getByRole('button', { name: 'Load more feedback' }))
    await screen.findByText('Reviewed · conclusion_wrong')
    const article = screen.getByText(item.user_note).closest('article')!
    expect(within(article).getByText('Reviewed · conclusion_wrong')).toBeInTheDocument()
    expect(screen.getAllByText(item.user_note)).toHaveLength(1)
  })

  it('isolates an old project list response when the project prop changes without an external key', async () => {
    const oldList = deferred<FeedbackItem[]>()
    vi.mocked(listFeedback).mockReturnValueOnce(oldList.promise)
    const { rerender } = render(<FeedbackReviewPanel projectId="project_1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh feedback' }))
    rerender(<FeedbackReviewPanel projectId="project_2" />)
    const newItem = { ...item, id: 'feedback_2', project_id: 'project_2', user_note: 'New project report.' }
    vi.mocked(listFeedback).mockResolvedValueOnce([newItem])
    fireEvent.click(screen.getByText('Feedback review and evaluation candidates'))
    await screen.findByText(newItem.user_note)
    await act(async () => oldList.resolve([item]))
    expect(screen.getByText(newItem.user_note)).toBeInTheDocument()
    expect(screen.queryByText(item.user_note)).not.toBeInTheDocument()
    expect(listFeedback).toHaveBeenLastCalledWith('project_2', undefined, 0)
  })

  it('ignores an old project review response after switching the panel project', async () => {
    const oldReview = deferred<FeedbackItem>()
    vi.mocked(reviewFeedback).mockReturnValueOnce(oldReview.promise)
    const { rerender } = render(<FeedbackReviewPanel projectId="project_1" />)
    fireEvent.click(screen.getByText('Feedback review and evaluation candidates'))
    await screen.findByText(item.user_note)
    fireEvent.click(screen.getByText('Review this report'))
    fireEvent.change(screen.getByRole('textbox', { name: 'Review reason' }), { target: { value: 'Old project review.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }))
    rerender(<FeedbackReviewPanel projectId="project_2" />)
    const newItem = { ...item, id: 'feedback_2', project_id: 'project_2', user_note: 'New project report.' }
    vi.mocked(listFeedback).mockResolvedValueOnce([newItem])
    fireEvent.click(screen.getByText('Feedback review and evaluation candidates'))
    await screen.findByText(newItem.user_note)
    await act(async () => oldReview.resolve({ ...item, status: 'reviewed', revision: 2 }))
    expect(screen.getByText(newItem.user_note)).toBeInTheDocument()
    expect(screen.queryByText(item.user_note)).not.toBeInTheDocument()
    expect(screen.queryByText('Reviewed · conclusion_wrong')).not.toBeInTheDocument()
  })

  it('exports only loaded anchored non-rejected candidates and omits identity fields', async () => {
    vi.mocked(listFeedback).mockResolvedValue([
      { ...item, principal_session_id: 'private-session', principal_id: 'private@example.test' } as FeedbackItem,
      { ...item, id: 'rejected', status: 'rejected' },
      { ...item, id: 'legacy', context: null },
    ])
    const createUrl = vi.fn((blob: Blob) => {
      expect(blob.type).toBe('application/json')
      return 'blob:feedback-fixture'
    })
    const revokeUrl = vi.fn()
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: createUrl })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: revokeUrl })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
    render(<FeedbackReviewPanel projectId="project_1" />)
    fireEvent.click(screen.getByText('Feedback review and evaluation candidates'))
    await waitFor(() => expect(screen.getByRole('button', { name: /export loaded/i })).not.toBeDisabled())
    fireEvent.click(screen.getByRole('button', { name: /export loaded/i }))
    expect(createUrl).toHaveBeenCalledTimes(1)
    const blob = createUrl.mock.calls[0][0] as Blob
    const text = await new Promise<string>((resolve) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result))
      reader.readAsText(blob)
    })
    expect(JSON.parse(text).map((row: FeedbackItem) => row.id)).toEqual(['feedback_1'])
    expect(text).not.toContain('private-session')
    expect(text).not.toContain('private@example.test')
    expect(text).toContain('unlabeled')
    expect(click).toHaveBeenCalledOnce()
    expect(revokeUrl).toHaveBeenCalledWith('blob:feedback-fixture')
    click.mockRestore()
    Reflect.deleteProperty(URL, 'createObjectURL')
    Reflect.deleteProperty(URL, 'revokeObjectURL')
  })
})
