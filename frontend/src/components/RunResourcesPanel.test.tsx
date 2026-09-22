import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { RunResources } from '../api/runResources'
import type { RunRecord, TelemetryMetric } from '../api/types'
import { I18nContext } from '../i18n'
import RunResourcesPanel from './RunResourcesPanel'

vi.mock('../api/client', () => ({
  getRunResources: vi.fn(),
  errorMessage: (reason: unknown) => String(reason instanceof Error ? reason.message : reason),
}))
import { getRunResources } from '../api/client'

const run: RunRecord = {
  id: 'run_1', project_id: 'project_1', trace_id: 'trace_1', prompt: 'Research', agent_id: null,
  mode: 'deep_research', provider_profile_id: null, provider_snapshot: null, skill_snapshot: [],
  status: 'running', sdk_session_id: null, error: null, error_code: null,
  created_at: '2026-09-09T12:00:00Z', started_at: '2026-09-09T12:00:00Z', finished_at: null,
}
function metric<T>(value: T | null): TelemetryMetric<T> {
  return { value, availability: value === null ? 'unavailable' : 'observed', unit: 'count', source: 'SDK', source_event_sequences: [], scope: 'query_pipeline', unavailable_reason: value === null ? 'Not reported' : null }
}
const resources: RunResources = {
  schema_version: '1', run_id: run.id, project_id: run.project_id, trace_id: run.trace_id,
  mode: 'deep_research', status: 'running', observed_at: run.started_at!, computed_through_sequence: 8,
  observations_available: true, unavailable_reason: null,
  limits: { frozen: true, sdk_budget_usd: 2, turn_limit: null, wall_clock_deadline: '2026-09-09T12:30:00Z' },
  elapsed_seconds: 60, remaining_seconds: 1740, remaining_budget_usd: null,
  cost: { estimated_cost_usd: metric<number>(null), cost_basis: metric<string>(null) },
  tokens: { input_tokens: metric<number>(null), output_tokens: metric<number>(null), cache_read_input_tokens: metric<number>(null), cache_creation_input_tokens: metric<number>(null), cache_hit: metric<boolean>(null) },
  research_units: [{ id: 'source-mapper', description: '' }, { id: 'multilingual-researcher', description: '' }, { id: 'conflict-verifier', description: '' }],
  agents: [{ agent_id: 'agent_1', agent_type: 'source-mapper', state: 'started', last_observed_at: run.started_at! }],
  tasks: [{ task_id: 'task_1', task_type: 'local_agent', tool_use_id: 'dispatch', requested_agent_type: 'source-mapper', description: '<script>Original source</script>', status: 'running', last_tool_name: 'Read', started_at: run.started_at, finished_at: null, last_observed_at: run.started_at!, usage: { total_tokens: 120, tool_uses: 2, duration_ms: 1500, source_sequence: 8, availability: 'reported' }, warnings: [] }],
}
function value(label: string) { return within(screen.getByText(label).parentElement!).getByRole('definition') }
beforeEach(() => { vi.clearAllMocks(); vi.mocked(getRunResources).mockResolvedValue(resources) })
afterEach(() => vi.useRealTimers())

