import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import LandingPage from './components/LandingPage'
import Workspace from './components/Workspace'

describe('OPHIRA frontend', () => {
  it('presents the brand proposition and sends a brief to the app', () => {
    render(<LandingPage />)

    expect(screen.getByRole('heading', { level: 1, name: /discover what the world needs/i })).toBeInTheDocument()
    expect(screen.getAllByText(/connect who can supply it/i).length).toBeGreaterThan(0)
    expect(screen.getByRole('textbox', { name: /what opportunity should ophira explore/i })).toHaveAttribute('name', 'brief')
  })

  it('turns an entered question into a visible research brief', () => {
    render(<Workspace />)
    const input = screen.getByRole('textbox', { name: /research brief/i })

    fireEvent.change(input, { target: { value: 'Explore service-ready products for boutique hotels' } })
    fireEvent.click(screen.getByRole('button', { name: /analyze/i }))

    expect(screen.getByText('Explore service-ready products for boutique hotels')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/brief ready/i)
  })

  it('moves from an opportunity into its supply-fit view', () => {
    render(<Workspace />)

    fireEvent.click(screen.getByRole('button', { name: /^supply$/i }))

    expect(screen.getByRole('heading', { name: /can the capability travel/i })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: /supply fit criteria/i })).toBeInTheDocument()
  })
})
