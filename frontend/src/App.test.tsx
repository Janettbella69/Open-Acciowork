import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { OpportunityProject, OphiraEvent, RunRecord } from './api/types'
import App from './App'
import LandingPage from './components/LandingPage'
import Workspace from './components/Workspace'
import { pageTitle } from './pageMetadata'

const timestamp = '2026-08-05T10:00:00Z'
const emptyAssessment = (dimension: 'demand' | 'supply' | 'capital' | 'sales') => ({
  dimension,
  status: 'unverified' as const,
  score: 0,
  summary: '',
  gaps: [],
  evidence_ids: [],
  updated_by: 'system',
  updated_at: timestamp,
})

const project: OpportunityProject = {
  id: 'project_test',
  name: 'Explore service-ready products for boutique hotels',
  objective: 'Explore service-ready products for boutique hotels',
  product_brief: 'Explore service-ready products for boutique hotels',
  target_markets: [],
  constraints: [],
  stage: 'discovery',
  hypotheses: [],
  evidence: [],
  assessments: {
    demand: emptyAssessment('demand'),
    supply: emptyAssessment('supply'),
    capital: emptyAssessment('capital'),
    sales: emptyAssessment('sales'),
  },
  stage_decisions: [],
  decisions: [],
  artifacts: [],
  revision: 1,
  created_at: timestamp,
  updated_at: timestamp,
}

const run: RunRecord = {
  id: 'run_test',
  trace_id: 'trace_test',
  project_id: project.id,
  prompt: project.objective,
  agent_id: null,
  mode: 'standard',
  provider_profile_id: null,
  provider_snapshot: null,
  skill_snapshot: [],
  status: 'queued',
  sdk_session_id: null,
  error: null,
  error_code: null,
  created_at: timestamp,
  started_at: null,
  finished_at: null,
}

let eventSequence = 0
let storedProjects: OpportunityProject[] = []

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response
}

class MockEventSource {
  static instances: MockEventSource[] = []
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  listeners = new Map<string, Set<EventListener>>()
  closed = false

  constructor(public url: string) {
    MockEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: EventListener) {
    const listeners = this.listeners.get(type) ?? new Set<EventListener>()
    listeners.add(listener)
    this.listeners.set(type, listeners)
  }

  close() {
    this.closed = true
  }

  emit(type: string, event: OphiraEvent) {
    const message = { data: JSON.stringify(event) } as MessageEvent<string>
    this.listeners.get(type)?.forEach((listener) => listener(message))
  }

  end() {
    const message = { data: JSON.stringify({
      run_id: run.id, project_id: project.id, last_sequence: eventSequence,
    }) } as MessageEvent<string>
    this.listeners.get('stream.completed')?.forEach((listener) => listener(message))
  }
}

function runtimeEvent(type: string, data: Record<string, unknown>): OphiraEvent {
  eventSequence += 1
  return {
    id: `${run.id}:${eventSequence}`,
    sequence: eventSequence,
    type,
    run_id: run.id,
    project_id: project.id,
    trace_id: run.trace_id,
    sdk_session_id: null,
    provider_profile_id: null,
    timestamp,
    actor: { type: 'agent', name: 'opportunity-lead', agent_id: null },
    parent_id: null,
    tool_use_id: type.startsWith('tool.') ? 'tool_test' : null,
    data,
  }
}

