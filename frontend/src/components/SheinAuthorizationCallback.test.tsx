import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Connector } from '../api/types'
import type { SheinAuthorizationView } from '../api/marketplaces'
import { I18nContext } from '../i18n'
import SheinAuthorizationCallback from './SheinAuthorizationCallback'

vi.mock('../api/client', () => ({
  getSheinAuthorization: vi.fn(), finishSheinAuthorization: vi.fn(), cancelSheinAuthorization: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))
import { cancelSheinAuthorization, finishSheinAuthorization, getSheinAuthorization } from '../api/client'

const callback = { connectorId: 'shein', appId: 'synthetic-app', state: `${'a'.repeat(32)}.${'b'.repeat(64)}`, tempToken: 'synthetic-token' }
const view: SheinAuthorizationView = { app_id: callback.appId, endpoint: 'https://openapi.sheincorp.com', scope: 'shein:store:read shein:products:read', supplier_id: null, expires_at: 9999999999, status: 'pending' }
const connector: Connector = { id: 'shein', type: 'shein', display_name: 'SHEIN', scope: view.scope, endpoint: view.endpoint, secret_env: 'OPHIRA_CONNECTOR_SHEIN_KEY', enabled: true, health: 'unknown', last_checked_at: null, created_at: '2026-09-22T00:00:00Z' }

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(getSheinAuthorization).mockResolvedValue(view)
  vi.mocked(finishSheinAuthorization).mockResolvedValue(connector)
  vi.mocked(cancelSheinAuthorization).mockResolvedValue({ status: 'cancelled' })
})

async function enterSecret() {
  const input = await screen.findByLabelText('SHEIN app secret')
  fireEvent.change(input, { target: { value: 'synthetic-app-secret-123' } })
  return input
}

describe('SHEIN authorization completion', () => {
  it('reads pending state but exchanges only after human submission, then clears the app secret', async () => {
    render(<SheinAuthorizationCallback callback={callback} />)
    const input = await enterSecret()
    expect(input).toHaveAttribute('type', 'password')
    expect(finishSheinAuthorization).not.toHaveBeenCalled()
    expect(document.body.textContent).not.toContain(callback.tempToken)
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save SHEIN connection' }))
    await screen.findByText(/SHEIN authorization saved/)
    expect(finishSheinAuthorization).toHaveBeenCalledWith('shein', { state: callback.state, app_id: callback.appId, app_secret: 'synthetic-app-secret-123', temp_token: callback.tempToken })
    expect(screen.queryByLabelText('SHEIN app secret')).not.toBeInTheDocument()
  })

  it('locks an ambiguous response until status is checked and recovers without resending credentials', async () => {
    vi.mocked(finishSheinAuthorization).mockRejectedValueOnce(new Error('lost response'))
    render(<SheinAuthorizationCallback callback={callback} />)
    const input = await enterSecret()
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save SHEIN connection' }))
    await screen.findByRole('alert')
    expect(input).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Verify and save SHEIN connection' })).toBeDisabled()
    vi.mocked(getSheinAuthorization).mockResolvedValue({ ...view, status: 'installing' })
    fireEvent.click(screen.getByRole('button', { name: 'Check authorization status' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Recover saved connection' }))
    await screen.findByText(/SHEIN authorization saved/)
    expect(finishSheinAuthorization).toHaveBeenLastCalledWith('shein', { state: callback.state })
  })

  it('rejects a callback for a different app before accepting its secret', async () => {
    vi.mocked(getSheinAuthorization).mockResolvedValue({ ...view, app_id: 'other-app' })
    render(<SheinAuthorizationCallback callback={callback} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('does not match')
    expect(screen.queryByLabelText('SHEIN app secret')).not.toBeInTheDocument()
    expect(finishSheinAuthorization).not.toHaveBeenCalled()
  })

  it.each(['expired', 'cancelled', 'failed', 'exchanging'] as const)('does not exchange an unusable or previously attempted flow: %s', async (status) => {
    vi.mocked(getSheinAuthorization).mockResolvedValue({ ...view, status })
    render(<SheinAuthorizationCallback callback={callback} />)
    await screen.findByText(callback.appId)
    expect(screen.queryByLabelText('SHEIN app secret')).not.toBeInTheDocument()
    expect(finishSheinAuthorization).not.toHaveBeenCalled()
  })

  it('cancels a pending flow explicitly', async () => {
    render(<SheinAuthorizationCallback callback={callback} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Cancel authorization' }))
    await screen.findByText('This authorization is no longer usable. Start again from Connectors.')
    expect(cancelSheinAuthorization).toHaveBeenCalledWith('shein', callback.state)
  })

  it('does not exchange twice while a submission is pending', async () => {
    let resolve!: (value: Connector) => void
    vi.mocked(finishSheinAuthorization).mockImplementation(() => new Promise((done) => { resolve = done }))
    render(<SheinAuthorizationCallback callback={callback} />)
    await enterSecret()
    const button = screen.getByRole('button', { name: 'Verify and save SHEIN connection' })
    fireEvent.click(button); fireEvent.click(button)
    expect(finishSheinAuthorization).toHaveBeenCalledTimes(1)
    await act(async () => resolve(connector))
    await screen.findByText(/SHEIN authorization saved/)
  })

  it('does not render a late status result after unmount', async () => {
    let resolve!: (value: SheinAuthorizationView) => void
    vi.mocked(getSheinAuthorization).mockImplementation(() => new Promise((done) => { resolve = done }))
    const page = render(<SheinAuthorizationCallback callback={callback} />)
    page.unmount()
    await act(async () => resolve(view))
    expect(screen.queryByLabelText('SHEIN app secret')).not.toBeInTheDocument()
  })

  it('shows incomplete callbacks without calling the API', () => {
    render(<SheinAuthorizationCallback callback={null} />)
    expect(screen.getByRole('alert')).toHaveTextContent('incomplete')
    expect(getSheinAuthorization).not.toHaveBeenCalled()
  })

  it('provides Chinese completion and scope copy', async () => {
    render(<I18nContext.Provider value="zh-CN"><SheinAuthorizationCallback callback={callback} /></I18nContext.Provider>)
    await waitFor(() => expect(screen.getByLabelText('SHEIN 应用密钥')).toBeInTheDocument())
    expect(screen.getByText('允许读取店铺和商品')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '验证并保存 SHEIN 连接' })).toBeInTheDocument()
  })
})
