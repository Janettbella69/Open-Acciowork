import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { FeedbackItem, RunRecord } from '../api/types'
import { useOpportunityRuntime } from '../api/useOpportunityRuntime'
import Workspace from './Workspace'

vi.mock('../api/useOpportunityRuntime', () => ({ useOpportunityRuntime: vi.fn() }))
vi.mock('../api/client', () => ({
  listProviders: vi.fn(async () => []),
  submitFeedback: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))

import { submitFeedback } from '../api/client'

const retryRuntime = vi.fn()

const failedRun: RunRecord = {
  id: 'run_failed',
  trace_id: 'trace_failed',
  project_id: 'project_1',
  prompt: 'Research this market',
  agent_id: null,
  mode: 'standard',
  provider_profile_id: null,
  provider_snapshot: null,
  skill_snapshot: [],
  status: 'failed',
  sdk_session_id: null,
  error: 'Provider timed out',
  error_code: 'PROVIDER_TIMEOUT',
  created_at: '2026-09-02T00:00:00Z',
  started_at: '2026-09-02T00:00:00Z',
  finished_at: '2026-09-02T00:01:00Z',
}

const recordedFeedback: FeedbackItem = {
  id: 'feedback_1',
  project_id: 'project_1',
  run_id: 'run_failed',
  evidence_id: null,
  kind: 'run_failure',
  user_note: 'User flagged a run failure.',
  status: 'open',
  created_at: '2026-09-02T00:02:00Z',
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(useOpportunityRuntime).mockReturnValue({
    projects: [],
    project: null,
    run: failedRun,
    telemetry: null,
    events: [],
    assistantText: '',
    reasoningSummary: '',
    permissions: [],
    connection: 'closed',
    error: '',
    starting: false,
    historyLoading: false,
    isActive: false,
    start: vi.fn(),
    continueProject: vi.fn(),
    retry: retryRuntime,
    cancel: vi.fn(),
    decidePermission: vi.fn(),
    selectProject: vi.fn(),
    refreshActiveProject: vi.fn(),
    reset: vi.fn(),
  })
})

describe('Workspace feedback', () => {
  it('exposes failed-run feedback, prevents duplicate sends, reports errors, and permits a retry', async () => {
    let rejectFeedback!: (reason: unknown) => void
    vi.mocked(submitFeedback).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFeedback = reject }))
    render(<Workspace />)

    const flag = screen.getByRole('button', { name: /flag run failure/i })
    fireEvent.click(flag)
    fireEvent.click(flag)
    expect(submitFeedback).toHaveBeenCalledTimes(1)

    await act(async () => { rejectFeedback(new Error('Feedback unavailable')) })
    const feedbackError = await screen.findByText('Feedback unavailable')
    expect(feedbackError).toHaveAttribute('role', 'alert')

    vi.mocked(submitFeedback).mockResolvedValueOnce(recordedFeedback)
    fireEvent.click(screen.getByRole('button', { name: /flag run failure/i }))
    await waitFor(() => expect(submitFeedback).toHaveBeenCalledTimes(2))
    const sent = await screen.findByRole('button', { name: /feedback sent/i })
    expect(sent).toBeDisabled()
    fireEvent.click(sent)
    expect(submitFeedback).toHaveBeenCalledTimes(2)
  })
})

describe('Workspace run retry', () => {
  it('prevents duplicate retry submits, surfaces an API error, and permits another attempt', async () => {
    let rejectRetry!: (reason: unknown) => void
    retryRuntime.mockImplementationOnce(() => new Promise((_resolve, reject) => {
      rejectRetry = reject
    }))
    render(<Workspace />)

    const branch = screen.getByRole('button', { name: /start new branch/i })
    fireEvent.click(branch)
    fireEvent.click(branch)
    expect(retryRuntime).toHaveBeenCalledTimes(1)
    expect(retryRuntime).toHaveBeenCalledWith('new_branch')

    await act(async () => { rejectRetry(new Error('Retry temporarily unavailable')) })
    expect(await screen.findByText('Retry temporarily unavailable')).toHaveAttribute('role', 'alert')

    retryRuntime.mockResolvedValueOnce({ ...failedRun, id: 'run_retried', status: 'queued' })
    fireEvent.click(screen.getByRole('button', { name: /start new branch/i }))
    await waitFor(() => expect(retryRuntime).toHaveBeenCalledTimes(2))
  })
})