describe('TradeMind frontend', () => {
  beforeEach(() => {
    const localStore = new Map<string, string>()
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: (key: string) => localStore.get(key) ?? null,
        setItem: (key: string, value: string) => void localStore.set(key, value),
        removeItem: (key: string) => void localStore.delete(key),
        clear: () => localStore.clear(),
      },
    })
    window.history.replaceState({}, '', '/app')
    MockEventSource.instances = []
    eventSequence = 0
    storedProjects = []
    vi.stubGlobal('EventSource', MockEventSource)
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/meta/protocol')) {
        return jsonResponse({
          version: '0.1.0',
          event_types: ['run.started', 'assistant.text.delta', 'tool.started', 'run.completed', 'evidence.added', 'decision.recorded'],
          stream_end_event: 'stream.completed',
          run_statuses: ['queued', 'running', 'waiting_permission', 'paused', 'cancelling', 'completed', 'failed', 'cancelled'],
          dimensions: [],
          project_stages: [],
          permission_decisions: [],
          agent_templates: ['general', 'research', 'sourcing', 'marketing', 'docs', 'opportunity'],
        })
      }
      if (url.endsWith('/api/agents') && !init?.method) return jsonResponse([])
      if (url.endsWith('/api/providers') && !init?.method) return jsonResponse([])
      if (url.endsWith('/api/permissions/pending') && !init?.method) return jsonResponse([])
      if (url.endsWith('/api/connectors') && !init?.method) return jsonResponse([])
      if (url.endsWith('/api/skills') && !init?.method) return jsonResponse([])
      if (url.endsWith('/api/skills/repository')) return jsonResponse({ identity_mode: 'local_only', tenant_id: null, actor_id: 'local-operator', can_manage: true })
      if (url.endsWith('/api/projects') && !init?.method) return jsonResponse(storedProjects)
      if (url.endsWith('/api/projects') && init?.method === 'POST') return jsonResponse(project, 201)
      if (url.endsWith(`/api/projects/${project.id}/runs`) && init?.method === 'POST') return jsonResponse(run, 202)
      if (url.endsWith(`/api/projects/${project.id}/runs`)) return jsonResponse([{ ...run, status: 'completed', finished_at: timestamp }])
      if (url.endsWith(`/api/projects/${project.id}`)) return jsonResponse(project)
      if (url.endsWith(`/api/runs/${run.id}`)) return jsonResponse({ ...run, status: 'completed', sdk_session_id: 'session_test', finished_at: timestamp })
      throw new Error(`Unexpected request: ${init?.method ?? 'GET'} ${url}`)
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    Reflect.deleteProperty(window, 'localStorage')
  })

  it('uses distinct browser titles for product routes', () => {
    expect(pageTitle('/')).toBe('TradeMind - AI Global Commerce Workspace')
    expect(pageTitle('/app')).toBe('TradeMind - AI Global Commerce Workspace')
    expect(pageTitle('/app/schedules')).toBe('TradeMind Scheduled - AI Global Commerce Workspace')
    expect(pageTitle('/app/plugins')).toBe('TradeMind Plugins - AI Global Commerce Workspace')
    expect(pageTitle('/app/plugins/skills')).toBe('TradeMind Skills - AI Global Commerce Workspace')
    expect(pageTitle('/app/skills')).toBe('TradeMind Skills - AI Global Commerce Workspace')
    expect(pageTitle('/app/agents')).toBe('TradeMind Agents - AI Global Commerce Workspace')
    expect(pageTitle('/app/plugins/connectors')).toBe('TradeMind Connectors - AI Global Commerce Workspace')
    expect(pageTitle('/app/connectors')).toBe('TradeMind Connectors - AI Global Commerce Workspace')
    expect(pageTitle('/app/inbox')).toBe('TradeMind Inbox - AI Global Commerce Workspace')
    expect(pageTitle('/app/mobile')).toBe('TradeMind Mobile - AI Global Commerce Workspace')
    expect(pageTitle('/app/settings')).toBe('TradeMind Settings - AI Global Commerce Workspace')
  })

  it('presents the brand proposition and sends a brief to the app', () => {
    render(<LandingPage />)

    expect(screen.getByRole('heading', { level: 1, name: /go beyond borders/i })).toBeInTheDocument()
    expect(screen.getByText(/from product idea to global sales/i)).toBeInTheDocument()
    const brief = screen.getByRole('textbox', { name: /what opportunity should trademind explore/i })
    expect(brief).toHaveAttribute('name', 'brief')

    fireEvent.click(screen.getByRole('button', { name: 'Sourcing' }))
    expect(screen.getByRole('button', { name: 'Sourcing' })).toHaveAttribute('aria-pressed', 'true')
    expect(brief).toHaveValue('Compare suppliers, quotes, and landed margin for this product')

    expect(screen.getByRole('button', { name: /spot products before they peak/i })).toHaveAttribute('data-panel', 'demand')
    expect(screen.getByRole('button', { name: /compare factories and true margin/i })).toHaveAttribute('data-panel', 'route')
    expect(screen.getByRole('button', { name: /localize listings that convert/i })).toHaveAttribute('data-panel', 'sale')
    expect(screen.getByRole('button', { name: /launch across every channel/i })).toHaveAttribute('data-panel', 'europe')
  })

  it('keeps Plugins, Skills, and Connectors inside one Plugins navigation page', async () => {
    window.history.replaceState({}, '', '/app/plugins/skills')
    render(<App />)

    expect(screen.getByRole('heading', { level: 1, name: 'Skills' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /^agents$/i })).toHaveAttribute('href', '/app/agents')
    const pluginLinks = screen.getAllByRole('link', { name: /^plugins$/i })
    expect(pluginLinks.some((link) => link.getAttribute('aria-current') === 'page' && link.getAttribute('href') === '/app/plugins')).toBe(true)
    const sections = screen.getByRole('navigation', { name: /plugin sections/i })
    expect(within(sections).getByRole('link', { name: /^plugins$/i })).toHaveAttribute('href', '/app/plugins')
    expect(within(sections).getByRole('link', { name: /^skills$/i })).toHaveAttribute('aria-current', 'page')
    expect(within(sections).getByRole('link', { name: /^connectors$/i })).toHaveAttribute('href', '/app/plugins/connectors')
    expect(screen.getByRole('link', { name: /new task/i })).toHaveAttribute('href', '/app')
    expect(await screen.findByText(/no plugin skills discovered/i)).toBeInTheDocument()
  })

  it('renders Connectors as a Plugins page section rather than a sidebar branch', async () => {
    window.history.replaceState({}, '', '/app/plugins/connectors')
    render(<App />)

    expect(screen.getByRole('heading', { level: 1, name: 'Connectors' })).toBeInTheDocument()
    const sections = screen.getByRole('navigation', { name: /plugin sections/i })
    expect(within(sections).getByRole('link', { name: /^connectors$/i })).toHaveAttribute('aria-current', 'page')
    expect(screen.queryByRole('link', { name: /^connectors$/i, current: 'page' })).toBeInTheDocument()
    expect(await screen.findByText(/no connectors configured/i)).toBeInTheDocument()
  })

  it('routes Agents to its own directory instead of reloading the task composer', async () => {
    window.history.replaceState({}, '', '/app/agents')
    render(<App />)

    expect(screen.getByRole('heading', { level: 1, name: 'Agents' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /^agents$/i })).toHaveAttribute('aria-current', 'page')
    expect(await screen.findByText('Market Research Expert')).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /what can i do for you/i })).not.toBeInTheDocument()
  })

  it('creates a live backend run and renders observable SDK events', async () => {
    render(<Workspace />)
    await screen.findByRole('heading', { name: /what can i do for you/i })
    const input = screen.getByRole('textbox', { name: /ask trademind/i })

    fireEvent.change(input, { target: { value: project.objective } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    expect(screen.getAllByText(project.objective).length).toBeGreaterThan(0)
    await screen.findByText(/backend connected/i)
    expect(screen.queryByRole('heading', { name: /turn a market question into a commercial path/i })).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: /ask trademind/i })).toBeInTheDocument()
    expect(MockEventSource.instances).toHaveLength(1)
    const source = MockEventSource.instances[0]
    expect(source.url).toContain(`/api/runs/${run.id}/events`)

    await act(async () => {
      source.onopen?.()
      source.emit('run.started', runtimeEvent('run.started', { status: 'running' }))
      source.emit('assistant.text.delta', runtimeEvent('assistant.text.delta', { text: 'Validating demand evidence.' }))
      source.emit('tool.started', runtimeEvent('tool.started', { tool_name: 'WebSearch', tool_input: { query: 'boutique hotel amenities' } }))
    })

    expect(screen.getByText('Validating demand evidence.')).toBeInTheDocument()
    expect(screen.getByText(/using websearch/i)).toBeInTheDocument()
    expect(screen.getByText(/trace trace_test/i)).toBeInTheDocument()
  })

  it('passes a personal Agent selection into the backend run', async () => {
    window.history.replaceState({}, '', '/app?agent=agent_research')
    render(<Workspace />)

    expect(await screen.findByText(/agent selected/i)).toBeInTheDocument()
    fireEvent.change(screen.getByRole('textbox', { name: /ask trademind/i }), {
      target: { value: project.objective },
    })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => {
      const runRequest = vi.mocked(fetch).mock.calls.find(([input, init]) => (
        String(input).endsWith(`/api/projects/${project.id}/runs`) && init?.method === 'POST'
      ))
      expect(runRequest).toBeDefined()
      expect(JSON.parse(String(runRequest?.[1]?.body))).toMatchObject({ agent_id: 'agent_research' })
    })
  })

  it('sends the mode selected from the composer tools menu to the backend run', async () => {
    render(<Workspace />)
    await screen.findByRole('heading', { name: /what can i do for you/i })

    fireEvent.click(screen.getByRole('button', { name: /open tools menu/i }))
    fireEvent.click(screen.getByRole('button', { name: /^mode$/i }))
    fireEvent.click(screen.getByRole('button', { name: /^deep research/i }))
    fireEvent.change(screen.getByRole('textbox', { name: /ask trademind/i }), {
      target: { value: project.objective },
    })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => {
      const runRequest = vi.mocked(fetch).mock.calls.find(([input, init]) => (
        String(input).endsWith(`/api/projects/${project.id}/runs`) && init?.method === 'POST'
      ))
      expect(runRequest).toBeDefined()
      expect(JSON.parse(String(runRequest?.[1]?.body))).toMatchObject({ mode: 'deep_research' })
    })
  })

  it('keeps an imported website brief inside the same conversation workspace', async () => {
    window.history.replaceState({}, '', '/app?brief=%E4%BD%A0%E5%A5%BD')
    render(<Workspace />)

    const input = await screen.findByRole('textbox', { name: /ask trademind/i })
    expect(input).toHaveValue('你好')

    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    await screen.findByText(/backend connected/i)
    expect(await screen.findByRole('region', { name: /conversation with trademind/i })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { level: 1, name: project.name })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /turn a market question into a commercial path/i })).not.toBeInTheDocument()
    expect(window.location.pathname).toBe('/app')
    expect(window.location.search).toBe('')
  })

  it('keeps the primary input open-ended instead of injecting a preset workflow', async () => {
    render(<Workspace />)
    const input = await screen.findByRole('textbox', { name: /ask trademind/i })

    fireEvent.change(input, { target: { value: 'Help me reason through an unfamiliar problem.' } })

    expect(input).toHaveValue('Help me reason through an unfamiliar problem.')
    expect(screen.getByRole('region', { name: /example opportunities/i })).toBeInTheDocument()
  })

  it('continues inside the same project and resumes the model session', async () => {
    render(<Workspace />)
    const input = await screen.findByRole('textbox', { name: /ask trademind/i })

    fireEvent.change(input, { target: { value: project.objective } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))
    await screen.findByText(/backend connected/i)

    const source = MockEventSource.instances[0]
    act(() => {
      source.emit('run.completed', runtimeEvent('run.completed', { status: 'completed' }))
      source.end()
    })
    await waitFor(() => expect(screen.getByText(/ran for/i)).toBeInTheDocument())

    const followUp = screen.getByRole('textbox', { name: /ask trademind/i })
    fireEvent.change(followUp, { target: { value: 'Now challenge the strongest assumption.' } })
    fireEvent.click(screen.getByRole('button', { name: /send/i }))

    await waitFor(() => {
      const runPosts = vi.mocked(fetch).mock.calls.filter(([request, init]) => (
        String(request).endsWith(`/api/projects/${project.id}/runs`) && init?.method === 'POST'
      ))
      expect(runPosts).toHaveLength(2)
      expect(JSON.parse(String(runPosts[1][1]?.body))).toEqual({
        prompt: 'Now challenge the strongest assumption.',
        mode: 'standard',
        resume_session_id: 'session_test',
      })
    })
  })

  it('restores the latest recorded response when a saved project is selected', async () => {
    storedProjects = [project]
    render(<Workspace />)

    const savedProject = await screen.findByRole('button', { name: new RegExp(project.name, 'i') })
    fireEvent.click(savedProject)

    expect(await screen.findByText(/restoring the recorded response/i)).toBeInTheDocument()
    const source = MockEventSource.instances[0]
    act(() => {
      source.emit('assistant.text.delta', runtimeEvent('assistant.text.delta', { text: 'Restored from the durable event log.' }))
      source.emit('run.completed', runtimeEvent('run.completed', { status: 'completed' }))
      source.end()
    })

    expect(await screen.findByText('Restored from the durable event log.')).toBeInTheDocument()
    expect(screen.queryByText(/four dimensions, one decision/i)).not.toBeInTheDocument()
  })

  it('refreshes the actual project ledger from record events after historical run completion', async () => {
    storedProjects = [project]
    const originalFetch = vi.mocked(fetch).getMockImplementation()!
    let ledgerProject = project
    vi.mocked(fetch).mockImplementation((input, init) => {
      if (String(input).endsWith(`/api/projects/${project.id}`)) return Promise.resolve(jsonResponse(ledgerProject))
      return originalFetch(input, init)
    })
    render(<Workspace />)
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(project.name, 'i') }))
    await screen.findByText(/restoring the recorded response/i)
    const source = MockEventSource.instances[0]
    act(() => {
      source.emit('assistant.text.delta', runtimeEvent('assistant.text.delta', { text: 'Recorded analysis.' }))
      source.emit('run.completed', runtimeEvent('run.completed', { status: 'completed' }))
    })
    expect(source.closed).toBe(false)
    expect(screen.getByText(/restoring the recorded response/i)).toBeInTheDocument()

    ledgerProject = {
      ...project,
      revision: 3,
      evidence: [{
        id: 'evidence_tail', dimension: null, tags: [], kind: 'user_statement',
        claim: 'The buyer requested a sample.', source_title: 'User confirmation',
        source_url: null, source_type: 'user', source_path: null, retrieved_at: timestamp,
        observed_at: null, excerpt: null, location: null, freshness_note: null,
        status: 'active', confidence: 1, supports_hypothesis_ids: [], recorded_by: 'main',
        source_run_id: run.id, recorded_at: timestamp,
      }],
      decisions: [{
        id: 'decision_tail', question: 'Next step?', outcome: 'Prepare a sample for human review.',
        decision_type: 'test', rationale: 'Buyer interest requires validation.',
        evidence_ids: ['evidence_tail'], hypothesis_ids: [], assumptions: [], conditions: [],
        next_actions: [], status: 'active', made_by: 'main', review_at: null,
        invalidated_by_evidence_ids: [], created_at: timestamp,
      }],
    }
    await act(async () => {
      source.emit('evidence.added', runtimeEvent('evidence.added', {
        schema_version: 1, resource_id: 'evidence_tail', evidence_id: 'evidence_tail', recorded_at: timestamp,
      }))
      source.emit('decision.recorded', runtimeEvent('decision.recorded', {
        schema_version: 1, resource_id: 'decision_tail', decision_id: 'decision_tail', recorded_at: timestamp,
      }))
      source.end()
    })
    await waitFor(() => expect(screen.queryByText(/restoring the recorded response/i)).not.toBeInTheDocument())
    expect(source.closed).toBe(true)
    fireEvent.click(screen.getByText('Project record'))
    expect(screen.getByText('The buyer requested a sample.')).toBeVisible()
    expect(screen.getByText('Prepare a sample for human review.')).toBeVisible()
    expect(screen.getByText('Recorded analysis.')).toBeInTheDocument()
    expect(screen.queryByText('Stream Completed')).not.toBeInTheDocument()
  })
})
