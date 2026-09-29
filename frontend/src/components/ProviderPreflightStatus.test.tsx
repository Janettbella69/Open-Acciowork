import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { getScopedEventHistory } from '../api/client'
import { scopedEvent, scopedHistory, ScopedEventSource } from '../test/scopedEventFixtures'
import ProviderPreflightStatus from './ProviderPreflightStatus'

vi.mock('../api/client', () => ({
  getScopedEventHistory: vi.fn(),
  scopedEventUrl: (_scope: string, id: string, after: number) => `/provider/${id}?after=${after}`,
  errorMessage: (cause: unknown) => cause instanceof Error ? cause.message : 'Connection failed',
}))

beforeEach(() => {
  vi.clearAllMocks()
  ScopedEventSource.instances = []
  vi.stubGlobal('EventSource', ScopedEventSource)
})
afterEach(() => vi.unstubAllGlobals())

it('distinguishes a lost observation from a confirmed Provider failure', async () => {
  const streamId = 'provider:local:deepseek'
  const interrupted = scopedEvent(2, {
    stream_id: streamId, project_id: null, provider_profile_id: 'deepseek',
    type: 'provider.preflight.failed', data: { status: 'failed', reason: 'interrupted' },
  })
  vi.mocked(getScopedEventHistory).mockResolvedValue(scopedHistory([interrupted], streamId))
  render(<ProviderPreflightStatus providerId="deepseek" onSettled={vi.fn()} />)
  expect(await screen.findByText(/Preflight result unconfirmed/)).toBeVisible()
  expect(screen.queryByText(/Preflight failed/)).not.toBeInTheDocument()
})

it('restores durable preflight state across navigation and refreshes the provider after live completion', async () => {
  const streamId = 'tenant:opaque:deepseek'
  const started = scopedEvent(10, { stream_id: streamId, project_id: null, provider_profile_id: 'deepseek', type: 'provider.preflight.started', data: { preflight_id: 'preflight_1', status: 'started', configuration_revision: 1 } })
  vi.mocked(getScopedEventHistory).mockResolvedValue(scopedHistory([started], streamId))
  const onSettled = vi.fn()
  const first = render(<ProviderPreflightStatus providerId="deepseek" onSettled={onSettled} />)
  expect(await screen.findByText(/Preflighting/)).toBeVisible()
  first.unmount()
  expect(ScopedEventSource.instances[0].closed).toBe(true)
  render(<ProviderPreflightStatus providerId="deepseek" onSettled={onSettled} />)
  expect(await screen.findByText(/Preflighting/)).toBeVisible()
  const failed = scopedEvent(11, { ...started, id: `${streamId}:11`, sequence: 11, type: 'provider.preflight.failed', data: { preflight_id: 'preflight_1', status: 'failed', code: 'AUTH_REVOKED_OR_INVALID', reason: 'configuration_changed', configuration_revision: 1 } })
  act(() => ScopedEventSource.instances[1].emit(failed))
  expect(screen.getByText(/Preflight failed/)).toBeVisible()
  expect(screen.getByText('AUTH_REVOKED_OR_INVALID')).toBeVisible()
  expect(onSettled).toHaveBeenCalledTimes(3)
  vi.mocked(getScopedEventHistory).mockRejectedValueOnce({ status: 403 })
  await act(async () => ScopedEventSource.instances[1].onerror?.())
  await waitFor(() => expect(screen.queryByText(/Preflight failed/)).not.toBeInTheDocument())
  expect(screen.getByRole('alert')).toHaveTextContent('no longer available')
})
