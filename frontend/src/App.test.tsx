import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { OpportunityProject, OphiraEvent, RunRecord } from './api/types'
import LandingPage from './components/LandingPage'
import Workspace from './components/Workspace'

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
  revision: 1,
  created_at: timestamp,
  updated_at: timestamp,
}

const run: RunRecord = {
  id: 'run_test',
  trace_id: 'trace_test',
  project_id: project.id,
  prompt: project.objective,
  status: 'queued',
  sdk_session_id: null,
  error: null,
  created_at: timestamp,
  started_at: null,
  finished_at: null,
}

let eventSequence = 0

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
    timestamp,
    actor: { type: 'agent', name: 'opportunity-lead', agent_id: null },
    parent_id: null,
    tool_use_id: type.startsWith('tool.') ? 'tool_test' : null,
    data,
  }
}

describe('OPHIRA frontend', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/app')
    MockEventSource.instances = []
    eventSequence = 0
    vi.stubGlobal('EventSource', MockEventSource)
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/meta/protocol')) {
        return jsonResponse({
          version: '0.1.0',
          event_types: ['run.started', 'assistant.text.delta', 'tool.started', 'run.completed'],
          run_statuses: [],
          dimensions: [],
          project_stages: [],
          permission_decisions: [],
        })
      }
      if (url.endsWith('/api/projects') && !init?.method) return jsonResponse([])
      if (url.endsWith('/api/projects') && init?.method === 'POST') return jsonResponse(project, 201)
      if (url.endsWith(`/api/projects/${project.id}/runs`)) return jsonResponse(run, 202)
      if (url.endsWith(`/api/projects/${project.id}`)) return jsonResponse(project)
      if (url.endsWith(`/api/runs/${run.id}`)) return jsonResponse({ ...run, status: 'completed' })
      throw new Error(`Unexpected request: ${init?.method ?? 'GET'} ${url}`)
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('presents the brand proposition and sends a brief to the app', () => {
    render(<LandingPage />)

    expect(screen.getByRole('heading', { level: 1, name: /discover what the world needs/i })).toBeInTheDocument()
    expect(screen.getAllByText(/connect who can supply it/i).length).toBeGreaterThan(0)
    expect(screen.getByRole('textbox', { name: /what opportunity should ophira explore/i })).toHaveAttribute('name', 'brief')
  })

  it('creates a live backend run and renders observable SDK events', async () => {
    render(<Workspace />)
    await screen.findByRole('heading', { name: /what opportunity should we explore/i })
    const input = screen.getByRole('textbox', { name: /ask ophira/i })

    fireEvent.change(input, { target: { value: project.objective } })
    fireEvent.click(screen.getByRole('button', { name: /start research/i }))

    expect(screen.getAllByText(project.objective).length).toBeGreaterThan(0)
    await screen.findByText(/backend connected/i)
    expect(screen.queryByRole('heading', { name: /turn a market question into a commercial path/i })).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: /ask ophira/i })).toBeInTheDocument()
    expect(MockEventSource.instances).toHaveLength(1)
    const source = MockEventSource.instances[0]
    expect(source.url).toContain(`/api/runs/${run.id}/events`)

    act(() => {
      source.onopen?.()
      source.emit('run.started', runtimeEvent('run.started', { status: 'running' }))
      source.emit('assistant.text.delta', runtimeEvent('assistant.text.delta', { text: 'Validating demand evidence.' }))
      source.emit('tool.started', runtimeEvent('tool.started', { tool_name: 'WebSearch', tool_input: { query: 'boutique hotel amenities' } }))
    })

    expect(screen.getByText('Validating demand evidence.')).toBeInTheDocument()
    expect(screen.getByText(/tool started · websearch/i)).toBeInTheDocument()
    expect(screen.getByText(/trace trace_test/i)).toBeInTheDocument()
  })

  it('keeps an imported website brief inside the same conversation workspace', async () => {
    window.history.replaceState({}, '', '/app?brief=%E4%BD%A0%E5%A5%BD')
    render(<Workspace />)

    const input = await screen.findByRole('textbox', { name: /ask ophira/i })
    expect(input).toHaveValue('你好')

    fireEvent.click(screen.getByRole('button', { name: /start research/i }))

    await screen.findByText(/backend connected/i)
    expect(screen.getByRole('heading', { level: 1, name: project.name })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /turn a market question into a commercial path/i })).not.toBeInTheDocument()
    expect(window.location.pathname).toBe('/app')
    expect(window.location.search).toBe('')
  })

  it('uses a validation shortcut to prepare the primary conversation box', async () => {
    render(<Workspace />)
    const input = await screen.findByRole('textbox', { name: /ask ophira/i })

    fireEvent.click(screen.getByRole('button', { name: /test whether our capability can travel/i }))

    expect((input as HTMLTextAreaElement).value).toMatch(/best fits our current product and supply capability/i)
  })
})
