import { apiBaseUrl } from './client'
import type { Notification, OpportunityProject, PendingPermissionView, PermissionAnswers, RunRecord } from './types'

export type PairingView = {
  id: string
  status: 'offered' | 'pending' | 'approved' | 'rejected' | 'revoked' | 'expired'
  project_ids: string[]
  allow_run_start: boolean
  created_at: string
  expires_at: string
  device_name: string | null
  verification_code: string | null
  approved_at: string | null
}

export type MobileStatus = {
  enabled: boolean
  public_origin: string | null
  pairing_ttl_seconds: number
  device_max_age_seconds: number
}

export type CompanionSession = {
  status: 'unpaired' | 'pending' | 'approved' | 'rejected' | 'revoked' | 'expired' | 'disabled'
  device: PairingView | null
}

export type CompanionOverview = {
  device: PairingView
  projects: OpportunityProject[]
  pending_permissions: PendingPermissionView[]
  notifications: Notification[]
  runs: RunRecord[]
}

export type PairingInput = { pairing_id: string; secret: string }
export type PairingFragment = { kind: 'none' } | { kind: 'invalid' } | { kind: 'pairing'; input: PairingInput }

// This flag selects a surface, never grants access. The server's HttpOnly cookie
// is the only device credential. Do not store pairing links or secrets here.
export const companionModeStorageKey = 'ophira-companion-mode'

export function rememberCompanionMode(): void {
  try { window.localStorage.setItem(companionModeStorageKey, 'true') } catch { /* Storage may be disabled. */ }
}

export function isCompanionModeRemembered(): boolean {
  try { return window.localStorage.getItem(companionModeStorageKey) === 'true' } catch { return false }
}

export function consumePairingFragment(): PairingFragment {
  const params = new URLSearchParams(window.location.hash.slice(1))
  if (!params.has('pairing') && !params.has('secret')) return { kind: 'none' }
  // Clear before any request or rendering, including malformed/duplicate values.
  window.history.replaceState(window.history.state, '', `${window.location.pathname}${window.location.search}`)
  rememberCompanionMode()
  const id = params.get('pairing') ?? ''
  const secret = params.get('secret') ?? ''
  if (params.getAll('pairing').length !== 1 || params.getAll('secret').length !== 1
    || !/^[A-Za-z0-9_-]{1,160}$/.test(id) || !/^[A-Za-z0-9_-]{16,256}$/.test(secret)) return { kind: 'invalid' }
  return { kind: 'pairing', input: { pairing_id: id, secret } }
}

async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    credentials: 'include',
    cache: 'no-store',
    headers: {
      Accept: 'application/json',
      'X-Ophira-Companion': '1',
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  if (!response.ok) {
    let message = `Request failed (${response.status})`
    try {
      const data = await response.json() as { detail?: unknown }
      if (typeof data.detail === 'string') message = data.detail
      // Never render validation inputs: a rejected pairing body contains a secret.
    } catch { /* A gateway can return a non-JSON failure. */ }
    throw new Error(message)
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>
}

export function getMobileStatus(): Promise<MobileStatus> {
  return request('/api/mobile/status')
}

export function listMobilePairings(): Promise<PairingView[]> {
  return request('/api/mobile/pairings')
}

export function createMobilePairing(input: { project_ids: string[]; allow_run_start: boolean }): Promise<{ pairing: PairingView; pairing_url: string }> {
  return request('/api/mobile/pairings', input)
}

export function decideMobilePairing(id: string, decision: 'approve' | 'reject', verificationCode?: string): Promise<PairingView> {
  return request(`/api/mobile/pairings/${encodeURIComponent(id)}/decision`, {
    decision,
    ...(verificationCode === undefined ? {} : { verification_code: verificationCode }),
  })
}

export function revokeMobilePairing(id: string): Promise<PairingView> {
  return request(`/api/mobile/pairings/${encodeURIComponent(id)}/revoke`, {})
}

export function pairCompanion(input: PairingInput, deviceName: string): Promise<{ status: 'pending'; device: PairingView }> {
  return request('/api/companion/pair', { ...input, device_name: deviceName })
}

export function getCompanionSession(): Promise<CompanionSession> {
  return request('/api/companion/session')
}

export function getCompanionOverview(): Promise<CompanionOverview> {
  return request('/api/companion/overview')
}

export function resolveCompanionPermission(runId: string, id: string, decision: 'allow_once' | 'deny', answers?: PermissionAnswers): Promise<void> {
  return request(`/api/companion/runs/${encodeURIComponent(runId)}/permissions/${encodeURIComponent(id)}`, {
    decision,
    reason: decision === 'deny' ? 'Denied by the user.' : '',
    ...(answers ? { answers } : {}),
  })
}

export function startCompanionRun(projectId: string, prompt: string): Promise<RunRecord> {
  return request(`/api/companion/projects/${encodeURIComponent(projectId)}/runs`, { prompt })
}

export function cancelCompanionRun(id: string): Promise<RunRecord> {
  return request(`/api/companion/runs/${encodeURIComponent(id)}/cancel`, {})
}

export function logoutCompanion(): Promise<Record<string, never>> {
  return request('/api/companion/logout', {})
}
