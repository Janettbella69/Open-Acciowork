import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { CompanionOverview, CompanionSession } from '../api/companion'
import type { OpportunityProject, RunRecord } from '../api/types'
import MobileCompanion from './MobileCompanion'
import {
  getCompanionOverview, getCompanionSession, interjectCompanionRun,
  startCompanionRun,
} from '../api/companion'

vi.mock('../api/companion', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api/companion')>()
  return {
    ...original,
    getCompanionSession: vi.fn(),
    getCompanionOverview: vi.fn(),
    interjectCompanionRun: vi.fn(),
    startCompanionRun: vi.fn(),
  }
})
vi.mock('./CompanionPushControls', () => ({ default: () => null }))

const run: RunRecord = {
  id: 'run_1', trace_id: 'trace_1', project_id: 'project_1', prompt: 'Research shipping',
  agent_id: null, mode: 'standard', provider_profile_id: null, provider_snapshot: null,
  skill_snapshot: [], status: 'running', sdk_session_id: 'session_1', error: null,
  error_code: null, created_at: '2026-09-24T00:00:00Z', started_at: '2026-09-24T00:00:01Z',
  finished_at: null,
}
const device = {
  id: 'pair_123', status: 'approved' as const, project_ids: ['project_1'],
  allow_run_start: true, created_at: '2026-09-24T00:00:00Z',
  expires_at: '2026-10-24T00:00:00Z', device_name: 'Phone',
  verification_code: null, approved_at: '2026-09-24T00:00:00Z',
}
const session: CompanionSession = { status: 'approved', device }
const overview: CompanionOverview = {
  device, projects: [], pending_permissions: [], notifications: [], runs: [run],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getCompanionSession).mockResolvedValue(session)
  vi.mocked(getCompanionOverview).mockResolvedValue(overview)
  vi.mocked(interjectCompanionRun).mockResolvedValue({
    request_id: 'message_1', run_id: run.id, prompt: 'Check freight rates',
    mode: 'next_turn', status: 'queued', next_run_id: null,
  })
})

afterEach(() => vi.unstubAllGlobals())

it('lets an approved phone queue a direction for an active run', async () => {
  render(<MobileCompanion initialSession={session} entry={{ kind: 'none' }} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Direct this run' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'Direction' }), {
    target: { value: 'Check freight rates' },
  })
  fireEvent.change(screen.getByRole('combobox', { name: 'Delivery' }), {
    target: { value: 'next_turn' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Send direction' }))
  await waitFor(() => expect(interjectCompanionRun).toHaveBeenCalledWith(
    run.id, expect.any(String), 'Check freight rates', 'next_turn',
  ))
  expect(await screen.findByRole('status', { name: '' })).toHaveTextContent(
    'Direction saved for the next turn.',
  )
})

it('waits for the user to confirm voice text and submit before starting a run', async () => {
  class FakeRecognition {
    static current: FakeRecognition
    lang = ''
    continuous = false
    interimResults = false
    onresult: ((event: { results: { isFinal: boolean; 0: { transcript: string } }[] }) => void) | null = null
    onerror: ((event: { error: string }) => void) | null = null
    onend: (() => void) | null = null
    start = vi.fn()
    stop = vi.fn(() => this.onend?.())
    abort = vi.fn()
    constructor() { FakeRecognition.current = this }
  }
  vi.stubGlobal('webkitSpeechRecognition', FakeRecognition)
  vi.mocked(getCompanionOverview).mockResolvedValue({
    ...overview,
    projects: [{ id: 'project_1', name: 'Shop', objective: 'Review orders' } as OpportunityProject],
    runs: [],
  })
  vi.mocked(startCompanionRun).mockResolvedValue(run)
  render(<MobileCompanion initialSession={session} entry={{ kind: 'none' }} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Dictate message' }))
  act(() => FakeRecognition.current.onresult?.({ results: [
    { isFinal: true, 0: { transcript: 'Review the new order' } },
  ] }))
  fireEvent.click(screen.getByRole('button', { name: 'Stop dictation' }))
  expect(startCompanionRun).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Use reviewed text' }))
  expect(screen.getByRole('textbox', { name: 'Prompt' })).toHaveValue('Review the new order')
  expect(startCompanionRun).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Start run' }))
  await waitFor(() => expect(startCompanionRun).toHaveBeenCalledWith(
    'project_1', 'Review the new order',
  ))
})