it('shows frozen shared limits and preserves unknown cost instead of showing zero', async () => {
  const cancel = vi.fn()
  render(<RunResourcesPanel run={run} onCancel={cancel} />)
  await screen.findByText('Unlimited turns')
  expect(value('Shared SDK budget')).toHaveTextContent('$2.0000')
  expect(value('Reported SDK cost')).toHaveTextContent('—')
  expect(value('Remaining reported budget')).toHaveTextContent('—')
  expect(value('Elapsed time')).toHaveTextContent('1:00')
  expect(value('Time to deadline')).toHaveTextContent('29:00')
  expect(screen.getAllByText('No invocation reported')).toHaveLength(2)
  expect(screen.getByText('1 native invocations observed')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Cancel research' }))
  expect(cancel).toHaveBeenCalledOnce()
})

it('shows task snapshots without adding them to run totals or executing source markup', async () => {
  const { container } = render(<RunResourcesPanel run={run} onCancel={vi.fn()} />)
  await screen.findByText('Reported subtasks · 1')
  expect(screen.getByText('<script>Original source</script>')).toBeInTheDocument()
  expect(container.querySelector('script')).toBeNull()
  expect(value('Reported task tokens')).toHaveTextContent('120')
  expect(value('Reported input tokens')).toHaveTextContent('—')
  expect(screen.getByText('Usage snapshot: event 8')).toBeInTheDocument()
})

it('polls sequentially and interpolates the server countdown without using local clock time', async () => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2040-01-01T00:00:00Z'))
  render(<RunResourcesPanel run={run} onCancel={vi.fn()} />)
  await act(async () => {})
  expect(value('Time to deadline')).toHaveTextContent('29:00')
  await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
  expect(value('Time to deadline')).toHaveTextContent('28:58')
  expect(getRunResources).toHaveBeenCalledOnce()
  await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
  expect(getRunResources).toHaveBeenCalledTimes(2)
})

it('stops polling at a terminal observation and does not infer missing task completion', async () => {
  vi.useFakeTimers()
  vi.mocked(getRunResources).mockResolvedValue({ ...resources, status: 'cancelled', elapsed_seconds: 10, remaining_seconds: null })
  render(<RunResourcesPanel run={run} onCancel={vi.fn()} />)
  await act(async () => {})
  expect(screen.queryByRole('button', { name: 'Cancel research' })).not.toBeInTheDocument()
  expect(screen.getByText('The run has ended; this task’s terminal observation is missing.')).toBeInTheDocument()
  await act(async () => { await vi.advanceTimersByTimeAsync(15000) })
  expect(getRunResources).toHaveBeenCalledOnce()
  expect(value('Elapsed time')).toHaveTextContent('0:10')
})

it('clears private values after read failure and can refresh', async () => {
  vi.useFakeTimers()
  vi.mocked(getRunResources).mockRejectedValueOnce(new Error('Access revoked'))
  render(<RunResourcesPanel run={run} onCancel={vi.fn()} />)
  await act(async () => {})
  expect(screen.getByRole('alert')).toHaveTextContent('Access revoked')
  expect(screen.queryByText('Shared SDK budget')).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Refresh resources' }))
  await act(async () => {})
  expect(value('Shared SDK budget')).toHaveTextContent('$2.0000')
})

it('aborts and ignores a delayed response when the selected run changes', async () => {
  let finish!: (value: RunResources) => void
  vi.mocked(getRunResources).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  vi.mocked(getRunResources).mockResolvedValueOnce({ ...resources, run_id: 'run_2', trace_id: 'trace_2', tasks: [] })
  const { rerender } = render(<RunResourcesPanel run={run} onCancel={vi.fn()} />)
  const signal = vi.mocked(getRunResources).mock.calls[0][1]
  rerender(<RunResourcesPanel run={{ ...run, id: 'run_2', trace_id: 'trace_2' }} onCancel={vi.fn()} />)
  await act(async () => finish(resources))
  expect(signal?.aborted).toBe(true)
  expect(screen.queryByText('<script>Original source</script>')).not.toBeInTheDocument()
  expect(screen.getByText('Reported subtasks · 0')).toBeInTheDocument()
})

it('does not show a response whose project or trace does not match', async () => {
  vi.mocked(getRunResources).mockResolvedValue({ ...resources, project_id: 'other' })
  render(<RunResourcesPanel run={run} onCancel={vi.fn()} />)
  await screen.findByRole('alert')
  expect(screen.queryByText('Shared SDK budget')).not.toBeInTheDocument()
})

