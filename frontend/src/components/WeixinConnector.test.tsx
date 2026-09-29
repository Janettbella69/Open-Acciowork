import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import WeixinConnector from './WeixinConnector'
import {
  cancelWeixinPairing, createWeixinPairing, getWeixinOverview, pollWeixinPairing,
  revokeWeixinAccount, saveWeixinBinding,
} from '../api/client'
import type { WeixinPairing } from '../api/weixin'
import { deferredPromise } from '../test/scopedEventFixtures'
import { weixinAccount, weixinOverview, weixinPairing } from '../test/weixinFixtures'
import { I18nContext } from '../i18n'

vi.mock('../api/client', () => ({
  cancelWeixinPairing: vi.fn(), createWeixinPairing: vi.fn(), getWeixinOverview: vi.fn(),
  pollWeixinPairing: vi.fn(), revokeWeixinAccount: vi.fn(), saveWeixinBinding: vi.fn(),
  errorMessage: (cause: unknown) => cause instanceof Error ? cause.message : 'Request failed',
}))

beforeEach(() => {
  vi.resetAllMocks()
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-09-30T12:00:00Z'))
  vi.mocked(getWeixinOverview).mockResolvedValue(weixinOverview)
  vi.mocked(createWeixinPairing).mockResolvedValue(weixinPairing)
  vi.mocked(pollWeixinPairing).mockResolvedValue({ ...weixinPairing, status: 'scaned' })
  vi.mocked(cancelWeixinPairing).mockResolvedValue({ ...weixinPairing, status: 'cancelled', qr_svg_data_url: undefined })
  vi.mocked(saveWeixinBinding).mockImplementation(async (_id, binding) => ({
    ...weixinAccount, ...binding, revision: binding.expected_revision + 1,
    enabled_at: binding.enabled ? new Date().toISOString() : null,
  }))
  vi.mocked(revokeWeixinAccount).mockResolvedValue({ ...weixinAccount, status: 'revoked', revision: 2 })
})
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

async function mount() {
  let view!: ReturnType<typeof render>
  await act(async () => { view = render(<WeixinConnector />) })
  return view
}
async function connect() {
  await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Connect WeChat with QR code' })))
}
async function tick(milliseconds = 1000) { await act(async () => vi.advanceTimersByTimeAsync(milliseconds)) }

