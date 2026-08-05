import { fireEvent, render, screen } from '@testing-library/react'
import SettingsPage from './SettingsPage'

describe('SettingsPage', () => {
  it('keeps high-risk access disabled until the user enables it', () => {
    const stored = new Map<string, string>()
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: (key: string) => stored.get(key) ?? null,
        setItem: (key: string, value: string) => stored.set(key, value),
      },
    })
    render(<SettingsPage />)

    const fullAccess = screen.getByRole('switch', { name: /full access/i })
    expect(fullAccess).not.toBeChecked()

    const memoryLink = screen.getByRole('link', { name: /05 memory/i })
    fireEvent.click(memoryLink)
    expect(memoryLink).toHaveAttribute('aria-current', 'page')

    expect(screen.getByRole('switch', { name: /^workspace memory/i })).toBeChecked()
    const conversationMemory = screen.getByRole('switch', { name: /reference recent conversations/i })
    expect(conversationMemory).not.toBeChecked()
    fireEvent.click(conversationMemory)
    expect(stored.get('ophira-workspace-settings')).toContain('"conversationMemory":true')

    fireEvent.click(fullAccess)
    expect(fullAccess).toBeChecked()
    expect(stored.get('ophira-workspace-settings')).toContain('"fullAccess":true')

    expect(screen.getByRole('combobox', { name: /plugin access/i })).toHaveValue('ask')
    expect(screen.getByRole('combobox', { name: /browser access/i })).toHaveValue('ask')
    const computerControl = screen.getByRole('combobox', { name: /computer control access/i })
    expect(computerControl).toHaveValue('disabled')

    fireEvent.change(computerControl, { target: { value: 'ask' } })
    expect(stored.get('ophira-workspace-settings')).toContain('"computerControl":"ask"')

    const discoverable = screen.getByRole('switch', { name: /allow this computer to be discovered and controlled/i })
    expect(discoverable).not.toBeChecked()
    fireEvent.click(discoverable)
    expect(stored.get('ophira-workspace-settings')).toContain('"computerDiscoverable":true')
    expect(screen.getByText(/pairing service required/i)).toBeInTheDocument()
  })
})
