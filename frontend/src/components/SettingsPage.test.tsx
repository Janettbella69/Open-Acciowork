import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { MemoryItem, OrgPolicy, PermissionGrant, ProviderProfile } from '../api/types'
import { I18nContext } from '../i18n'
import SettingsPage from './SettingsPage'

vi.mock('../api/client', () => ({
  listProviders: vi.fn(),
  saveProvider: vi.fn(),
  preflightProvider: vi.fn(),
  listMemory: vi.fn(),
  decideMemory: vi.fn(),
  correctMemory: vi.fn(),
  getOrgPolicy: vi.fn(),
  saveOrgPolicy: vi.fn(),
  listPermissionGrants: vi.fn(),
  revokePermissionGrant: vi.fn(),
  errorMessage: (error: unknown) => (error instanceof Error ? error.message : 'unknown error'),
}))

import {
  correctMemory,
  decideMemory,
  getOrgPolicy,
  listMemory,
  listPermissionGrants,
  listProviders,
  preflightProvider,
  revokePermissionGrant,
  saveOrgPolicy,
  saveProvider,
} from '../api/client'

const deepseekProfile: ProviderProfile = {
  id: 'deepseek',
  display_name: 'DeepSeek (api.deepseek.com)',
  base_url: 'https://api.deepseek.com/anthropic',
  auth_env_name: 'ANTHROPIC_AUTH_TOKEN',
  model: 'deepseek-v4-pro',
  region: 'cn',
  production_eligible: false,
  enabled: true,
  secret_configured: true,
  last_verified_at: '2026-08-20T11:11:35Z',
  last_error_code: null,
  last_error_message: null,
  capabilities: {
    tool_use: true,
    streaming: true,
    images: false,
    prompt_caching: false,
    extended_thinking: false,
    tool_search: false,
  },
}

const proposedMemory: MemoryItem = {
  id: 'mem_1',
  scope: 'project',
  scope_id: 'project_1',
  text: '用户偏好海运小批量补货',
  kind: 'preference',
  sensitivity: 'normal',
  status: 'proposed',
  version: 1,
  created_at: '2026-08-20T10:00:00Z',
  updated_at: '2026-08-20T10:00:00Z',
}

const permissionGrant: PermissionGrant = {
  id: 'grant_1',
  project_id: 'project_1',
  tool_name: 'Bash',
  path_prefix: 'path:/workspace/reports',
  decision: 'allow_persistent',
  expires_at: '2026-09-20T10:00:00Z',
  created_at: '2026-08-20T10:00:00Z',
  revoked: false,
}

const orgPolicy: OrgPolicy = {
  permission_mode: 'default',
  allowed_provider_ids: [],
  allowed_skill_ids: [],
  browser_enabled: true,
  computer_control_enabled: false,
  channel_delivery_enabled: false,
  domain_allowlist: [],
  updated_at: '2026-09-02T00:00:00Z',
}

function stubLocalStorage() {
  const stored = new Map<string, string>()
  Object.defineProperty(window, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => stored.get(key) ?? null,
      setItem: (key: string, value: string) => stored.set(key, value),
    },
  })
  return stored
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listProviders).mockResolvedValue([deepseekProfile])
  vi.mocked(listMemory).mockResolvedValue([proposedMemory])
  vi.mocked(saveProvider).mockImplementation(async (id, input) => ({
    ...deepseekProfile,
    id,
    display_name: input.display_name,
  }))
  vi.mocked(preflightProvider).mockResolvedValue({
    provider_profile_id: 'deepseek',
    ok: true,
    error_code: null,
    message: 'Claude Agent SDK、MCP 与 Plugin Skill 预检通过。',
    sdk_session_id: 'sess-1',
    model: 'deepseek-v4-pro',
    mcp_connected: true,
    discovered_skills: ['ophira-core:global-opportunity-execution'],
    checked_at: '2026-08-20T12:00:00Z',
  })
  vi.mocked(getOrgPolicy).mockResolvedValue(orgPolicy)
  vi.mocked(saveOrgPolicy).mockImplementation(async (policy) => policy)
  vi.mocked(listPermissionGrants).mockResolvedValue([])
  vi.mocked(decideMemory).mockImplementation(async (id, decision) => ({
    ...proposedMemory,
    id,
    status: decision === 'approve' ? 'approved' : 'rejected',
  }))
  vi.mocked(correctMemory).mockImplementation(async (id, input) => ({
    ...proposedMemory,
    id,
    ...input,
    status: 'approved',
    version: proposedMemory.version + 1,
  }))
  vi.mocked(revokePermissionGrant).mockImplementation(async (id) => ({ ...permissionGrant, id, revoked: true }))
})

