import { fireEvent, render, screen } from '@testing-library/react'
import App from '../App'

describe('ScheduledTasksPage', () => {
  it('opens from the app route and saves a template as a local task draft', () => {
    const stored = new Map<string, string>()
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: (key: string) => stored.get(key) ?? null,
        setItem: (key: string, value: string) => stored.set(key, value),
      },
    })
    window.history.replaceState({}, '', '/app/schedules')
    render(<App />)

    expect(screen.getByRole('heading', { level: 1, name: /scheduled tasks/i })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /morning opportunity brief/i }))

    expect(screen.getByRole('dialog', { name: /create scheduled task/i })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /save draft/i }))

    expect(screen.getByRole('heading', { name: /my task drafts/i })).toBeInTheDocument()
    expect(screen.getByText('Morning opportunity brief')).toBeInTheDocument()
    expect(stored.get('ophira-scheduled-task-drafts')).toContain('Morning opportunity brief')
  })
})
