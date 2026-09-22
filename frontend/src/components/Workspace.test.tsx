import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { FeedbackItem, OrgPolicy, RunRecord } from '../api/types'
import { useOpportunityRuntime } from '../api/useOpportunityRuntime'
import Workspace from './Workspace'

vi.mock('../api/useOpportunityRuntime', () => ({ useOpportunityRuntime: vi.fn() }))
vi.mock('../api/client', () => ({
  listProviders: vi.fn(async () => []),
  getOrgPolicy: vi.fn(),
  saveOrgPolicy: vi.fn(),
  submitFeedback: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))

import { getOrgPolicy, saveOrgPolicy, submitFeedback } from '../api/client'

const organizationPolicy: OrgPolicy = {
  permission_mode: 'default',
  allowed_provider_ids: [],
  allowed_skill_ids: [],
  browser_enabled: true,
  computer_control_enabled: false,
  channel_delivery_enabled: false,
  domain_allowlist: [],
  commercial_actions_enabled: false,
  allowed_commercial_operations: [],
  allowed_commercial_connector_ids: [],
  commercial_max_changes_per_action: 20,
  commercial_forbidden_fields: [],
  commercial_numeric_limits: [],
  updated_at: '2026-09-11T00:00:00Z',
}

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
    permissionBusy: false,
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
    resumePermission: vi.fn(),
    refreshPermissions: vi.fn(),
    selectProject: vi.fn(),
    refreshActiveProject: vi.fn(),
    reset: vi.fn(),
    conversationDeleted: vi.fn(),
    projectDeleted: vi.fn(),
  })
  vi.mocked(getOrgPolicy).mockResolvedValue(organizationPolicy)
})

describe('Workspace approval mode', () => {
  it('saves the composer approval choice onto the latest organization policy', async () => {
    const latest: OrgPolicy = { ...organizationPolicy, browser_enabled: false, updated_at: '2026-09-11T01:00:00Z' }
    vi.mocked(getOrgPolicy).mockResolvedValueOnce(organizationPolicy).mockResolvedValueOnce(latest)
    vi.mocked(saveOrgPolicy).mockImplementationOnce(async (policy) => policy)
    render(<Workspace />)

    const trigger = screen.getByRole('button', { name: /open approval mode selector/i })
    await waitFor(() => expect(trigger).toHaveTextContent('Ask for approval'))
    fireEvent.click(trigger)
    fireEvent.click(screen.getByRole('button', { name: /approve for me/i }))

    await waitFor(() => expect(saveOrgPolicy).toHaveBeenCalledWith({ ...latest, permission_mode: 'auto' }))
    await waitFor(() => expect(trigger).toHaveTextContent('Approve for me'))
    expect(screen.queryByRole('dialog', { name: /choose an approval mode/i })).not.toBeInTheDocument()
  })
})

describe('Workspace feedback', () => {
  it('lets the user report one exact recorded assistant message without flagging its neighbors', async () => {
    const base = vi.mocked(useOpportunityRuntime)()
    vi.mocked(useOpportunityRuntime).mockReturnValue({
      ...base, run: { ...failedRun, status: 'completed' }, assistantText: 'Two synthetic conclusions.',
      events: ['Synthetic first conclusion is 25 USD.', 'Synthetic second conclusion is 30 USD.'].map((text, index) => ({
        id: `run_failed:${index + 1}`, sequence: index + 1, type: 'assistant.message.completed',
        run_id: 'run_failed', project_id: 'project_1', trace_id: 'trace_failed',
        sdk_session_id: null, provider_profile_id: null, timestamp: '2026-09-05T00:00:00Z',
        actor: { type: 'agent', name: 'main', agent_id: null }, parent_id: null, tool_use_id: null, data: { text },
      })),
    })
    vi.mocked(submitFeedback).mockResolvedValueOnce({ ...recordedFeedback, kind: 'conclusion_wrong' })
    render(<Workspace />)
    fireEvent.click(screen.getByText('Report a conclusion issue'))
    const reports = screen.getByText('Report a conclusion issue').closest('details')!
    const first = within(reports).getByText('Synthetic first conclusion is 25 USD.').closest('article')!
    const second = within(reports).getByText('Synthetic second conclusion is 30 USD.').closest('article')!
    fireEvent.click(within(first).getByRole('button', { name: /flag conclusion/i }))
    fireEvent.change(within(first).getByRole('textbox'), { target: { value: 'First quote excludes freight.' } })
    fireEvent.click(within(first).getByRole('button', { name: /submit feedback/i }))
    await waitFor(() => expect(submitFeedback).toHaveBeenCalledWith({
      project_id: 'project_1', run_id: 'run_failed', message_id: 'run_failed:1',
      kind: 'conclusion_wrong', user_note: 'First quote excludes freight.',
    }))
    expect(within(second).getByRole('button', { name: /flag conclusion/i })).not.toBeDisabled()
  })

  it('exposes failed-run feedback, prevents duplicate sends, reports errors, and permits a retry', async () => {
    let rejectFeedback!: (reason: unknown) => void
    vi.mocked(submitFeedback).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectFeedback = reject }))
    render(<Workspace />)

    const flag = screen.getByRole('button', { name: /flag run failure/i })
    fireEvent.click(flag)
    expect(submitFeedback).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /submit feedback/i })).toBeDisabled()
    fireEvent.change(screen.getByRole('textbox', { name: /what is wrong/i }), {
      target: { value: 'The provider timed out before the fixture was inspected.' },
    })
    const send = screen.getByRole('button', { name: /submit feedback/i })
    fireEvent.click(send)
    fireEvent.click(send)
    expect(submitFeedback).toHaveBeenCalledTimes(1)
    expect(submitFeedback).toHaveBeenCalledWith({
      project_id: 'project_1', run_id: 'run_failed', kind: 'run_failure',
      user_note: 'The provider timed out before the fixture was inspected.',
    })

    await act(async () => { rejectFeedback(new Error('Feedback unavailable')) })
    const feedbackError = await screen.findByText('Feedback unavailable')
    expect(feedbackError).toHaveAttribute('role', 'alert')

    vi.mocked(submitFeedback).mockResolvedValueOnce(recordedFeedback)
    fireEvent.click(screen.getByRole('button', { name: /submit feedback/i }))
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
