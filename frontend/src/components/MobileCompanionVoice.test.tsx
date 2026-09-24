import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { CompanionOverview, CompanionSession } from '../api/companion'
import type { OpportunityProject, RunRecord } from '../api/types'
import MobileCompanion from './MobileCompanion'
import { getCompanionOverview, getCompanionSession, startCompanionRun } from '../api/companion'

vi.mock('../api/companion', async (importOriginal) => {
  const original = await importOriginal<typeof import('../api/companion')>()
  return {
    ...original,
    getCompanionSession: vi.fn(),
    getCompanionOverview: vi.fn(),
    startCompanionRun: vi.fn(),
  }
})
vi.mock('./CompanionPushControls', () => ({ default: () => null }))

const run: RunRecord = {
  id: 'run_1', trace_id: 'trace_1', project_id: 'project_1', prompt: 'Review orders',
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
  device, projects: [{ id: 'project_1', name: 'Shop', objective: 'Review orders' } as OpportunityProject],
  pending_permissions: [], notifications: [], runs: [],
}

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

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getCompanionSession).mockResolvedValue(session)
  vi.mocked(getCompanionOverview).mockResolvedValue(overview)
  vi.mocked(startCompanionRun).mockResolvedValue(run)
})

afterEach(() => vi.unstubAllGlobals())

it('requires review and submit before a phone dictation starts a Run', async () => {
  vi.stubGlobal('webkitSpeechRecognition', FakeRecognition)
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
