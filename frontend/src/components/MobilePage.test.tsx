import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { localSettingsStorageKey } from '../localSettings'
import MobilePage from './MobilePage'

let localStore: Map<string, string>

beforeEach(() => {
  localStore = new Map<string, string>()
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => localStore.get(key) ?? null,
      setItem: (key: string, value: string) => void localStore.set(key, value),
      removeItem: (key: string) => void localStore.delete(key),
      clear: () => localStore.clear(),
    },
  })
})

describe('MobilePage', () => {
  it('persists companion-device preferences without inventing a pairing code', async () => {
    render(<MobilePage />)

    expect(screen.getByRole('heading', { name: /connect to trademind mobile/i })).toBeInTheDocument()
    expect(screen.getByText(/no qr code generated/i)).toBeInTheDocument()
    const discoverable = screen.getByRole('switch', { name: /allow this computer to be discovered/i })
    expect(discoverable).not.toBeChecked()

    fireEvent.click(discoverable)
    await waitFor(() => expect(JSON.parse(localStore.get(localSettingsStorageKey) ?? '{}')).toMatchObject({
      computerDiscoverable: true,
    }))
  })
})
