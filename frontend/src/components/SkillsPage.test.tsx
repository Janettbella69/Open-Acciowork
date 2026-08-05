import { fireEvent, render, screen } from '@testing-library/react'
import SkillsPage from './SkillsPage'

describe('SkillsPage', () => {
  it('searches built-in skills and saves a custom skill draft', () => {
    const stored = new Map<string, string>()
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: (key: string) => stored.get(key) ?? null,
        setItem: (key: string, value: string) => stored.set(key, value),
      },
    })

    render(<SkillsPage />)

    const search = screen.getByRole('searchbox', { name: /search skills/i })
    fireEvent.change(search, { target: { value: 'supplier' } })
    expect(screen.getByRole('heading', { name: /supplier capability map/i })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /market signal radar/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /new skill/i }))
    expect(screen.getByRole('dialog', { name: /create a skill/i })).toBeInTheDocument()

    fireEvent.change(screen.getByRole('textbox', { name: /skill name/i }), { target: { value: 'Channel Review' } })
    fireEvent.change(screen.getByRole('textbox', { name: /instructions/i }), { target: { value: 'Review channel readiness, evidence gaps, and the next approved test.' } })
    fireEvent.click(screen.getByRole('button', { name: /save skill/i }))

    expect(screen.getByRole('heading', { name: /channel review/i })).toBeInTheDocument()
    expect(stored.get('ophira-custom-skill-drafts')).toContain('Channel Review')
    expect(screen.getByRole('button', { name: /yours/i })).toHaveAttribute('data-active', 'true')
  })
})
