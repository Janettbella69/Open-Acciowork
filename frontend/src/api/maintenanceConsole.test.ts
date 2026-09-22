import { afterEach, expect, it, vi } from 'vitest'
import { confirmMaintenance, consumeMaintenanceKey, getMaintenanceStatus, type MaintenanceOperation } from './maintenanceConsole'

afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, '', '/') })

it('consumes a fragment key without persisting it or leaving it in the address', () => {
  const persist = vi.fn()
  vi.stubGlobal('localStorage', { setItem: persist })
  vi.stubGlobal('sessionStorage', { setItem: persist })
  const key = 'a'.repeat(43)
  window.history.replaceState(null, '', `/#key=${key}`)
  expect(consumeMaintenanceKey()).toBe(key)
  expect(window.location.hash).toBe('')
  expect(consumeMaintenanceKey()).toBe('')
  expect(persist).not.toHaveBeenCalled()
})

it('binds confirmations to their review and uses only the local origin without cookies or redirects', async () => {
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
  vi.stubGlobal('fetch', fetch)
  const reviewed = { request_id: 'a'.repeat(32), fingerprint: 'b'.repeat(64) } as MaintenanceOperation
  await confirmMaintenance('private-key', reviewed)
  expect(fetch).toHaveBeenCalledWith(`/api/maintenance/reviews/${reviewed.request_id}/confirm`, expect.objectContaining({
    method: 'POST', credentials: 'omit', redirect: 'error', cache: 'no-store',
    headers: { Authorization: 'Bearer private-key', 'Content-Type': 'application/json' },
    body: JSON.stringify({ fingerprint: reviewed.fingerprint, confirmed: true }),
  }))
  expect(fetch.mock.calls[0][1]).not.toHaveProperty('signal')
})

it('does not expose arbitrary server failure content', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, json: async () => ({ detail: 'credential canary: do not display' }) }))
  await expect(getMaintenanceStatus('private-key')).rejects.toThrow('maintenance_request_failed')
})
