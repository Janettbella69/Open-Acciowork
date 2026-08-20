import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SkillVersion } from '../api/types'
import SkillsPage from './SkillsPage'

function skill(name: string, description: string, enabled = true): SkillVersion {
  return {
    id: `ophira-core:${name}`,
    namespace: 'ophira-core',
    name,
    description,
    version: '1.0.0',
    content_hash: 'a'.repeat(64),
    source: 'application',
    enabled,
    path: `/plugin/skills/${name}/SKILL.md`,
  }
}

const skills = [
  skill('global-opportunity-execution', 'Run a sourced global opportunity study.'),
  skill('supplier-due-diligence', 'Assess supplier capability and risk.'),
  skill('landed-cost-analysis', 'Deterministic landed cost method.'),
  skill('customer-path', 'Find customer segments and contact paths.'),
]

function jsonResponse(body: unknown, status = 200): Response {
  return { ok: status < 300, status, json: async () => body } as Response
}

describe('SkillsPage', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/skills') && !init?.method) return jsonResponse(skills)
      if (url.includes('/enable?enabled=false') && init?.method === 'POST') {
        return jsonResponse({ ...skills[1], enabled: false })
      }
      if (url.endsWith('/test') && init?.method === 'POST') {
        return jsonResponse({
          skill_id: 'ophira-core:supplier-due-diligence',
          ok: true,
          checked_at: '2026-08-20T12:00:00Z',
          checks: [
            { name: 'frontmatter', status: 'passed', detail: 'name/description present' },
            { name: 'secret_scan', status: 'passed', detail: 'clean' },
            {
              name: 'permission_frontmatter',
              status: 'warning',
              detail: 'allowed-tools 不强制执行',
            },
          ],
        })
      }
      throw new Error(`Unexpected request: ${init?.method ?? 'GET'} ${url}`)
    }))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the versioned plugin skills discovered by the backend', async () => {
    render(<SkillsPage />)

    expect(
      await screen.findByRole('heading', { level: 2, name: /global opportunity execution/i }),
    ).toBeInTheDocument()
    expect(screen.getByText(/ophira-core:global-opportunity-execution/i)).toBeInTheDocument()
    expect(screen.getAllByRole('article')).toHaveLength(4)

    const search = screen.getByRole('searchbox', { name: /search skills/i })
    fireEvent.change(search, { target: { value: 'supplier' } })
    expect(screen.getAllByRole('article')).toHaveLength(1)
    expect(screen.getByRole('heading', { name: /supplier due diligence/i })).toBeInTheDocument()

    fireEvent.change(search, { target: { value: 'nothing-matches-this' } })
    expect(screen.getByText(/no skills match this search/i)).toBeInTheDocument()
  })

  it('runs the deterministic package test and renders the checks', async () => {
    render(<SkillsPage />)
    await screen.findByRole('heading', { name: /supplier due diligence/i })

    const card = screen.getByRole('heading', { name: /supplier due diligence/i }).closest('article')!
    fireEvent.click(within(card).getByRole('button', { name: /^test$/i }))

    expect(await within(card).findByText(/package test passed/i)).toBeInTheDocument()
    expect(within(card).getByText(/secret_scan/)).toBeInTheDocument()
    expect(within(card).getByText(/不强制执行/)).toBeInTheDocument()
    const calls = vi.mocked(fetch).mock.calls.map(([request]) => String(request))
    expect(calls.some((url) => url.endsWith(
      `/api/skills/${encodeURIComponent('ophira-core:supplier-due-diligence')}/test`,
    ))).toBe(true)
  })

  it('toggles a skill through the backend enable endpoint', async () => {
    render(<SkillsPage />)
    await screen.findByRole('heading', { name: /supplier due diligence/i })

    const card = screen.getByRole('heading', { name: /supplier due diligence/i }).closest('article')!
    fireEvent.click(within(card).getByRole('button', { name: /disable/i }))

    await waitFor(() => {
      expect(within(card).getByRole('button', { name: /enable/i })).toBeInTheDocument()
    })
    const calls = vi.mocked(fetch).mock.calls.map(([request]) => String(request))
    expect(calls.some((url) => url.includes(
      `/api/skills/${encodeURIComponent('ophira-core:supplier-due-diligence')}/enable?enabled=false`,
    ))).toBe(true)
  })
})
