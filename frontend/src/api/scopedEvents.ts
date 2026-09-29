import type { EventActor } from './types'

export const projectEventTypes = [
  'project.changed', 'schedule.fired', 'schedule.skipped', 'schedule.completed',
  'schedule.failed', 'schedule.cancelled',
] as const
export const providerEventTypes = [
  'provider.preflight.started', 'provider.preflight.passed', 'provider.preflight.failed',
] as const

export type EventScope = 'project' | 'provider'
export type ScopedEvent = {
  id: string
  stream_id: string
  sequence: number
  type: typeof projectEventTypes[number] | typeof providerEventTypes[number]
  project_id: string | null
  run_id: string | null
  trace_id: string
  sdk_session_id: string | null
  provider_profile_id: string | null
  timestamp: string
  actor: EventActor
  data: Record<string, unknown>
}
export type ScopedEventHistory = {
  events: ScopedEvent[]
  last_sequence: number
  stream_id: string
}

export function scopedEventMatches(event: ScopedEvent, scope: EventScope, id: string, streamId: string): boolean {
  return !!event && event.stream_id === streamId
    && Number.isSafeInteger(event.sequence) && event.sequence > 0
    && event.id === `${streamId}:${event.sequence}`
    && (scope === 'project'
      ? event.project_id === id && (projectEventTypes as readonly string[]).includes(event.type)
      : event.provider_profile_id === id && (providerEventTypes as readonly string[]).includes(event.type))
    && !!event.data && typeof event.data === 'object' && !Array.isArray(event.data)
}
