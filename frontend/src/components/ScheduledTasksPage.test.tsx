import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { OpportunityProject, RunRecord, Schedule } from '../api/types'
import { I18nContext } from '../i18n'
import ScheduledTasksPage from './ScheduledTasksPage'
import { useScopedEvents } from '../api/useScopedEvents'
import { deferredPromise, scopedEvent } from '../test/scopedEventFixtures'

vi.mock('../api/useScopedEvents', () => ({ useScopedEvents: vi.fn(() => ({ events: [], status: 'live', error: '', refresh: vi.fn() })) }))

vi.mock('./EventTriggersPanel', () => ({ default: () => null }))

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
  resumeSchedule,
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

const activeRunConflict = 'This schedule already has an active run. Wait for it to finish or cancel it in the workspace before running again.'
const dispatchConflict = 'This schedule is already being dispatched. Refresh before running again.'
let storedSchedules: Schedule[]

beforeEach(() => {
  vi.clearAllMocks()
  storedSchedules = [schedule]
  vi.mocked(listSchedules).mockImplementation(async () => storedSchedules)
  vi.mocked(listProjects).mockResolvedValue([project])
  vi.mocked(getRun).mockResolvedValue(failedRun)
  vi.mocked(createSchedule).mockImplementation(async (input) => {
    const created = {
    ...schedule,
    id: 'schedule_new',
    prompt: input.prompt,
    cron: input.cron ?? null,
    timezone: input.timezone,
    kind: input.kind,
    permission_ceiling: input.permission_ceiling,
    last_run_id: null,
    last_fire_at: null,
    }
    storedSchedules = [created, ...storedSchedules]
    return created
  })
  vi.mocked(pauseSchedule).mockImplementation(async (id) => {
    const updated = { ...schedule, id, enabled: false }
    storedSchedules = storedSchedules.map((item) => item.id === id ? updated : item)
    return updated
  })
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

    const createdRow = (await screen.findByText(/prepare a morning opportunity brief/i, {
      selector: 'article strong',
    })).closest('article')!
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

  it.each([
    ['en', activeRunConflict, activeRunConflict],
    ['en', dispatchConflict, dispatchConflict],
    ['zh-CN', activeRunConflict, '此任务已有运行正在进行。请等待其结束，或先在工作台取消后再运行。'],
    ['zh-CN', dispatchConflict, '此任务正在派发。请刷新后再运行。'],
  ] as const)('shows %s manual-run conflicts without stale success or cadence changes: %s', async (language, detail, expected) => {
    vi.mocked(listSchedules).mockResolvedValue([{ ...schedule, enabled: false, last_run_id: null }])
    render(<I18nContext.Provider value={language}><ScheduledTasksPage /></I18nContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: language === 'en' ? /my tasks/i : /我的任务/ }))
    const row = (await screen.findByText(schedule.prompt)).closest('article')!
    const button = within(row).getByRole('button', { name: language === 'en' ? 'Run now' : '立即运行' })
    fireEvent.click(button)
    expect(await screen.findByRole('status')).toHaveTextContent('run_manual')

    vi.mocked(runSchedule).mockRejectedValueOnce(new Error(detail))
    fireEvent.click(button)
    expect(await screen.findByRole('alert')).toHaveTextContent(expected)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(runSchedule).toHaveBeenCalledTimes(2)
    expect(button).toBeEnabled()
    expect(row).toHaveAttribute('data-enabled', 'false')
    expect(within(row).getByText(/0 8 \* \* \*/)).toBeInTheDocument()
    expect(within(row).getByText(/Asia\/Shanghai/)).toBeInTheDocument()
    expect(pauseSchedule).not.toHaveBeenCalled()
  })

  it('keeps each pending schedule locked and ignores a late success after another task conflicts', async () => {
    const second = { ...schedule, id: 'schedule_2', prompt: 'Second scheduled task', last_run_id: null }
    vi.mocked(listSchedules).mockResolvedValue([{ ...schedule, last_run_id: null }, second])
    let resolveFirst!: (run: RunRecord) => void
    vi.mocked(runSchedule).mockImplementation((id) => id === schedule.id
      ? new Promise((resolve) => { resolveFirst = resolve })
      : Promise.reject(new Error(dispatchConflict)))
    render(<ScheduledTasksPage />)
    fireEvent.click(screen.getByRole('button', { name: /my tasks/i }))
    const firstRow = (await screen.findByText(schedule.prompt)).closest('article')!
    const secondRow = screen.getByText(second.prompt).closest('article')!
    const firstButton = within(firstRow).getByRole('button', { name: 'Run now' })
    fireEvent.click(firstButton)
    expect(firstButton).toBeDisabled()
    expect(within(firstRow).getByRole('button', { name: 'Pause' })).toBeDisabled()
    fireEvent.click(within(secondRow).getByRole('button', { name: 'Run now' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(dispatchConflict)
    expect(firstButton).toBeDisabled()
    fireEvent.click(firstButton)
    expect(runSchedule).toHaveBeenCalledTimes(2)

    await act(async () => { resolveFirst({ ...failedRun, id: 'late_run', status: 'queued' }) })
    expect(firstButton).toBeEnabled()
    expect(screen.getByRole('alert')).toHaveTextContent(dispatchConflict)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(runSchedule).toHaveBeenCalledTimes(2)
  })
})

describe('durable schedule activity', () => {
  it('shows a skipped reason without a Run and lets users pause live updates', async () => {
    const event = scopedEvent(1, { type: 'schedule.skipped', data: { schedule_id: 'schedule_1', reason: 'active_run' } })
    vi.mocked(useScopedEvents).mockReturnValue({ events: [event], status: 'live', error: '', refresh: vi.fn() })
    render(<ScheduledTasksPage />)
    await screen.findByText('Connected · server-side execution')
    fireEvent.click(screen.getByRole('button', { name: /My tasks/ }))
    expect(await screen.findByText('Skipped')).toBeVisible()
    expect(screen.getByText(/A previous run is still active/)).toBeVisible()
    expect(screen.queryByRole('link', { name: 'Open run' })).not.toBeInTheDocument()
    expect(vi.mocked(useScopedEvents).mock.calls.at(-1)![0]).toMatchObject({ scope: 'project', id: project.id, live: true })
    fireEvent.click(screen.getByRole('checkbox', { name: 'Live updates' }))
    expect(vi.mocked(useScopedEvents).mock.calls.at(-1)![0]).toMatchObject({ live: false })
    vi.mocked(useScopedEvents).mockReturnValue({ events: [], status: 'live', error: '', refresh: vi.fn() })
  })

  it('refreshes scheduled tasks on control events and clears a revoked project', async () => {
    render(<ScheduledTasksPage />)
    await screen.findByText('Connected · server-side execution')
    fireEvent.click(screen.getByRole('button', { name: /My tasks/ }))
    const options = vi.mocked(useScopedEvents).mock.calls.at(-1)![0]
    await act(async () => options.onEvent?.(scopedEvent(1, { type: 'schedule.completed', data: { schedule_id: schedule.id } })))
    expect(listSchedules).toHaveBeenCalledTimes(2)
    act(() => options.onUnavailable?.())
    expect(screen.queryByText(schedule.prompt)).not.toBeInTheDocument()
    expect(screen.getByText(/Its schedule history has been cleared/)).toBeVisible()
  })
})

it('subscribes to one selected project history at a time', async () => {
  vi.mocked(listProjects).mockResolvedValue([project, { ...project, id: 'project_2', name: 'Second project' }])
  vi.mocked(listSchedules).mockResolvedValue([schedule, { ...schedule, id: 'schedule_2', project_id: 'project_2' }])
  render(<ScheduledTasksPage />)
  await screen.findByText('Connected · server-side execution')
  fireEvent.click(screen.getByRole('button', { name: /My tasks/ }))
  expect(vi.mocked(useScopedEvents).mock.calls.every(([options]) => options.id === 'project_1')).toBe(true)
  fireEvent.change(screen.getByRole('combobox', { name: 'Schedule history project' }), { target: { value: 'project_2' } })
  expect(vi.mocked(useScopedEvents).mock.calls.at(-1)![0]).toMatchObject({ scope: 'project', id: 'project_2' })
  expect(screen.queryByRole('region', { name: `Recent schedule activity for ${project.name}` })).not.toBeInTheDocument()
  expect(screen.getByRole('region', { name: 'Recent schedule activity for Second project' })).toBeVisible()
})

it('does not restore a revoked project from a late schedule refresh', async () => {
  let finishRefresh!: (items: Schedule[]) => void
  render(<ScheduledTasksPage />)
  await screen.findByText('Connected · server-side execution')
  fireEvent.click(screen.getByRole('button', { name: /My tasks/ }))
  const options = vi.mocked(useScopedEvents).mock.calls.at(-1)![0]
  vi.mocked(listSchedules).mockImplementationOnce(() => new Promise((resolve) => { finishRefresh = resolve }))
  act(() => options.onEvent?.(scopedEvent(1, { type: 'schedule.completed', data: { schedule_id: schedule.id } })))
  act(() => options.onUnavailable?.())
  await act(async () => finishRefresh([schedule]))
  expect(screen.queryByText(schedule.prompt)).not.toBeInTheDocument()
  expect(screen.queryByRole('combobox', { name: 'Schedule history project' })).not.toBeInTheDocument()
})

describe('schedule writes and event-driven reads', () => {
  it.each(['pause', 'resume'] as const)('does not overwrite a committed %s with a late event refresh', async (action) => {
    const initial = { ...schedule, enabled: action === 'pause' }
    const updated = { ...initial, enabled: !initial.enabled }
    const oldRead = deferredPromise<Schedule[]>()
    const write = deferredPromise<Schedule>()
    const freshRead = deferredPromise<Schedule[]>()
    vi.mocked(listSchedules).mockResolvedValueOnce([initial])
      .mockReturnValueOnce(oldRead.promise).mockReturnValue(freshRead.promise)
    vi.mocked(action === 'pause' ? pauseSchedule : resumeSchedule).mockReturnValueOnce(write.promise)
    render(<ScheduledTasksPage />)
    fireEvent.click(screen.getByRole('button', { name: /my tasks/i }))
    const row = (await screen.findByText(schedule.prompt)).closest('article')!
    const options = vi.mocked(useScopedEvents).mock.calls.at(-1)![0]
    act(() => options.onReady?.())
    expect(listSchedules).toHaveBeenCalledTimes(2)
    fireEvent.click(within(row).getByRole('button', { name: action === 'pause' ? 'Pause' : 'Resume' }))
    act(() => options.onEvent?.(scopedEvent(1, { type: 'schedule.fired', data: { schedule_id: schedule.id } })))
    expect(listSchedules).toHaveBeenCalledTimes(2)
    await act(async () => write.resolve(updated))
    expect(row).toHaveAttribute('data-enabled', String(updated.enabled))
    expect(listSchedules).toHaveBeenCalledTimes(3)
    await act(async () => oldRead.resolve([initial]))
    expect(row).toHaveAttribute('data-enabled', String(updated.enabled))
    await act(async () => freshRead.resolve([updated]))
    expect(row).toHaveAttribute('data-enabled', String(updated.enabled))
  })

  it('keeps a newly created schedule when an older list finishes late', async () => {
    const oldRead = deferredPromise<Schedule[]>()
    const write = deferredPromise<Schedule>()
    const freshRead = deferredPromise<Schedule[]>()
    const created = { ...schedule, id: 'schedule_new', prompt: 'New recurring task' }
    vi.mocked(listSchedules).mockResolvedValueOnce([schedule])
      .mockReturnValueOnce(oldRead.promise).mockReturnValue(freshRead.promise)
    vi.mocked(createSchedule).mockReturnValueOnce(write.promise)
    render(<ScheduledTasksPage />)
    await screen.findByText('Connected · server-side execution')
    fireEvent.click(screen.getByRole('button', { name: /my tasks/i }))
    const options = vi.mocked(useScopedEvents).mock.calls.at(-1)![0]
    act(() => options.onReady?.())
    fireEvent.click(screen.getByRole('button', { name: /new task/i }))
    const dialog = screen.getByRole('dialog', { name: /create scheduled task/i })
    fireEvent.change(within(dialog).getByRole('textbox', { name: /recurring brief/i }), { target: { value: created.prompt } })
    fireEvent.click(within(dialog).getByRole('button', { name: /create schedule/i }))
    act(() => options.onReady?.())
    expect(listSchedules).toHaveBeenCalledTimes(2)
    await act(async () => write.resolve(created))
    expect(screen.getByText(created.prompt)).toBeVisible()
    await act(async () => oldRead.resolve([schedule]))
    expect(screen.getByText(created.prompt)).toBeVisible()
    expect(listSchedules).toHaveBeenCalledTimes(3)
    await act(async () => freshRead.resolve([created, schedule]))
    expect(screen.getByText(created.prompt)).toBeVisible()
  })

  it('waits for all concurrent schedule writes before reloading the list', async () => {
    const second = { ...schedule, id: 'schedule_2', prompt: 'Second schedule' }
    const firstWrite = deferredPromise<Schedule>()
    const secondWrite = deferredPromise<Schedule>()
    const freshRead = deferredPromise<Schedule[]>()
    vi.mocked(listSchedules).mockResolvedValueOnce([schedule, second]).mockReturnValue(freshRead.promise)
    vi.mocked(pauseSchedule).mockImplementation((id) => id === schedule.id ? firstWrite.promise : secondWrite.promise)
    render(<ScheduledTasksPage />)
    fireEvent.click(screen.getByRole('button', { name: /my tasks/i }))
    const firstRow = (await screen.findByText(schedule.prompt)).closest('article')!
    const secondRow = screen.getByText(second.prompt).closest('article')!
    fireEvent.click(within(firstRow).getByRole('button', { name: 'Pause' }))
    fireEvent.click(within(secondRow).getByRole('button', { name: 'Pause' }))
    const options = vi.mocked(useScopedEvents).mock.calls.at(-1)![0]
    act(() => options.onReady?.())
    await act(async () => firstWrite.resolve({ ...schedule, enabled: false }))
    expect(listSchedules).toHaveBeenCalledTimes(1)
    expect(firstRow).toHaveAttribute('data-enabled', 'false')
    expect(within(secondRow).getByRole('button', { name: 'Pause' })).toBeDisabled()
    await act(async () => secondWrite.resolve({ ...second, enabled: false }))
    expect(listSchedules).toHaveBeenCalledTimes(2)
    await act(async () => freshRead.resolve([{ ...schedule, enabled: false }, { ...second, enabled: false }]))
    expect(firstRow).toHaveAttribute('data-enabled', 'false')
    expect(secondRow).toHaveAttribute('data-enabled', 'false')
  })
})
