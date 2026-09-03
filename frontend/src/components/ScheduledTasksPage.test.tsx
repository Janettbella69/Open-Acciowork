import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { OpportunityProject, RunRecord, Schedule } from '../api/types'
import { I18nContext } from '../i18n'
import ScheduledTasksPage from './ScheduledTasksPage'

vi.mock('../api/client', () => ({
  listSchedules: vi.fn(),
  createSchedule: vi.fn(),
  pauseSchedule: vi.fn(),
  resumeSchedule: vi.fn(),
  runSchedule: vi.fn(),
  listProjects: vi.fn(),
  getRun: vi.fn(),
  errorMessage: (error: unknown) => (error instanceof Error ? error.message : 'unknown error'),
}))

import {
  createSchedule,
  getRun,
  listProjects,
  listSchedules,
  pauseSchedule,
  runSchedule,
} from '../api/client'

const project = {
  id: 'project_1',
  name: 'EU kettle validation',
  objective: 'Validate EU demand',
  product_brief: '',
  target_markets: [],
  constraints: [],
  stage: 'discovery',
  hypotheses: [],
  evidence: [],
  assessments: {},
  stage_decisions: [],
  decisions: [],
  artifacts: [],
  revision: 1,
  created_at: '2026-08-20T08:00:00Z',
  updated_at: '2026-08-20T08:00:00Z',
} as unknown as OpportunityProject

const schedule: Schedule = {
  id: 'schedule_1',
  project_id: 'project_1',
  provider_profile_id: 'deepseek',
  mode: 'standard',
  kind: 'heartbeat',
  prompt: 'Monitor competitor pricing and report changes.',
  cron: '0 8 * * *',
  run_once_at: null,
  timezone: 'Asia/Shanghai',
  permission_ceiling: [
    { tool_name: 'Write', scope: 'path:.' },
    { tool_name: 'Edit', scope: 'path:.' },
  ],
  enabled: true,
  next_fire_at: '2026-08-21T00:00:00Z',
  last_fire_at: '2026-08-20T00:00:00Z',
  last_run_id: 'run_9',
  created_at: '2026-08-19T00:00:00Z',
}

const failedRun = {
  id: 'run_9',
  trace_id: 'trace_9',
  project_id: 'project_1',
  prompt: schedule.prompt,
  agent_id: null,
  mode: 'standard',
  provider_profile_id: 'deepseek',
  provider_snapshot: null,
  skill_snapshot: [],
  status: 'failed',
  sdk_session_id: null,
  error: '凭据无效或已撤销，请更新当前 Provider 的 Key 后重试。',
  error_code: 'AUTH_REVOKED_OR_INVALID',
  created_at: '2026-08-20T00:00:00Z',
  started_at: '2026-08-20T00:00:00Z',
  finished_at: '2026-08-20T00:01:00Z',
} as RunRecord

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listSchedules).mockResolvedValue([schedule])
  vi.mocked(listProjects).mockResolvedValue([project])
  vi.mocked(getRun).mockResolvedValue(failedRun)
  vi.mocked(createSchedule).mockImplementation(async (input) => ({
    ...schedule,
    id: 'schedule_new',
    prompt: input.prompt,
    cron: input.cron ?? null,
    timezone: input.timezone,
    kind: input.kind,
    permission_ceiling: input.permission_ceiling,
    last_run_id: null,
    last_fire_at: null,
  }))
  vi.mocked(pauseSchedule).mockResolvedValue({ ...schedule, enabled: false })
  vi.mocked(runSchedule).mockResolvedValue({ ...failedRun, id: 'run_manual', status: 'queued' })
})