describe('SettingsPage · provider management', () => {
  it('lists configured providers with status and never renders the key', async () => {
    stubLocalStorage()
    render(<SettingsPage />)

    const row = (await screen.findByText('DeepSeek (api.deepseek.com)')).closest('article')!
    expect(within(row).getByText(/deepseek-v4-pro/)).toBeInTheDocument()
    expect(within(row).getByText(/key configured/i)).toBeInTheDocument()
    expect(within(row).getByText(/verified/i)).toBeInTheDocument()
    expect(within(row).queryByDisplayValue(/sk-/)).not.toBeInTheDocument()
  })

  it('saves edits without echoing or resending a blank secret', async () => {
    stubLocalStorage()
    render(<SettingsPage />)
    const row = (await screen.findByText('DeepSeek (api.deepseek.com)')).closest('article')!

    fireEvent.click(within(row).getByRole('button', { name: /edit/i }))
    const form = screen.getByRole('form', { name: /provider editor/i })
    const secretInput = within(form).getByLabelText(/api key/i) as HTMLInputElement
    expect(secretInput.value).toBe('')
    expect(secretInput.type).toBe('password')

    fireEvent.change(within(form).getByLabelText(/display name/i), {
      target: { value: 'DeepSeek prod' },
    })
    fireEvent.click(within(form).getByRole('button', { name: /save provider/i }))

    await waitFor(() => expect(saveProvider).toHaveBeenCalledTimes(1))
    const [savedId, payload] = vi.mocked(saveProvider).mock.calls[0]
    expect(savedId).toBe('deepseek')
    expect(payload.display_name).toBe('DeepSeek prod')
    expect('secret' in payload).toBe(false)
  })

  it('runs preflight and surfaces the actionable error message on failure', async () => {
    stubLocalStorage()
    vi.mocked(preflightProvider).mockResolvedValue({
      provider_profile_id: 'deepseek',
      ok: false,
      error_code: 'QUOTA_OR_BILLING',
      message: 'Provider 额度或计费状态异常，请充值或检查套餐。',
      sdk_session_id: null,
      model: 'deepseek-v4-pro',
      mcp_connected: false,
      discovered_skills: [],
      checked_at: '2026-08-20T12:00:00Z',
    })
    render(<SettingsPage />)
    const row = (await screen.findByText('DeepSeek (api.deepseek.com)')).closest('article')!

    fireEvent.click(within(row).getByRole('button', { name: /run preflight/i }))

    expect(await screen.findByText(/额度或计费状态异常/)).toBeInTheDocument()
    expect(preflightProvider).toHaveBeenCalledWith('deepseek')
  })

  it('approves a proposed memory through the backend decision endpoint', async () => {
    stubLocalStorage()
    render(<SettingsPage />)

    const item = (await screen.findByText(/海运小批量补货/)).closest('article')!
    fireEvent.click(within(item).getByRole('button', { name: /approve/i }))

    await waitFor(() => expect(decideMemory).toHaveBeenCalledWith('mem_1', 'approve'))
    // The item leaves the pending queue and lands in the reviewed history.
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: /approve/i })).not.toBeInTheDocument()
    })
    expect(screen.getByText(/no memory proposals are waiting/i)).toBeInTheDocument()
    expect(screen.getByText(/reviewed memories \(1\)/i)).toBeInTheDocument()
  })

  it('saves a user correction as a new approved memory version', async () => {
    stubLocalStorage()
    render(<SettingsPage />)

    const item = (await screen.findByText(/海运小批量补货/)).closest('article')!
    fireEvent.click(within(item).getByRole('button', { name: /^edit$/i }))
    const editor = within(item).getByRole('form', { name: /edit memory/i })
    fireEvent.change(within(editor).getByLabelText(/memory text/i), {
      target: { value: '用户偏好空运小批量补货' },
    })
    fireEvent.change(within(editor).getByLabelText(/sensitivity/i), {
      target: { value: 'sensitive' },
    })
    fireEvent.click(within(editor).getByRole('button', { name: /^save$/i }))

    await waitFor(() => expect(correctMemory).toHaveBeenCalledWith('mem_1', {
      text: '用户偏好空运小批量补货',
      kind: 'preference',
      sensitivity: 'sensitive',
    }))
    expect(await screen.findByText(/用户偏好空运小批量补货/)).toBeInTheDocument()
    expect(screen.getByText(/reviewed memories \(1\)/i)).toBeInTheDocument()
  })

  it('allows a reviewed memory to enter and cancel the same editor', async () => {
    stubLocalStorage()
    vi.mocked(listMemory).mockResolvedValue([{ ...proposedMemory, status: 'approved' }])
    render(<SettingsPage />)

    fireEvent.click(await screen.findByText(/reviewed memories \(1\)/i))
    const item = screen.getByText(/海运小批量补货/).closest('article')!
    fireEvent.click(within(item).getByRole('button', { name: /^edit$/i }))
    expect(within(item).getByRole('form', { name: /edit memory/i })).toBeInTheDocument()
    fireEvent.click(within(item).getByRole('button', { name: /^cancel$/i }))
    expect(within(item).queryByRole('form', { name: /edit memory/i })).not.toBeInTheDocument()
    expect(correctMemory).not.toHaveBeenCalled()
  })

  it('keeps the memory editor open and reports a failed correction', async () => {
    stubLocalStorage()
    let rejectEdit!: (reason: unknown) => void
    vi.mocked(correctMemory).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectEdit = reject }))
    render(<SettingsPage />)

    const item = (await screen.findByText(/海运小批量补货/)).closest('article')!
    fireEvent.click(within(item).getByRole('button', { name: /^edit$/i }))
    const editor = within(item).getByRole('form', { name: /edit memory/i })
    const save = within(editor).getByRole('button', { name: /^save$/i })
    fireEvent.click(save)
    fireEvent.click(save)
    await waitFor(() => expect(correctMemory).toHaveBeenCalledTimes(1))
    expect(within(editor).getByRole('button', { name: /saving/i })).toBeDisabled()
    expect(within(editor).getByRole('button', { name: /^cancel$/i })).toBeDisabled()

    await act(async () => { rejectEdit(new Error('Correction failed')) })
    expect(await screen.findByRole('alert')).toHaveTextContent('Correction failed')
    expect(within(item).getByRole('form', { name: /edit memory/i })).toBeInTheDocument()
    expect(within(item).getByRole('button', { name: /^save$/i })).not.toBeDisabled()
  })
})