it('provides Chinese resource copy and keeps limits visible when observations are unavailable', async () => {
  vi.mocked(getRunResources).mockResolvedValue({ ...resources, observations_available: false, unavailable_reason: 'event_replay_limit', tasks: [], agents: [] })
  render(<I18nContext.Provider value="zh-CN"><RunResourcesPanel run={run} onCancel={vi.fn()} /></I18nContext.Provider>)
  await screen.findByText('不设轮数上限')
  expect(value('共用 SDK 估算额度')).toHaveTextContent('$2.0000')
  expect(screen.getByText('事件记录不完整或超过回放上限，暂时无法展示任务与用量观测。')).toBeInTheDocument()
  expect(screen.queryByText('Reported subtasks · 0')).not.toBeInTheDocument()
})

const workerLimits = {
  mode: 'systemd' as const, cpu_percent: 200, memory_bytes: 2 * 1024 ** 3,
  tasks_max: 128, output_bytes: 32 * 1024 ** 2, frame_bytes: 4 * 1024 ** 2,
  timeout_seconds: 14400,
}

it('loads standard run resources only when opened and aborts polling when closed', async () => {
  vi.mocked(getRunResources).mockResolvedValue({ ...resources, mode: 'standard', research_units: [] })
  render(<RunResourcesPanel run={{ ...run, mode: 'standard' }} onCancel={vi.fn()} />)
  expect(getRunResources).not.toHaveBeenCalled()
  const summary = screen.getByText('Run resources', { selector: 'summary' })
  fireEvent.click(summary)
  await screen.findByText('Unlimited turns')
  expect(getRunResources).toHaveBeenCalledOnce()
  expect(screen.queryByText('Deep Research resources')).not.toBeInTheDocument()
  expect(screen.queryByText(/Three read-only research units/)).not.toBeInTheDocument()
  const signal = vi.mocked(getRunResources).mock.calls[0][1]
  fireEvent.click(summary)
  await waitFor(() => expect(signal?.aborted).toBe(true))
  expect(screen.queryByText('Unlimited turns')).not.toBeInTheDocument()
})

it.each([false, true])('distinguishes configured Linux limits from verified enforcement (%s)', async (verified) => {
  vi.mocked(getRunResources).mockResolvedValue({ ...resources, limits: {
    ...resources.limits, worker: workerLimits, worker_kernel_limits_verified: verified,
    worker_output_bytes: 1024, worker_last_outcome: 'output_limit',
  } })
  render(<RunResourcesPanel run={run} onCancel={vi.fn()} />)
  await screen.findByText(verified
    ? 'Linux CPU, memory and process limits verified at startup.'
    : 'Linux resource limits are configured; enforcement has not been verified for this worker.')
  expect(value('Configured CPU limit')).toHaveTextContent('200%')
  expect(value('Configured memory limit')).toHaveTextContent('2048.0 MiB')
  expect(value('Configured process and thread limit')).toHaveTextContent('128')
  expect(value('Output limit')).toHaveTextContent('32.0 MiB')
  expect(value('Recorded worker output')).toHaveTextContent('1.0 KiB')
  expect(screen.getByText('Output limit reached')).toBeInTheDocument()
})

it('shows only native limits and preserves unknown output totals in Chinese', async () => {
  vi.mocked(getRunResources).mockResolvedValue({ ...resources, limits: {
    ...resources.limits, worker: { ...workerLimits, mode: 'native' },
    worker_output_bytes: 800, worker_output_exhausted: true, worker_last_outcome: 'unconfirmed',
  } })
  render(<I18nContext.Provider value="zh-CN"><RunResourcesPanel run={run} onCancel={vi.fn()} /></I18nContext.Provider>)
  await screen.findByText('已启用时长和输出限制；CPU、内存与进程数限制需要 Linux 隔离环境。')
  expect(screen.queryByText('配置的 CPU 上限')).not.toBeInTheDocument()
  expect(value('已记录进程输出')).toHaveTextContent('800 B')
  expect(screen.getByText('上次输出总量无法确认，本次运行不能续接，请新建运行。')).toBeInTheDocument()
})
