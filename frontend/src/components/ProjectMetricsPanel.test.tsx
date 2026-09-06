import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { MetricDistribution, ProjectMetrics, ProjectRunMetrics } from '../api/projectMetrics'
import ProjectMetricsPanel from './ProjectMetricsPanel'

vi.mock('../api/client', () => ({
  getProjectMetrics: vi.fn(),
  errorMessage: (error: unknown) => String(error instanceof Error ? error.message : error),
}))

import { getProjectMetrics } from '../api/client'

function distribution(value: number | null, unavailable = 1): MetricDistribution {
  return {
    observed_count: value === null ? 0 : 1,
    unavailable_count: unavailable,
    p50: value,
    p95: value,
    observed_total: value,
  }
}

function fixture(projectId = 'project_1', provider = 'provider-current'): ProjectMetrics {
  const runs: ProjectRunMetrics = {
    run_count: 2,
    status_counts: { completed: 1, failed: 1 },
    execution_failure_rate: { numerator: 1, denominator: 2, value: 0.5 },
    duration_ms: distribution(1000),
    queue_duration_ms: distribution(0),
    ttft_ms: distribution(null, 2),
    cost_estimate_usd: distribution(0.25),
    cost_basis_counts: { sdk_anthropic_list_price: 1 },
  }
  const invocations = {
    invocation_count: 1,
    completed_count: 1,
    failed_count: 0,
    unknown_count: 0,
    pending_count: 0,
    failure_rate: { numerator: 0, denominator: 1, value: 0 },
    duration_ms: distribution(200, 0),
    queue_duration_ms: distribution(0, 0),
  }
  return {
    schema_version: '1',
    project_id: projectId,
    window: {
      created_since: '2026-08-30T12:00:00Z',
      created_until: '2026-09-06T12:00:00Z',
      observed_at: '2026-09-06T12:00:00Z',
      selection_basis: 'run.created_at',
    },
    coverage: {
      matching_runs: 2,
      selected_runs: 2,
      omitted_by_run_limit: 0,
      unavailable_event_replays: 1,
      unavailable_reasons: { run_event_limit: 1 },
      invalid_created_at_runs: 0,
      event_bytes_read: 2000,
      event_count_read: 10,
    },
    summary: runs,
    providers: [{
      dimensions: { provider_profile_id: provider, configured_model: 'configured-model' },
      runs: structuredClone(runs),
    }],
    models: [{
      dimensions: { observed_model: 'served-model' },
      runs: { ...structuredClone(runs), cost_estimate_usd: null },
    }],
    skills: [{ identifier: 'ophira-core:landed-cost-analysis', run_count: 1, invocations }],
    mcp_tools: [{
      identifier: 'mcp__ophira_commerce_math__cashflow_peak', run_count: 1, invocations,
    }],
    errors: [{ code: 'SDK_RUNTIME_FAILED', failed_run_count: 1 }],
    business_outcome_available: false,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => { resolve = done })
  return { promise, resolve }
}

function openPanel() {
  fireEvent.click(screen.getByRole('button', { name: 'Project run metrics' }))
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(getProjectMetrics).mockResolvedValue(fixture())
})