describe('SettingsPage · organization policy and grants', () => {
  it('locks the policy controls while a save is in flight', async () => {
    stubLocalStorage()
    let resolveSave!: (value: OrgPolicy) => void
    vi.mocked(saveOrgPolicy).mockImplementationOnce(() => new Promise((resolve) => { resolveSave = resolve }))
    render(<SettingsPage />)

    const mode = await screen.findByRole('combobox', { name: /organization permission mode/i })
    const browser = screen.getByRole('switch', { name: /browser \(org policy\)/i })
    fireEvent.change(mode, { target: { value: 'auto' } })

    expect(mode).toBeDisabled()
    expect(browser).toBeDisabled()
    expect(screen.getByText(/saving organization policy/i)).toHaveAttribute('role', 'status')

    await waitFor(() => expect(saveOrgPolicy).toHaveBeenCalledTimes(1))
    await act(async () => { resolveSave({ ...orgPolicy, permission_mode: 'auto' }) })
    await waitFor(() => expect(mode).not.toBeDisabled())
    expect(mode).toHaveValue('auto')
  })

  it('keeps grants usable when policy loading fails and reports revoke errors', async () => {
    stubLocalStorage()
    vi.mocked(getOrgPolicy).mockRejectedValue(new Error('Policy unavailable'))
    vi.mocked(listPermissionGrants).mockResolvedValue([permissionGrant])
    vi.mocked(revokePermissionGrant).mockRejectedValue(new Error('Grant revoke failed'))
    render(<SettingsPage />)

    const toolName = await screen.findByText('Bash')
    expect(screen.getByText(/scope: path:\/workspace\/reports/i)).toBeInTheDocument()
    fireEvent.click(toolName)
    expect(revokePermissionGrant).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: /revoke grant/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Grant revoke failed')
    expect(revokePermissionGrant).toHaveBeenCalledWith('grant_1')
  })
})

describe('SettingsPage · local preferences', () => {
  it('keeps high-risk access disabled until the user enables it', async () => {
    const stored = stubLocalStorage()
    render(<SettingsPage />)
    await screen.findByText('DeepSeek (api.deepseek.com)')

    const fullAccess = screen.getByRole('switch', { name: /full access/i })
    expect(fullAccess).not.toBeChecked()

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
  })

  it('localizes provider, memory, and permission controls in Chinese', async () => {
    stubLocalStorage()
    render(
      <I18nContext.Provider value="zh-CN">
        <SettingsPage />
      </I18nContext.Provider>,
    )

    const row = (await screen.findByText('DeepSeek (api.deepseek.com)')).closest('article')!
    expect(within(row).getByText('密钥已配置')).toBeInTheDocument()
    expect(within(row).getByRole('button', { name: '运行预检' })).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: '设置分区' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '批准' })).toBeInTheDocument()
    expect(screen.getByRole('combobox', { name: '插件访问权限' })).toBeInTheDocument()
  })
})
