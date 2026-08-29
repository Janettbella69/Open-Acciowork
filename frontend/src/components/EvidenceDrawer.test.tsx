import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Evidence, OpportunityProject } from '../api/types'
import { I18nContext } from '../i18n'
import EvidenceDrawer from './EvidenceDrawer'

vi.mock('../api/client', () => ({
  setEvidenceStatus: vi.fn(),
  errorMessage: (error: unknown) => (error instanceof Error ? error.message : 'unknown error'),
}))

import { setEvidenceStatus } from '../api/client'

const fact: Evidence = {
  id: 'ev_fact',
  dimension: null,
  tags: ['pricing'],
  kind: 'observed_fact',
  claim: '报价单显示单价 6.8 USD',
  source_title: 'quotes.csv',
  source_url: null,
  source_type: 'file',
  source_path: 'uploads/quotes.csv',
  retrieved_at: '2026-08-20T10:00:00Z',
  observed_at: null,
  excerpt: null,
  location: null,
  freshness_note: null,
  status: 'active',
  confidence: 0.9,
  supports_hypothesis_ids: [],
  recorded_by: 'agent',
  recorded_at: '2026-08-20T10:00:00Z',
}

const signal: Evidence = {
  ...fact,
  id: 'ev_signal',
  kind: 'community_signal',
  claim: '社媒热度上升',
  source_title: 'Eurostat',
  source_url: 'https://example.org/stats',
  source_path: null,
  source_type: 'web',
  confidence: 0.4,
  freshness_note: 'URL 来自用户材料，未实时抓取',
}

const project = {
  id: 'project_1',
  name: 'Kettle',
  objective: 'Validate',
  product_brief: '',
  target_markets: [],
  constraints: [],
  stage: 'discovery',
  hypotheses: [],
  evidence: [fact, signal],
  assessments: {},
  stage_decisions: [],
  decisions: [],
  artifacts: [],
  revision: 1,
  created_at: '2026-08-20T09:00:00Z',
  updated_at: '2026-08-20T10:00:00Z',
} as unknown as OpportunityProject

describe('EvidenceDrawer', () => {
  beforeEach(() => {
    vi.mocked(setEvidenceStatus).mockResolvedValue({
      evidence: { ...fact, status: 'stale' },
      affected_decision_ids: ['decision_1'],
    })
  })

  it('renders sourced evidence with kind, confidence, and provenance', () => {
    render(<EvidenceDrawer project={project} onChanged={vi.fn()} />)

    const factCard = screen.getByText(/报价单显示单价/).closest('article')!
    expect(within(factCard).getByText('Observed fact')).toBeInTheDocument()
    expect(within(factCard).getByText(/confidence 90%/i)).toBeInTheDocument()
    expect(within(factCard).getByText('uploads/quotes.csv')).toBeInTheDocument()

    const signalCard = screen.getByText(/社媒热度上升/).closest('article')!
    expect(within(signalCard).getByRole('link', { name: 'Eurostat' })).toHaveAttribute(
      'href', 'https://example.org/stats',
    )
    expect(within(signalCard).getByText(/未实时抓取/)).toBeInTheDocument()
  })

  it('filters by evidence kind', () => {
    render(<EvidenceDrawer project={project} onChanged={vi.fn()} />)

    fireEvent.change(screen.getByRole('combobox', { name: /filter by kind/i }), {
      target: { value: 'community_signal' },
    })

    expect(screen.queryByText(/报价单显示单价/)).not.toBeInTheDocument()
    expect(screen.getByText(/社媒热度上升/)).toBeInTheDocument()
  })

  it('marks evidence stale through the backend and reports affected decisions', async () => {
    const onChanged = vi.fn()
    render(<EvidenceDrawer project={project} onChanged={onChanged} />)

    const factCard = screen.getByText(/报价单显示单价/).closest('article')!
    fireEvent.click(within(factCard).getByRole('button', { name: /mark stale/i }))

    await waitFor(() =>
      expect(setEvidenceStatus).toHaveBeenCalledWith('project_1', 'ev_fact', 'stale'),
    )
    expect(await screen.findByRole('status')).toHaveTextContent(/1 linked decisions need review/i)
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it('offers restore for non-active evidence', () => {
    const staleProject = {
      ...project,
      evidence: [{ ...fact, status: 'stale' }],
    } as unknown as OpportunityProject
    render(<EvidenceDrawer project={staleProject} onChanged={vi.fn()} />)

    expect(screen.getByRole('button', { name: /restore/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /mark stale/i })).not.toBeInTheDocument()
  })

  it('translates evidence controls while preserving source content', () => {
    render(
      <I18nContext.Provider value="zh-CN">
        <EvidenceDrawer project={project} onChanged={vi.fn()} />
      </I18nContext.Provider>,
    )

    const factCard = screen.getByText(/报价单显示单价/).closest('article')!
    expect(within(factCard).getByText('观测事实')).toBeInTheDocument()
    expect(within(factCard).getByText(/置信度 90%/)).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: '按类型筛选' })).toBeInTheDocument()
    expect(within(factCard).getByRole('button', { name: '标记过期' })).toBeInTheDocument()
  })
})
