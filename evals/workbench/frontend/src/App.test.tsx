import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter, useLocation } from 'react-router-dom'
import App from './App'

const experiment = (id: string) => ({
  id, name: `Archive ${id}`, provider: null, generated_at: null, case_count: 0,
  source: { path: `reports/${id}/overall.json`, sha256: null, status: 'available' },
  counts: { completed: 0, snapshot_accepted: 0, snapshot_rejected: 0, snapshot_unknown: 0, items: 0, labeled: 0 },
})
const fetchMock = vi.fn(async (path: string) => {
  const payload = path === '/api/acciowork/experiments' ? [experiment('baseline'), experiment('candidate')]
    : path === '/api/acciowork/traces' ? []
      : path === '/api/acciowork/review' ? { judges: {}, items: [] }
        : path.startsWith('/api/acciowork/compare?') ? { cases: [] }
          : undefined
  if (!payload) throw new Error(`Unexpected API request: ${path}`)
  return Response.json(payload)
})

function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{location.pathname}{location.search}{location.hash}</output>
}

function open(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><App /><LocationProbe /></MemoryRouter>)
}

beforeEach(() => { vi.stubGlobal('fetch', fetchMock) })
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('standalone evaluation routes', () => {
  it('opens the independent experiment list at the root with only project navigation', async () => {
    open('/')
    await screen.findByText('Archive baseline')
    expect(screen.getByTestId('location').textContent).toBe('/experiments')
    const links = within(screen.getByRole('navigation', { name: '评测导航' })).getAllByRole('link')
    expect(links.map(link => link.getAttribute('href'))).toEqual(['/experiments', '/traces', '/review', '/compare'])
    expect(screen.getByRole('link', { name: 'Archive baseline' }).getAttribute('href')).toBe('/experiments/baseline')
    expect(fetchMock.mock.calls.map(([path]) => path).every(path => path.startsWith('/api/acciowork/'))).toBe(true)
  })

  it.each([
    ['/traces', 'Trace', '没有符合条件的运行。'],
    ['/review', '人工复核', '没有符合筛选条件的陈述。'],
    ['/compare', '实验对比', '选择两个实验，逐 Case 打开两侧 Trace。'],
  ])('supports direct entry to %s', async (path, title, readyText) => {
    open(path)
    await screen.findByText(readyText)
    expect(screen.getByRole('heading', { name: title, level: 1 })).toBeTruthy()
    expect(screen.getByTestId('location').textContent).toBe(path)
  })

  it('preserves selected experiments and anchors when opening a legacy project URL', async () => {
    open('/acciowork/compare?left=baseline&right=candidate#case-b')
    await waitFor(() => expect(screen.getByTestId('location').textContent).toBe('/compare?left=baseline&right=candidate#case-b'))
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith('/api/acciowork/compare?left=baseline&right=candidate', undefined))
    expect((screen.getByRole('combobox', { name: '基线实验' }) as HTMLSelectElement).value).toBe('baseline')
    expect((screen.getByRole('combobox', { name: '对照实验' }) as HTMLSelectElement).value).toBe('candidate')
  })

  it('shows import instructions in this repository when the archive is empty', async () => {
    fetchMock.mockImplementationOnce(async () => Response.json([]))
    open('/experiments')
    await screen.findByText(/evals\/workbench\/README\.md/)
  })
})