describe('ScheduledTasksPage', () => {
  it('lists backend schedules with fire times and a visible failed last run', async () => {
    render(<ScheduledTasksPage />)
    fireEvent.click(screen.getByRole('button', { name: /my tasks/i }))

    const row = (await screen.findByText(/monitor competitor pricing/i)).closest('article')!
    expect(within(row).getByText(/0 8 \* \* \*/)).toBeInTheDocument()
    expect(within(row).getByText(/asia\/shanghai/i)).toBeInTheDocument()
    expect(within(row).getByText(/^heartbeat$/i)).toBeInTheDocument()
    const ceiling = within(row).getByRole('region', { name: /permission ceiling/i })
    expect(within(ceiling).getByText(/workspace file changes only/i)).toBeInTheDocument()
    expect(within(ceiling).getByText('Write')).toBeInTheDocument()
    expect(within(ceiling).getByText('Edit')).toBeInTheDocument()
    expect(within(ceiling).getAllByText(/project workspace \(path:\.\)/i)).toHaveLength(2)
    expect(await within(row).findByText(/last run failed/i)).toBeInTheDocument()
    expect(within(row).getByText(/AUTH_REVOKED_OR_INVALID/)).toBeInTheDocument()
    expect(getRun).toHaveBeenCalledWith('run_9')
  })

  it('creates a real schedule from a template with the mapped cron', async () => {
    render(<ScheduledTasksPage />)
    await screen.findByRole('button', { name: /morning opportunity brief/i })

    fireEvent.click(screen.getByRole('button', { name: /morning opportunity brief/i }))
    const dialog = screen.getByRole('dialog', { name: /create scheduled task/i })
    fireEvent.change(within(dialog).getByLabelText(/^project$/i), {
      target: { value: 'project_1' },
    })
    const readOnly = within(dialog).getByRole('radio', {
      name: /read-only analysis · no added action grants/i,
    })
    expect(readOnly).toBeChecked()
    expect(within(dialog).getByText(/delivery already authorized.*may still occur/i)).toBeInTheDocument()
    expect(within(dialog).getAllByRole('radio')).toHaveLength(2)
    fireEvent.click(within(dialog).getByRole('button', { name: /create schedule/i }))

    await waitFor(() => expect(createSchedule).toHaveBeenCalledTimes(1))
    const input = vi.mocked(createSchedule).mock.calls[0][0]
    expect(input.project_id).toBe('project_1')
    expect(input.cron).toBe('0 8 * * *')
    expect(input.mode).toBe('standard')
    expect(input.kind).toBe('task')
    expect(input.permission_ceiling).toEqual([])
    expect(typeof input.timezone).toBe('string')
    expect(input.prompt).toMatch(/morning opportunity brief/i)

    const createdRow = (await screen.findByText(/prepare a morning opportunity brief/i)).closest('article')!
    expect(within(createdRow).getByText(/read-only analysis · no added action grants/i)).toBeInTheDocument()
  })

  it('creates monitoring templates as heartbeat schedules', async () => {
    render(<ScheduledTasksPage />)
    const template = await screen.findByRole('button', { name: /competitor watch/i })

    fireEvent.click(template)
    const dialog = screen.getByRole('dialog', { name: /create scheduled task/i })
    expect(within(dialog).getByText(/heartbeat monitor/i)).toBeInTheDocument()
    fireEvent.change(within(dialog).getByLabelText(/^project$/i), {
      target: { value: 'project_1' },
    })
    fireEvent.click(within(dialog).getByRole('button', { name: /create schedule/i }))

    await waitFor(() => expect(createSchedule).toHaveBeenCalledTimes(1))
    expect(vi.mocked(createSchedule).mock.calls[0][0].kind).toBe('heartbeat')
  })

  it('requires an explicit choice for bounded Write and Edit workspace access', async () => {
    render(<ScheduledTasksPage />)
    await screen.findByRole('button', { name: /new task/i })

    fireEvent.click(screen.getByRole('button', { name: /new task/i }))
    const dialog = screen.getByRole('dialog', { name: /create scheduled task/i })
    fireEvent.change(within(dialog).getByRole('textbox', { name: /recurring brief/i }), {
      target: { value: 'Prepare a workspace report' },
    })
    fireEvent.change(within(dialog).getByLabelText(/^project$/i), {
      target: { value: 'project_1' },
    })
    fireEvent.click(within(dialog).getByRole('radio', {
      name: /workspace files · write and edit only/i,
    }))
    expect(within(dialog).getByText(/shell commands, deletion, browser actions, and new external actions/i)).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: /create schedule/i }))

    await waitFor(() => expect(createSchedule).toHaveBeenCalledTimes(1))
    expect(vi.mocked(createSchedule).mock.calls[0][0].permission_ceiling).toEqual([
      { tool_name: 'Write', scope: 'path:.' },
      { tool_name: 'Edit', scope: 'path:.' },
    ])
  })

  it('pauses and manually fires a schedule through the backend', async () => {
    render(<ScheduledTasksPage />)
    fireEvent.click(screen.getByRole('button', { name: /my tasks/i }))
    const row = (await screen.findByText(/monitor competitor pricing/i)).closest('article')!

    fireEvent.click(within(row).getByRole('button', { name: /pause/i }))
    await waitFor(() => expect(pauseSchedule).toHaveBeenCalledWith('schedule_1'))
    expect(await within(row).findByText(/paused/i)).toBeInTheDocument()
    expect(row).toHaveAttribute('data-enabled', 'false')
    expect(within(row).getByRole('button', { name: /resume/i })).toBeInTheDocument()

    fireEvent.click(within(row).getByRole('button', { name: /run now/i }))
    await waitFor(() => expect(runSchedule).toHaveBeenCalledWith('schedule_1'))
  })

  it('localizes a template prompt before the user schedules it', async () => {
    render(
      <I18nContext.Provider value="zh-CN">
        <ScheduledTasksPage />
      </I18nContext.Provider>,
    )
    const template = await screen.findByRole('button', { name: /晨间机会简报/ })

    fireEvent.click(template)

    const dialog = screen.getByRole('dialog', { name: '创建定时任务' })
    expect(within(dialog).getByRole('textbox', { name: '周期性需求' })).toHaveValue(
      '准备一份晨间机会简报，包含新信号、证据缺口和三个最高优先级行动。',
    )
  })
})
