import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { vi } from 'vitest'
import { searchProjectSessions } from '../api/client'
import { I18nContext } from '../i18n'
import SessionSearchPanel from './SessionSearchPanel'

vi.mock('../api/client', () => ({ searchProjectSessions: vi.fn() }))

describe('project session search', () => {
  it('searches only the selected project and opens the matching Run', async () => {
    const onSelectRun = vi.fn()
    vi.mocked(searchProjectSessions).mockResolvedValue({
      project_id: 'project_1', query: 'ceramic', observed_at: '2026-09-23T00:00:00Z',
      total_runs: 3, scanned_runs: 3, skipped_event_logs: 0, truncated: false, next_cursor: null,
      matches: [{ run_id: 'run_1', status: 'completed', created_at: '2026-09-22T00:00:00Z',
        source: 'result', excerpt: 'Ceramic supplier shortlist', event_sequence: 7 }],
    })
    render(<I18nContext.Provider value="en">
      <SessionSearchPanel projectId="project_1" onSelectRun={onSelectRun} />
    </I18nContext.Provider>)
    fireEvent.click(screen.getByText('Search past conversations'))
    fireEvent.change(screen.getByLabelText('Search this project'), { target: { value: 'ceramic' } })
    fireEvent.click(within(screen.getByRole('search')).getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(screen.getByText('Ceramic supplier shortlist')).toBeVisible())
    expect(searchProjectSessions).toHaveBeenCalledWith('project_1', 'ceramic', expect.any(AbortSignal), undefined)
    fireEvent.click(screen.getByText('Ceramic supplier shortlist'))
    expect(onSelectRun).toHaveBeenCalledWith('run_1')
  })

  it('reports incomplete coverage rather than claiming no history exists', async () => {
    vi.mocked(searchProjectSessions).mockResolvedValue({
      project_id: 'project_1', query: 'missing', observed_at: '2026-09-23T00:00:00Z',
      total_runs: 500, scanned_runs: 8, skipped_event_logs: 1, truncated: true, next_cursor: null, matches: [],
    })
    render(<SessionSearchPanel projectId="project_1" onSelectRun={vi.fn()} />)
    fireEvent.click(screen.getByText('Search past conversations'))
    fireEvent.change(screen.getByLabelText('Search this project'), { target: { value: 'missing' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(screen.getByText('Some event logs could not be searched. Results may be incomplete.')).toBeVisible())
  })

  it('labels a matched user direction as a message in the Run', async () => {
    vi.mocked(searchProjectSessions).mockResolvedValue({
      project_id: 'project_1', query: 'freight', observed_at: '2026-09-23T00:00:00Z',
      total_runs: 1, scanned_runs: 1, skipped_event_logs: 0, truncated: false, next_cursor: null,
      matches: [{ run_id: 'run_1', status: 'completed', created_at: '2026-09-22T00:00:00Z',
        source: 'user_message', excerpt: 'Compare freight rates', event_sequence: 4 }],
    })
    render(<I18nContext.Provider value="en">
      <SessionSearchPanel projectId="project_1" onSelectRun={vi.fn()} />
    </I18nContext.Provider>)
    fireEvent.click(screen.getByText('Search past conversations'))
    fireEvent.change(screen.getByLabelText('Search this project'), { target: { value: 'freight' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    expect(await screen.findByText(/User message/)).toBeVisible()
  })

  it('continues into older pages and clears prior matches when the query changes', async () => {
    vi.mocked(searchProjectSessions).mockReset()
      .mockResolvedValueOnce({
        project_id: 'project_1', query: 'ceramic', observed_at: '2026-09-23T00:00:00Z',
        total_runs: 3, scanned_runs: 2, skipped_event_logs: 0, truncated: true,
        next_cursor: 'older-cursor',
        matches: [{ run_id: 'run_new', status: 'completed', created_at: '2026-09-23T00:00:00Z',
          source: 'prompt', excerpt: 'New ceramic prompt', event_sequence: null }],
      })
      .mockResolvedValueOnce({
        project_id: 'project_1', query: 'ceramic', observed_at: '2026-09-23T00:00:01Z',
        total_runs: 3, scanned_runs: 1, skipped_event_logs: 0, truncated: false,
        next_cursor: null,
        matches: [{ run_id: 'run_old', status: 'completed', created_at: '2026-09-22T00:00:00Z',
          source: 'result', excerpt: 'Old ceramic result', event_sequence: 4 }],
      })
    const onSelectRun = vi.fn()
    render(<SessionSearchPanel projectId="project_1" onSelectRun={onSelectRun} />)
    fireEvent.click(screen.getByText('Search past conversations'))
    fireEvent.change(screen.getByLabelText('Search this project'), { target: { value: 'ceramic' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Search older runs' }))
    expect(await screen.findByText('Old ceramic result')).toBeVisible()
    expect(screen.getByText('New ceramic prompt')).toBeVisible()
    expect(searchProjectSessions).toHaveBeenLastCalledWith(
      'project_1', 'ceramic', expect.any(AbortSignal), 'older-cursor',
    )
    fireEvent.click(screen.getByText('Old ceramic result'))
    expect(onSelectRun).toHaveBeenCalledWith('run_old')
    fireEvent.change(screen.getByLabelText('Search this project'), { target: { value: 'other' } })
    expect(screen.queryByText('Old ceramic result')).not.toBeInTheDocument()
  })
})
