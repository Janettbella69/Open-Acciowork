import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { listTasks } from '../api/client'
import type { TaskOverview } from '../api/taskOverview'
import { I18nContext } from '../i18n'
import { deferredPromise } from '../test/scopedEventFixtures'
import TaskOverviewPage from './TaskOverviewPage'

vi.mock('../api/client', () => ({
  listTasks: vi.fn(),
  errorMessage: (cause: unknown) => cause instanceof Error ? cause.message : 'Unknown error',
}))

const overview: TaskOverview = {
  observed_at: '2026-09-23T00:00:00Z', total_visible_runs: 2, truncated: false,
  rows: [
    {
      run_id: 'run_first', project_id: 'project_first', project_name: 'Supplier project',
      prompt: 'Find ceramics suppliers', status: 'waiting_permission', created_at: '2026-09-23T00:00:00Z',
      pending_approvals: 2, max_budget_usd: 10, sdk_budget_usd: 80,
      estimated_cost_usd: 1.2356,
      result_preview: null, observations_available: true,
    },
    {
      run_id: 'run_second', project_id: 'project_second', project_name: 'Listing project',
      prompt: 'Review listing', status: 'completed', created_at: '2026-09-22T00:00:00Z',
      pending_approvals: 0, max_budget_usd: null, sdk_budget_usd: null,
      estimated_cost_usd: null,
      result_preview: 'Three revisions recommended.', observations_available: true,
    },
  ],
}

beforeEach(() => { vi.resetAllMocks() })
afterEach(() => { cleanup(); vi.useRealTimers() })

describe('task overview', () => {
  it('shows independent runs, approval counts, budgets, and exact run links', async () => {
    vi.mocked(listTasks).mockResolvedValue(overview)
    render(<TaskOverviewPage />)
    const first = await screen.findByRole('link', { name: /Find ceramics suppliers/ })
    expect(first).toHaveAttribute('href', '/app?project=project_first&run=run_first')
    expect(within(first).getByText('Pending approvals: 2')).toBeVisible()
    expect(within(first).getByText('SDK estimated cost: $1.2356')).toBeVisible()
    expect(within(first).getByText('Run budget: $10.00')).toBeVisible()
    expect(within(first).getByText('SDK budget threshold: $80.00')).toBeVisible()
    expect(screen.getByText('Three revisions recommended.').closest('a')).toHaveAttribute('href', '/app?project=project_second&run=run_second')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(listTasks).toHaveBeenCalledTimes(2))
  })

  it('reports bounded coverage instead of hiding older tasks', async () => {
    vi.mocked(listTasks).mockResolvedValue({ ...overview, truncated: true })
    render(<TaskOverviewPage />)
    expect(await screen.findByRole('status')).toHaveTextContent('Showing recent runs; some observations are unavailable.')
    expect(screen.getByText('Find ceramics suppliers')).toBeVisible()
  })

  it('localizes a task awaiting approval', async () => {
    vi.mocked(listTasks).mockResolvedValue(overview)
    render(<I18nContext.Provider value="zh-CN"><TaskOverviewPage /></I18nContext.Provider>)
    const first = await screen.findByRole('link', { name: /Find ceramics suppliers/ })
    expect(within(first).getByText('等待授权')).toBeVisible()
  })

  it('shares slow reads between polling and manual refresh, then applies each completed snapshot', async () => {
    vi.useFakeTimers()
    const first = deferredPromise<TaskOverview>()
    const second = deferredPromise<TaskOverview>()
    vi.mocked(listTasks)
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise)
    render(<TaskOverviewPage />)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await act(async () => vi.advanceTimersByTimeAsync(10_000))
    expect(listTasks).toHaveBeenCalledTimes(1)
    await act(async () => first.resolve(overview))
    expect(screen.getByText('Find ceramics suppliers')).toBeVisible()
    expect(screen.queryByText('Loading tasks…')).not.toBeInTheDocument()

    await act(async () => vi.advanceTimersByTimeAsync(5_000))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await act(async () => vi.advanceTimersByTimeAsync(10_000))
    expect(listTasks).toHaveBeenCalledTimes(2)
    await act(async () => second.resolve({ ...overview, rows: [{ ...overview.rows[0], prompt: 'Newer result' }] }))
    expect(screen.getByText('Newer result')).toBeVisible()
    expect(screen.queryByText('Find ceramics suppliers')).not.toBeInTheDocument()
  })

  it.each([401, 403])('clears task prompts and results after HTTP %s and allows an authorized refresh', async (status) => {
    const denied = deferredPromise<TaskOverview>()
    vi.mocked(listTasks).mockResolvedValueOnce(overview).mockReturnValueOnce(denied.promise)
      .mockResolvedValueOnce({ ...overview, rows: [] })
    render(<TaskOverviewPage />)
    await screen.findByText('Three revisions recommended.')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    await act(async () => denied.reject(Object.assign(new Error('Task access denied'), { status })))
    expect(screen.getByRole('alert')).toHaveTextContent('Task access denied')
    expect(screen.queryByText('Three revisions recommended.')).not.toBeInTheDocument()
    expect(screen.queryByText('Find ceramics suppliers')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Listing project/ })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('No tasks yet. Start a new task to see it here.')).toBeVisible()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('retains the last snapshot during a temporary network failure', async () => {
    vi.mocked(listTasks).mockResolvedValueOnce(overview).mockRejectedValueOnce(new Error('Network unavailable'))
    render(<TaskOverviewPage />)
    await screen.findByText('Three revisions recommended.')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Network unavailable')
    expect(screen.getByText('Three revisions recommended.')).toBeVisible()
  })

  it('aborts a manual refresh on unmount and ignores its late response after remounting', async () => {
    const old = deferredPromise<TaskOverview>()
    vi.mocked(listTasks).mockResolvedValueOnce(overview).mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce({ ...overview, rows: [{ ...overview.rows[0], prompt: 'Current account task' }] })
    const view = render(<TaskOverviewPage />)
    await screen.findByText('Three revisions recommended.')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    const signal = vi.mocked(listTasks).mock.calls[1][0]!
    view.unmount()
    expect(signal.aborted).toBe(true)

    render(<TaskOverviewPage />)
    await screen.findByText('Current account task')
    await act(async () => old.resolve(overview))
    expect(screen.getByText('Current account task')).toBeVisible()
    expect(screen.queryByText('Three revisions recommended.')).not.toBeInTheDocument()
  })
})
