import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { MemoryItem, ProviderProfile } from '../api/types'
import { I18nContext } from '../i18n'
import SettingsPage from './SettingsPage'

vi.mock('../api/client', () => ({
  listProviders: vi.fn(),
  saveProvider: vi.fn(),
  preflightProvider: vi.fn(),
  listMemory: vi.fn(),
  decideMemory: vi.fn(),
  errorMessage: (error: unknown) => (error instanceof Error ? error.message : 'unknown error'),
}))

import {
  decideMemory,
  listMemory,
  listProviders,
  preflightProvider,
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
  status: 'proposed',
  version: 1,
  created_at: '2026-08-20T10:00:00Z',
  updated_at: '2026-08-20T10:00:00Z',
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
  vi.mocked(decideMemory).mockImplementation(async (id, decision) => ({
    ...proposedMemory,
    id,
    status: decision === 'approve' ? 'approved' : 'rejected',
  }))
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
