import type { ScopedEvent, ScopedEventHistory } from '../api/scopedEvents'

export function deferredPromise<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((accept, decline) => { resolve = accept; reject = decline })
  return { promise, resolve, reject }
}

export function scopedEvent(sequence: number, overrides: Partial<ScopedEvent> = {}): ScopedEvent {
  const streamId = overrides.stream_id ?? 'project:project_1'
  return {
    id: `${streamId}:${sequence}`, stream_id: streamId, sequence,
    type: 'project.changed', project_id: 'project_1', run_id: null,
    trace_id: 'trace_1', sdk_session_id: null, provider_profile_id: null,
    timestamp: '2026-09-29T12:00:00Z', actor: { type: 'system', name: 'Control plane', agent_id: null },
    data: { revision: sequence, resource_id: 'project_1', schema_version: 1 }, ...overrides,
  }
}

export function scopedHistory(events: ScopedEvent[] = [], streamId = 'project:project_1'): ScopedEventHistory {
  return { events, stream_id: streamId, last_sequence: events.at(-1)?.sequence ?? 0 }
}

export class ScopedEventSource {
  static instances: ScopedEventSource[] = []
  listeners = new Map<string, EventListener>()
  closed = false
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  constructor(public url: string) { ScopedEventSource.instances.push(this) }
  addEventListener(type: string, listener: EventListener) { this.listeners.set(type, listener) }
  close() { this.closed = true }
  emit(event: ScopedEvent) {
    this.listeners.get(event.type)?.(new MessageEvent(event.type, { data: JSON.stringify(event), lastEventId: event.id }))
  }
}