describe('WeChat connector', () => {
  it('connects personal WeChat, saves a local project and requires a separate explicit enable', async () => {
    await mount()
    expect(screen.getByText(/Personal use on this computer/)).toBeVisible()
    expect(screen.getByText(/Group chats and group mentions are not supported/)).toBeVisible()
    await connect()
    expect(screen.getByRole('img', { name: 'WeChat authorization QR code' })).toHaveAttribute('src', weixinPairing.qr_svg_data_url)
    await tick()
    expect(screen.getByText('Scanned. Confirm the connection in WeChat.')).toBeVisible()
    vi.mocked(pollWeixinPairing).mockResolvedValueOnce({ ...weixinPairing, status: 'confirmed', account_id: weixinAccount.id })
    vi.mocked(getWeixinOverview).mockResolvedValue({ ...weixinOverview, accounts: [weixinAccount] })
    await tick()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByText('Connected · disabled')).toBeVisible()
    expect(screen.getByText('Only direct messages sent after this account is enabled will start tasks. Earlier messages are not replayed automatically.')).toBeVisible()
    expect(saveWeixinBinding).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Enable WeChat direct messages' })).toBeDisabled()
    fireEvent.change(screen.getByRole('combobox', { name: 'Project for WeChat messages' }), { target: { value: 'project_1' } })
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Save WeChat project' })))
    expect(saveWeixinBinding).toHaveBeenLastCalledWith('account_1', { project_id: 'project_1', enabled: false, expected_revision: 1 }, expect.any(AbortSignal))
    expect(screen.getByText('Connected · disabled')).toBeVisible()
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Enable WeChat direct messages' })))
    expect(saveWeixinBinding).toHaveBeenLastCalledWith('account_1', { project_id: 'project_1', enabled: true, expected_revision: 2 }, expect.any(AbortSignal))
    expect(screen.getByRole('button', { name: 'Pause WeChat messages' })).toBeVisible()
    expect(screen.getByText(/Reply delivery is disabled in Settings/)).toBeVisible()
    expect(screen.queryByRole('button', { name: /export/i })).not.toBeInTheDocument()
  })

  it('shows organization management only to administrators and never offers another member mapping', async () => {
    vi.mocked(getWeixinOverview).mockResolvedValue({ ...weixinOverview, identity_mode: 'trusted_gateway', can_manage: false, accounts: [weixinAccount] })
    await mount()
    expect(screen.getByText(/An administrator manages WeChat connections/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Connect WeChat with QR code' })).not.toBeInTheDocument()
    expect(screen.queryByText(weixinAccount.bot_id)).not.toBeInTheDocument()
    expect(createWeixinPairing).not.toHaveBeenCalled()
    vi.mocked(getWeixinOverview).mockResolvedValue({ ...weixinOverview, identity_mode: 'trusted_gateway', accounts: [weixinAccount] })
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Refresh WeChat status' })))
    expect(screen.getByText(/current administrator identity/)).toBeVisible()
    expect(screen.getAllByRole('combobox')).toHaveLength(1)
  })

  it('cancels and clears a QR immediately, ignoring an in-flight poll that confirms later', async () => {
    const poll = deferredPromise<WeixinPairing>()
    vi.mocked(pollWeixinPairing).mockReturnValueOnce(poll.promise)
    await mount()
    await connect()
    await tick()
    const signal = vi.mocked(pollWeixinPairing).mock.calls[0][2]!
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Cancel WeChat connection' })))
    expect(signal.aborted).toBe(true)
    expect(cancelWeixinPairing).toHaveBeenCalledWith('pairing_1', expect.any(AbortSignal))
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    await act(async () => poll.resolve({ ...weixinPairing, status: 'confirmed', account_id: weixinAccount.id }))
    expect(screen.getByText('WeChat connection cancelled.')).toBeVisible()
    expect(getWeixinOverview).toHaveBeenCalledTimes(1)
    expect(saveWeixinBinding).not.toHaveBeenCalled()
  })

  it('honors a confirmation already recorded before cancellation reached the server', async () => {
    vi.mocked(cancelWeixinPairing).mockResolvedValue({ ...weixinPairing, status: 'confirmed', account_id: weixinAccount.id })
    await mount()
    await connect()
    vi.mocked(getWeixinOverview).mockResolvedValue({ ...weixinOverview, accounts: [weixinAccount] })
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Cancel WeChat connection' })))
    expect(screen.getByText('Connected · disabled')).toBeVisible()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(saveWeixinBinding).not.toHaveBeenCalled()
  })

  it('waits for an explicit verification code and clears it immediately after submission', async () => {
    vi.mocked(createWeixinPairing).mockResolvedValue({ ...weixinPairing, status: 'need_verifycode' })
    const poll = deferredPromise<WeixinPairing>()
    vi.mocked(pollWeixinPairing).mockReturnValueOnce(poll.promise)
    await mount()
    await connect()
    await tick(5000)
    expect(pollWeixinPairing).not.toHaveBeenCalled()
    const input = screen.getByLabelText('WeChat verification code')
    expect(input).toHaveAttribute('type', 'password')
    fireEvent.change(input, { target: { value: '123456' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit verification code' }))
    expect(input).toHaveValue('')
    expect(pollWeixinPairing).toHaveBeenCalledWith('pairing_1', '123456', expect.any(AbortSignal))
    await act(async () => poll.resolve({ ...weixinPairing, status: 'verify_code_blocked' }))
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('WeChat verification code')).not.toBeInTheDocument()
    await tick(5000)
    expect(pollWeixinPairing).toHaveBeenCalledTimes(1)
  })

  it('expires a QR locally and aborts a stalled poll without restoring it from a late response', async () => {
    const poll = deferredPromise<WeixinPairing>()
    vi.mocked(createWeixinPairing).mockResolvedValue({ ...weixinPairing, expires_at: '2026-09-30T12:00:02Z' })
    vi.mocked(pollWeixinPairing).mockReturnValueOnce(poll.promise)
    await mount()
    await connect()
    await tick(2000)
    expect(screen.getByText('This QR code expired. Start a new connection.')).toBeVisible()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(vi.mocked(pollWeixinPairing).mock.calls[0][2]!.aborted).toBe(true)
    await act(async () => poll.resolve({ ...weixinPairing, status: 'scaned' }))
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })

  it.each([401, 403])('clears QR, account data and verification drafts after access fails with %s', async (status) => {
    vi.mocked(getWeixinOverview).mockResolvedValue({ ...weixinOverview, accounts: [weixinAccount] })
    vi.mocked(createWeixinPairing).mockResolvedValue({ ...weixinPairing, status: 'need_verifycode' })
    vi.mocked(pollWeixinPairing).mockRejectedValueOnce(Object.assign(new Error('Access denied'), { status }))
    await mount()
    await connect()
    fireEvent.change(screen.getByLabelText('WeChat verification code'), { target: { value: '123456' } })
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Submit verification code' })))
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.queryByText(weixinAccount.bot_id)).not.toBeInTheDocument()
    expect(screen.queryByLabelText('WeChat verification code')).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('management access is no longer available')
  })

  it('reloads authoritative binding after a conflict and discards the old project draft', async () => {
    vi.mocked(getWeixinOverview).mockResolvedValueOnce({ ...weixinOverview, accounts: [weixinAccount] })
      .mockResolvedValue({ ...weixinOverview, accounts: [{ ...weixinAccount, project_id: 'project_2', revision: 3 }] })
    vi.mocked(saveWeixinBinding).mockRejectedValueOnce(Object.assign(new Error('Conflict'), { status: 409 }))
    await mount()
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'project_1' } })
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Save WeChat project' })))
    expect(screen.getByRole('combobox')).toHaveValue('project_2')
    expect(screen.getByRole('alert')).toHaveTextContent('settings changed')
    expect(saveWeixinBinding).toHaveBeenCalledTimes(1)
  })

  it('requires confirmation before revocation and never restores a revoked account', async () => {
    vi.mocked(getWeixinOverview).mockResolvedValue({ ...weixinOverview, accounts: [{ ...weixinAccount, project_id: 'project_1', enabled: true }] })
    await mount()
    fireEvent.click(screen.getByRole('button', { name: 'Revoke WeChat account' }))
    expect(revokeWeixinAccount).not.toHaveBeenCalled()
    await act(async () => fireEvent.click(screen.getByRole('button', { name: 'Confirm WeChat revocation' })))
    expect(revokeWeixinAccount).toHaveBeenCalledWith('account_1', 1, expect.any(AbortSignal))
    expect(screen.getByText('Revoked')).toBeVisible()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
  })

  it('does not load an external QR URL and never persists pairing material', async () => {
    const storage = vi.spyOn(Storage.prototype, 'setItem')
    vi.mocked(createWeixinPairing).mockResolvedValue({ ...weixinPairing, qr_svg_data_url: 'https://evil.invalid/private-qr' })
    const view = await mount()
    await connect()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('QR image is unavailable')
    view.unmount()
    expect(storage).not.toHaveBeenCalled()
    expect(vi.mocked(createWeixinPairing).mock.calls[0][0]!.aborted).toBe(true)
  })

  it('aborts pending authorization on unmount and ignores its late response', async () => {
    const create = deferredPromise<WeixinPairing>()
    vi.mocked(createWeixinPairing).mockReturnValueOnce(create.promise)
    const view = await mount()
    fireEvent.click(screen.getByRole('button', { name: 'Connect WeChat with QR code' }))
    view.unmount()
    expect(vi.mocked(createWeixinPairing).mock.calls[0][0]!.aborted).toBe(true)
    await act(async () => create.resolve(weixinPairing))
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
  })

  it('renders the personal setup in Chinese', async () => {
    vi.mocked(getWeixinOverview).mockResolvedValue({ ...weixinOverview, accounts: [weixinAccount] })
    await act(async () => render(<I18nContext.Provider value="zh-CN"><WeixinConnector /></I18nContext.Provider>))
    expect(screen.getByRole('heading', { name: '微信单聊' })).toBeVisible()
    expect(screen.getByRole('button', { name: '扫码连接微信' })).toBeEnabled()
    expect(within(screen.getByRole('region', { name: '微信单聊' })).getByText(/暂不支持群聊和群内 @/)).toBeVisible()
    expect(screen.getByText('只有本次启用后发送的单聊消息会启动任务，启用前的历史消息不会自动补跑。')).toBeVisible()
  })
})
