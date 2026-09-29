import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  cancelWeixinPairing, createWeixinPairing, getWeixinOverview, pollWeixinPairing,
  revokeWeixinAccount, saveWeixinBinding,
} from './client'
import { weixinQrImage } from './weixin'

afterEach(() => vi.unstubAllGlobals())

describe('WeChat control API', () => {
  it('uses no-store requests, bounded identity paths, revision guards and a private verification body', async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) } as Response))
    vi.stubGlobal('fetch', fetchMock)
    const controller = new AbortController()
    await getWeixinOverview(controller.signal)
    await createWeixinPairing(controller.signal)
    await pollWeixinPairing('pair/one', undefined, controller.signal)
    await pollWeixinPairing('pair/one', '123456', controller.signal)
    await cancelWeixinPairing('pair/one', controller.signal)
    await saveWeixinBinding('account/one', { project_id: 'project_1', enabled: false, expected_revision: 7 }, controller.signal)
    await revokeWeixinAccount('account/one', 8, controller.signal)
    const calls = fetchMock.mock.calls as unknown as Array<[string, RequestInit]>
    expect(calls.map(([url]) => url)).toEqual([
      '/api/channels/weixin', '/api/channels/weixin/pairings',
      '/api/channels/weixin/pairings/pair%2Fone/poll', '/api/channels/weixin/pairings/pair%2Fone/poll',
      '/api/channels/weixin/pairings/pair%2Fone', '/api/channels/weixin/accounts/account%2Fone/binding',
      '/api/channels/weixin/accounts/account%2Fone?expected_revision=8',
    ])
    expect(calls.map(([, init]) => init.method ?? 'GET')).toEqual(['GET', 'POST', 'POST', 'POST', 'DELETE', 'PUT', 'DELETE'])
    expect(calls.every(([, init]) => init.cache === 'no-store' && init.signal === controller.signal)).toBe(true)
    expect(calls[2][1].body).toBe('{}')
    expect(calls[3][1].body).toBe('{"verify_code":"123456"}')
    expect(calls[5][1].body).toBe('{"project_id":"project_1","enabled":false,"expected_revision":7}')
    expect(calls.some(([url]) => url.includes('123456'))).toBe(false)
  })

  it('never displays verification input echoed in structured API validation details', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 422, json: async () => ({ detail: [{ loc: ['body', 'verify_code'], msg: 'Invalid code', input: 'private-verification-code' }] }) } as Response)))
    await expect(pollWeixinPairing('pair_1', 'private-verification-code')).rejects.toThrow('body.verify_code: Invalid code')
    await expect(pollWeixinPairing('pair_1', 'private-verification-code')).rejects.not.toThrow('private-verification-code')
  })

  it('accepts only bounded server SVG data images and never external QR URLs', () => {
    const image = 'data:image/svg+xml;base64,PHN2Zy8+'
    expect(weixinQrImage(image)).toBe(image)
    expect(weixinQrImage('https://example.com/login?private-token=one')).toBeNull()
    expect(weixinQrImage('javascript:alert(1)')).toBeNull()
    expect(weixinQrImage('data:text/html,<script>alert(1)</script>')).toBeNull()
    expect(weixinQrImage('data:image/svg+xml;base64,%%%%')).toBeNull()
    expect(weixinQrImage(`${image}${'A'.repeat(1_000_000)}`)).toBeNull()
  })
})
