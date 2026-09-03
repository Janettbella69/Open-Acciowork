import { render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { SkillVersion } from '../api/types'
import PluginsPage from './PluginsPage'

function skill(namespace: string, name: string, enabled = true): SkillVersion {
  return {
    id: `${namespace}:${name}`,
    namespace,
    name,
    description: `${name} capability`,
    version: '1.0.0',
    content_hash: 'a'.repeat(64),
    source: 'application',
    enabled,
  }
}

describe('PluginsPage', () => {
  beforeEach(() => {
    window.history.replaceState({}, '', '/app/plugins')
    const skills = [
      skill('trademind-core', 'market-research'),
      skill('trademind-core', 'supplier-review', false),
      skill('document-tools', 'document-editing'),
    ]
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => skills,
    } as Response)))
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('groups backend Skills into real plugin namespaces and exposes the three page sections', async () => {
    render(<PluginsPage />)

    const sections = screen.getByRole('navigation', { name: /plugin sections/i })
    expect(within(sections).getByRole('link', { name: /^plugins$/i })).toHaveAttribute('aria-current', 'page')
    expect(within(sections).getByRole('link', { name: /^skills$/i })).toHaveAttribute('href', '/app/plugins/skills')
    expect(within(sections).getByRole('link', { name: /^connectors$/i })).toHaveAttribute('href', '/app/plugins/connectors')

    const corePlugin = (await screen.findByRole('heading', { name: 'Trademind Core' })).closest('article')!
    expect(screen.getAllByRole('article')).toHaveLength(2)
    expect(within(corePlugin).getByText(/2 skills · 1 enabled/i)).toBeInTheDocument()
  })
})
