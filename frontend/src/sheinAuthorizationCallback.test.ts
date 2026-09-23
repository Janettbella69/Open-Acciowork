import { describe, expect, it, vi } from 'vitest'
import { captureSheinCallback, SHEIN_CALLBACK_PREFIX } from './sheinAuthorizationCallback'

const state = `${'a'.repeat(32)}.${'b'.repeat(64)}`
const location = { pathname: `${SHEIN_CALLBACK_PREFIX}shein`, search: `?appid=synthetic-app&state=${state}&tempToken=synthetic-token`, hash: '' }

describe('SHEIN authorization callback capture', () => {
  it('captures only bounded callback fields and immediately removes the query from history', () => {
    const history = { replaceState: vi.fn() }
    expect(captureSheinCallback(location, history)).toEqual({ connectorId: 'shein', appId: 'synthetic-app', state, tempToken: 'synthetic-token' })
    expect(history.replaceState).toHaveBeenCalledWith(null, '', location.pathname)
  })
  it.each([
    { search: `${location.search}&state=duplicate` },
    { search: '?appid=synthetic-app&state=bad&tempToken=synthetic-token' },
    { search: '?state=missing-app' },
    { search: `${location.search}${'x'.repeat(5000)}` },
    { pathname: `${SHEIN_CALLBACK_PREFIX}../other` },
    { hash: '#untrusted' },
  ])('clears malformed callback material without accepting it (%j)', (change) => {
    const history = { replaceState: vi.fn() }
    expect(captureSheinCallback({ ...location, ...change }, history)).toBeNull()
    expect(history.replaceState).toHaveBeenCalledOnce()
  })
  it('does not change unrelated page query parameters', () => {
    const history = { replaceState: vi.fn() }
    expect(captureSheinCallback({ ...location, pathname: '/app', search: '?project=one' }, history)).toBeNull()
    expect(history.replaceState).not.toHaveBeenCalled()
  })
})
