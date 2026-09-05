import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import FeedbackForm from './FeedbackForm'

vi.mock('../api/client', () => ({
  submitFeedback: vi.fn(), errorMessage: (error: unknown) => String(error),
}))
import { submitFeedback } from '../api/client'

describe('FeedbackForm', () => {
  beforeEach(() => vi.clearAllMocks())

  it('sends the exact completed message anchor and written why', async () => {
    vi.mocked(submitFeedback).mockResolvedValue({ id: 'feedback_1' } as never)
    render(<FeedbackForm target={{ project_id: 'project_1', run_id: 'run_1', message_id: 'run_1:7', kind: 'conclusion_wrong' }} label="Flag conclusion" />)
    fireEvent.click(screen.getByRole('button', { name: /flag conclusion/i }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '   ' } })
    expect(screen.getByRole('button', { name: /submit feedback/i })).toBeDisabled()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '  The quote excludes freight.  ' } })
    fireEvent.click(screen.getByRole('button', { name: /submit feedback/i }))
    await waitFor(() => expect(submitFeedback).toHaveBeenCalledWith({
      project_id: 'project_1', run_id: 'run_1', message_id: 'run_1:7', kind: 'conclusion_wrong', user_note: 'The quote excludes freight.',
    }))
    expect(await screen.findByRole('status')).toHaveTextContent('unlabeled candidate')
  })

  it('does not mark a newly selected anchor sent when the old request resolves', async () => {
    let finish!: (value: never) => void
    vi.mocked(submitFeedback).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const { rerender } = render(<FeedbackForm key="old" target={{ run_id: 'old', kind: 'run_failure' }} label="Flag run failure" />)
    fireEvent.click(screen.getByRole('button', { name: /flag run failure/i }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Old run reason.' } })
    fireEvent.click(screen.getByRole('button', { name: /submit feedback/i }))
    rerender(<FeedbackForm key="new" target={{ run_id: 'new', kind: 'run_failure' }} label="Flag run failure" />)
    await act(async () => finish({ id: 'old_feedback' } as never))
    expect(screen.getByRole('button', { name: /flag run failure/i })).not.toBeDisabled()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})