describe('ProjectMetricsPanel', () => {
  it('loads only after opening and requests the selected period with a cancellation signal', async () => {
    render(<ProjectMetricsPanel projectId="project_1" />)
    expect(getProjectMetrics).not.toHaveBeenCalled()
    expect(screen.queryByRole('combobox', { name: 'Metrics period' })).not.toBeInTheDocument()

    openPanel()
    await screen.findByText(/provider-current/)

    const period = screen.getByRole('combobox', { name: 'Metrics period' }) as HTMLSelectElement
    expect(getProjectMetrics).toHaveBeenCalledOnce()
    expect(getProjectMetrics).toHaveBeenCalledWith(
      'project_1', Number(period.value), expect.any(AbortSignal),
    )
    expect(screen.getByRole('option', { name: 'Last 7 days' })).toHaveValue('7')
    expect(screen.getByRole('option', { name: 'Last 30 days' })).toHaveValue('30')
    const costGroup = screen.getByText('Observed SDK estimate', { selector: 'dt' }).parentElement!
    expect(costGroup).toHaveTextContent('$0.2500')
    expect(costGroup).toHaveTextContent('1 measured · 1 unavailable')
    expect(screen.queryByText(/actual (?:cost|spend)|billed total/i)).not.toBeInTheDocument()
  })

  it.each([
    ['missing cost distribution', null],
    ['unavailable total', distribution(null, 2)],
    ['zero total with no observations', { ...distribution(null, 2), observed_total: 0 }],
  ] as const)('displays %s as unavailable without implying a zero bill', async (_name, costMetric) => {
    const data = fixture()
    data.summary.cost_estimate_usd = costMetric
    vi.mocked(getProjectMetrics).mockResolvedValueOnce(data)
    render(<ProjectMetricsPanel projectId="project_1" />)
    openPanel()
    await screen.findByText(/provider-current/)

    const costGroup = screen.getByText('Observed SDK estimate', { selector: 'dt' }).parentElement!
    expect(within(costGroup).getByText('Unavailable')).toBeInTheDocument()
    expect(costGroup).not.toHaveTextContent('$0.0000')
    const ttftGroup = screen.getByText('First token P95', { selector: 'dt' }).parentElement!
    expect(within(ttftGroup).getByText('Unavailable')).toBeInTheDocument()
    const queueGroup = screen.getByText('Queue duration P95', { selector: 'dt' }).parentElement!
    expect(within(queueGroup).getByText('0 ms')).toBeInTheDocument()
  })

  it('preserves explicitly observed zero cost and zero failure rate', async () => {
    const data = fixture()
    data.summary.cost_estimate_usd = distribution(0)
    data.summary.execution_failure_rate = { numerator: 0, denominator: 1, value: 0 }
    vi.mocked(getProjectMetrics).mockResolvedValueOnce(data)
    render(<ProjectMetricsPanel projectId="project_1" />)
    openPanel()
    await screen.findByText(/provider-current/)

    const costGroup = screen.getByText('Observed SDK estimate', { selector: 'dt' }).parentElement!
    expect(within(costGroup).getByText('$0.0000')).toBeInTheDocument()
    expect(within(costGroup).queryByText('Unavailable')).not.toBeInTheDocument()
    const failureGroup = screen.getByText('Execution failure rate').parentElement!
    expect(within(failureGroup).getByText('0.0%')).toBeInTheDocument()
  })

  it('refuses a response identifying a different project', async () => {
    vi.mocked(getProjectMetrics).mockResolvedValueOnce(fixture('project_other', 'provider-foreign'))
    render(<ProjectMetricsPanel projectId="project_1" />)
    openPanel()

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Metrics response did not match this project.',
    )
    expect(screen.queryByText(/provider-foreign/)).not.toBeInTheDocument()
  })

  it('keeps a newer period response when an aborted earlier request still resolves', async () => {
    const oldResponse = deferred<ProjectMetrics>()
    vi.mocked(getProjectMetrics)
      .mockReturnValueOnce(oldResponse.promise)
      .mockResolvedValueOnce(fixture('project_1', 'provider-new-period'))
    render(<ProjectMetricsPanel projectId="project_1" />)
    openPanel()
    const oldSignal = vi.mocked(getProjectMetrics).mock.calls[0][2]!
    const period = screen.getByRole('combobox', { name: 'Metrics period' }) as HTMLSelectElement
    const nextDays = period.value === '7' ? 30 : 7

    fireEvent.change(period, { target: { value: String(nextDays) } })
    await screen.findByText(/provider-new-period/)
    expect(oldSignal.aborted).toBe(true)
    expect(getProjectMetrics).toHaveBeenLastCalledWith(
      'project_1', nextDays, expect.any(AbortSignal),
    )
    await act(async () => { oldResponse.resolve(fixture('project_1', 'provider-stale-period')) })

    expect(screen.queryByText(/provider-stale-period/)).not.toBeInTheDocument()
    expect(screen.getByText(/provider-new-period/)).toBeInTheDocument()
  })

  it('aborts and isolates an old project response after the project selection changes', async () => {
    const oldResponse = deferred<ProjectMetrics>()
    vi.mocked(getProjectMetrics)
      .mockReturnValueOnce(oldResponse.promise)
      .mockResolvedValueOnce(fixture('project_2', 'provider-new-project'))
    const result = render(<ProjectMetricsPanel projectId="project_1" />)
    openPanel()
    const oldSignal = vi.mocked(getProjectMetrics).mock.calls[0][2]!

    result.rerender(<ProjectMetricsPanel projectId="project_2" />)
    expect(oldSignal.aborted).toBe(true)
    if (!screen.queryByRole('combobox', { name: 'Metrics period' })) openPanel()
    await screen.findByText(/provider-new-project/)
    await act(async () => { oldResponse.resolve(fixture('project_1', 'provider-stale-project')) })

    expect(screen.queryByText(/provider-stale-project/)).not.toBeInTheDocument()
    expect(screen.getByText(/provider-new-project/)).toBeInTheDocument()
    expect(vi.mocked(getProjectMetrics).mock.calls.at(-1)?.[0]).toBe('project_2')
  })

  it('removes previously visible project data when refresh loses access and can retry', async () => {
    vi.mocked(getProjectMetrics)
      .mockResolvedValueOnce(fixture('project_1', 'provider-private-before-denial'))
      .mockRejectedValueOnce(Object.assign(new Error('Project access denied'), { status: 403 }))
      .mockResolvedValueOnce(fixture('project_1', 'provider-after-restored-access'))
    render(<ProjectMetricsPanel projectId="project_1" />)
    openPanel()
    await screen.findByText(/provider-private-before-denial/)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh metrics' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Project access denied')
    expect(screen.queryByText(/provider-private-before-denial/)).not.toBeInTheDocument()
    expect(screen.queryByText('served-model')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh metrics' }))
    await screen.findByText(/provider-after-restored-access/)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('shows an empty observation window without fabricating a successful zero-cost run', async () => {
    const empty = fixture()
    empty.coverage = { ...empty.coverage, matching_runs: 0, selected_runs: 0,
      unavailable_event_replays: 0, unavailable_reasons: {}, event_bytes_read: 0, event_count_read: 0 }
    empty.summary = {
      run_count: 0, status_counts: {},
      execution_failure_rate: { numerator: 0, denominator: 0, value: null },
      duration_ms: distribution(null, 0), queue_duration_ms: distribution(null, 0),
      ttft_ms: distribution(null, 0), cost_estimate_usd: distribution(null, 0),
      cost_basis_counts: {},
    }
    empty.providers = []
    empty.models = []
    empty.skills = []
    empty.mcp_tools = []
    empty.errors = []
    vi.mocked(getProjectMetrics).mockResolvedValueOnce(empty)
    render(<ProjectMetricsPanel projectId="project_1" />)
    openPanel()

    expect(await screen.findByText('No runs were created in this period.')).toBeInTheDocument()
    expect(screen.queryByText(/provider-current/)).not.toBeInTheDocument()
  })

  it('cancels an outstanding request when the panel unmounts', async () => {
    const response = deferred<ProjectMetrics>()
    vi.mocked(getProjectMetrics).mockReturnValueOnce(response.promise)
    const result = render(<ProjectMetricsPanel projectId="project_1" />)
    openPanel()
    await waitFor(() => expect(getProjectMetrics).toHaveBeenCalledOnce())
    const signal = vi.mocked(getProjectMetrics).mock.calls[0][2]!

    result.unmount()
    expect(signal.aborted).toBe(true)
    await act(async () => { response.resolve(fixture()) })
  })
})
