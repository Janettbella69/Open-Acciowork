import { useCallback, useEffect, useRef, useState } from 'react'
import { errorMessage } from '../api/client'
import { collaborationAccessLost } from '../api/collaboration'

/** Each mounted project view owns its requests; late responses never cross navigation. */
export function useCollaborationRequest(onUnavailable: () => void) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const pending = useRef(false)
  const controller = useRef(new AbortController())
  useEffect(() => {
    // Controls can be clicked after commit but before passive effects run.
    // Reuse the initial controller; only an effect restart needs a fresh one.
    if (controller.current.signal.aborted) {
      controller.current = new AbortController()
      pending.current = false
    }
    const active = controller.current
    return () => active.abort()
  }, [])
  const run = useCallback(async <T,>(operation: (signal: AbortSignal) => Promise<T>, accept: (value: T) => void) => {
    const active = controller.current
    if (active.signal.aborted || pending.current) return
    pending.current = true; setBusy(true); setError('')
    try {
      const value = await operation(active.signal)
      if (!active.signal.aborted) accept(value)
    } catch (reason: unknown) {
      if (!active.signal.aborted) {
        if (collaborationAccessLost(reason)) onUnavailable()
        else setError(errorMessage(reason))
      }
    } finally {
      if (controller.current === active) pending.current = false
      if (!active.signal.aborted) setBusy(false)
    }
  }, [onUnavailable])
  return { run, busy, error }
}

export function useCommentRequestKey() {
  const saved = useRef<{ payload: string; key: string } | null>(null)
  return {
    forPayload(payload: unknown) {
      const value = JSON.stringify(payload)
      if (saved.current?.payload !== value) saved.current = { payload: value, key: crypto.randomUUID() }
      return saved.current.key
    },
    clear() { saved.current = null },
  }
}
