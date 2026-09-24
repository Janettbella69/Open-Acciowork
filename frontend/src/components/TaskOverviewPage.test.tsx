import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { listTasks } from '../api/client'
import type { TaskOverview } from '../api/taskOverview'
import { I18nContext } from '../i18n'
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
      pending_approvals: 2, max_budget_usd: 10, estimated_cost_usd: 1.2356,
      result_preview: null, observations_available: true,
    },
    {
      run_id: 'run_second', project_id: 'project_second', project_name: 'Listing project',
      prompt: 'Review listing', status: 'completed', created_at: '2026-09-22T00:00:00Z',
      pending_approvals: 0, max_budget_usd: null, estimated_cost_usd: null,
      result_preview: 'Three revisions recommended.', observations_available: true,
    },
  ],
}

beforeEach(() => { vi.clearAllMocks() })

describe('task overview', () => {
  it('shows independent runs, approval counts, budgets, and exact run links', async () => {
    vi.mocked(listTasks).mockResolvedValue(overview)
    render(<TaskOverviewPage />)
    const first = await screen.findByRole('link', { name: /Find ceramics suppliers/ })
    expect(first).toHaveAttribute('href', '/app?project=project_first&run=run_first')
    expect(within(first).getByText('Pending approvals: 2')).toBeVisible()
    expect(within(first).getByText('SDK estimated cost: $1.2356')).toBeVisible()
    expect(within(first).getByText('Run budget: $10.00')).toBeVisible()
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

  it('keeps the newest refresh when an older request finishes later', async () => {
    let finishOld!: (value: TaskOverview) => void
    vi.mocked(listTasks)
      .mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve }))
      .mockResolvedValueOnce({ ...overview, rows: [{ ...overview.rows[0], prompt: 'Newer result' }] })
    render(<TaskOverviewPage />)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('Newer result')).toBeVisible()
    await act(async () => { finishOld(overview) })
    expect(screen.getByText('Newer result')).toBeVisible()
    expect(screen.queryByText('Find ceramics suppliers')).not.toBeInTheDocument()
  })
})
