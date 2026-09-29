import { useEffect, useRef, useState } from 'react'
import { errorMessage, getScopedEventHistory, scopedEventUrl } from './client'
import { projectEventTypes, providerEventTypes, scopedEventMatches, type EventScope, type ScopedEvent } from './scopedEvents'

export type ScopedEventsOptions = {
  scope: EventScope
  id: string | null
  live?: boolean
  onEvent?: (event: ScopedEvent) => void
  onReady?: () => void
  onUnavailable?: () => void
}
type Snapshot = {
  key: string
  events: ScopedEvent[]
  status: 'loading' | 'live' | 'paused' | 'reconnecting' | 'unavailable' | 'error'
  error: string
}

export function useScopedEvents({ scope, id, live = true, onEvent, onReady, onUnavailable }: ScopedEventsOptions) {
  const key = `${scope}:${id ?? ''}`
  const [snapshot, setSnapshot] = useState<Snapshot>({ key: '', events: [], status: 'loading', error: '' })
  const [revision, setRevision] = useState(0)
  const callbacks = useRef({ onEvent, onReady, onUnavailable })
  useEffect(() => { callbacks.current = { onEvent, onReady, onUnavailable } }, [onEvent, onReady, onUnavailable])

  useEffect(() => {
    if (!id) return
    let active = true
    let source: EventSource | null = null
    let checkingAccess = false
    let sequence = 0
    const controller = new AbortController()
    const update = (patch: Partial<Snapshot>) => {
      if (active) setSnapshot((current) => ({
        key, events: current.key === key ? current.events : [], status: 'loading', error: '', ...patch,
      }))
    }
    const fail = (cause: unknown) => {
      if (!active) return
      if (cause && typeof cause === 'object' && 'status' in cause && (cause.status === 401 || cause.status === 403 || cause.status === 404)) {
        source?.close()
        update({ events: [], status: 'unavailable', error: 'Event history is no longer available.' })
        active = false
        controller.abort()
        callbacks.current.onUnavailable?.()
      } else {
        update({ status: 'error', error: errorMessage(cause) })
      }
    }
    update({ status: 'loading' })
    void getScopedEventHistory(scope, id, { tail: true, limit: 200, signal: controller.signal })
      .then((page) => {
        if (!active) return
        if (!page.stream_id || !Number.isSafeInteger(page.last_sequence) || page.last_sequence < 0
          || !Array.isArray(page.events) || page.events.some((event) =>
            !scopedEventMatches(event, scope, id, page.stream_id) || event.sequence > page.last_sequence)) {
          throw new Error('The event history could not be verified. Refresh to retry.')
        }
        sequence = page.last_sequence
        const events = [...new Map(page.events.map((event) => [event.id, event])).values()]
          .sort((a, b) => a.sequence - b.sequence)
        update({ events, status: live ? 'loading' : 'paused' })
        callbacks.current.onReady?.()
        if (!live || !active) return
        source = new EventSource(scopedEventUrl(scope, id, sequence))
        source.onopen = () => update({ status: 'live' })
        const receive = (message: MessageEvent<string>) => {
          if (!active) return
          let event: ScopedEvent
          try { event = JSON.parse(message.data) as ScopedEvent } catch { return }
          if (!scopedEventMatches(event, scope, id, page.stream_id) || event.sequence <= sequence) return
          if (event.sequence !== sequence + 1) {
            source?.close()
            active = false
            controller.abort()
            setRevision((current) => current + 1)
            return
          }
          sequence = event.sequence
          setSnapshot((current) => ({ key, events: [...(current.key === key ? current.events : []), event].slice(-200), status: 'live', error: '' }))
          callbacks.current.onEvent?.(event)
        }
        for (const type of scope === 'project' ? projectEventTypes : providerEventTypes) {
          source.addEventListener(type, receive as EventListener)
        }
        source.onerror = () => {
          if (!active) return
          update({ status: 'reconnecting' })
          // EventSource hides HTTP status. Check access separately while native
          // reconnect preserves its exact Last-Event-ID cursor.
          if (checkingAccess) return
          checkingAccess = true
          void getScopedEventHistory(scope, id, { tail: true, limit: 1, signal: controller.signal })
            .then((check) => {
              if (!active || check.stream_id === page.stream_id) return
              source?.close()
              update({ events: [], status: 'loading' })
              active = false
              controller.abort()
              setRevision((current) => current + 1)
            })
            .catch(fail).finally(() => { checkingAccess = false })
        }
      }).catch(fail)
    return () => { active = false; controller.abort(); source?.close() }
  }, [scope, id, key, live, revision])

  return {
    events: id && snapshot.key === key ? snapshot.events : [],
    status: id && snapshot.key === key ? snapshot.status : 'loading',
    error: id && snapshot.key === key ? snapshot.error : '',
    refresh: () => setRevision((current) => current + 1),
  }
}
