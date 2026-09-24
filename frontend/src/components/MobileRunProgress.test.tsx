import { act, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import { getCompanionImagePreview, getCompanionRunActivity } from '../api/companion'
import type { OphiraEvent } from '../api/types'
import MobileRunProgress from './MobileRunProgress'

vi.mock('../api/companion', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api/companion')>()
  return {
    ...original,
    getCompanionRunActivity: vi.fn(),
    getCompanionImagePreview: vi.fn(),
  }
})

function event(sequence: number, type: string, data: Record<string, unknown>): OphiraEvent {
  return {
    id: `run_1:${sequence}`, sequence, type, run_id: 'run_1', project_id: 'project_1',
    trace_id: 'trace_1', sdk_session_id: null, provider_profile_id: null,
    timestamp: '2026-09-24T00:00:00Z', actor: { type: 'agent', name: 'main', agent_id: null },
    parent_id: null, tool_use_id: null, data,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:screenshot') })
  Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
  vi.mocked(getCompanionRunActivity).mockResolvedValueOnce({
    events: [
      event(1, 'tool.started', { tool_name: 'Read' }),
      event(2, 'tool.completed', {
        tool_name: 'TaskCreate', tool_input: { subject: 'Inspect page' },
        tool_response: { task: { id: 'task_1', subject: 'Inspect page' } },
      }),
      event(3, 'artifact.registered', { artifact_id: 'artifact_1' }),
    ],
    artifacts: [{
      id: 'artifact_1', project_id: 'project_1', path: 'screen.png', name: 'screen.png',
      mime: 'image/png', version: 1, run_id: 'run_1', source_ids: [], checksum: 'abc',
      size_bytes: 100, download_url: '/download', content_storage: 'snapshot',
      created_at: '2026-09-24T00:00:00Z', updated_at: '2026-09-24T00:00:00Z',
    }],
    next_sequence: 3, has_more: false, status: 'running',
  }).mockImplementation(() => new Promise(() => {}))
  vi.mocked(getCompanionImagePreview).mockResolvedValue(new Blob(['png'], { type: 'image/png' }))
})

it('shows current step, native task, artifact change, and verified image preview', async () => {
  const view = render(<MobileRunProgress runId="run_1" />)
  expect(await screen.findByText('Inspect page')).toBeVisible()
  expect(screen.getByText(/Current step:/).parentElement).toHaveTextContent('Read')
  expect(screen.getByText(/screen.png · Version 1/)).toBeVisible()
  expect(await screen.findByRole('img', { name: 'screen.png' })).toHaveAttribute('src', 'blob:screenshot')
  view.unmount()
  await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:screenshot'))
})

it('continues from the activity cursor with a waiting request and applies new steps', async () => {
  let deliver: ((value: Awaited<ReturnType<typeof getCompanionRunActivity>>) => void) | undefined
  vi.mocked(getCompanionRunActivity).mockImplementationOnce(() => new Promise((resolve) => {
    deliver = resolve
  }))
  // The first response comes from beforeEach; this second call waits for an event.
  const view = render(<MobileRunProgress runId="run_1" />)
  await waitFor(() => expect(getCompanionRunActivity).toHaveBeenCalledTimes(2))
  expect(getCompanionRunActivity).toHaveBeenNthCalledWith(2, 'run_1', 3, expect.anything(), true)
  await act(async () => {
    deliver?.({
      events: [event(4, 'tool.started', { tool_name: 'Search' })],
      artifacts: [], next_sequence: 4, has_more: false, status: 'running',
    })
  })
  expect(screen.getByText(/Current step:/).parentElement).toHaveTextContent('Search')
  view.unmount()
})
