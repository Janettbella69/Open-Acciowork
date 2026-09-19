import { useCallback, useEffect, useRef, useState } from 'react'
import { errorMessage, getConversationDeletionOperation, getProjectDeletionOperation, retryConversationDeletion, retryProjectDeletion, submitConversationDeletion, submitProjectDeletion } from './client'
import { requireDeletionOperation, type DeletionHandle, type ProjectDeletionOperation, type ProjectDeletionRequest } from './projectDeletion'

export type DeletionAttempt = {
  handle: DeletionHandle
  request: ProjectDeletionRequest | null
  operation: ProjectDeletionOperation | null
  accepted: boolean
  phase: 'unknown' | 'missing' | 'forbidden' | 'rejected' | 'ready'
  error: string
}
type Action = 'submit' | 'query' | 'retry' | null

function status(reason: unknown): number | null {
  return reason && typeof reason === 'object' && 'status' in reason ? Number(reason.status) : null
}

function importedAttempt(scope: string): DeletionAttempt | null {
  const params = new URLSearchParams(window.location.search)
  const prefix = scope === 'conversation' ? 'conversation_deletion' : 'deletion'
  const projectId = params.get(`${prefix}_project`) ?? ''
  const operationId = params.get(`${prefix}_operation`) ?? ''
  const runId = scope === 'conversation' ? params.get(`${prefix}_run`) ?? '' : undefined
  if (scope === 'conversation' && !/^run_[A-Za-z0-9_-]+$/.test(runId ?? '')) return null
  if (!/^[A-Za-z0-9_-]{1,160}$/.test(projectId) || !/^[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}$/i.test(operationId)) return null
  return { handle: { projectId, operationId, ...(runId ? { runId } : {}) }, request: null, operation: null, accepted: false, phase: 'unknown', error: '' }
}

function saveHandle(handle: DeletionHandle | null, scope: string) {
  const url = new URL(window.location.href)
  const prefix = scope === 'conversation' ? 'conversation_deletion' : 'deletion'
  if (handle) {
    url.searchParams.delete('brief')
    url.searchParams.set(`${prefix}_project`, handle.projectId)
    url.searchParams.set(`${prefix}_operation`, handle.operationId)
    if (handle.runId) url.searchParams.set(`${prefix}_run`, handle.runId)
  } else {
    url.searchParams.delete(`${prefix}_project`)
    url.searchParams.delete(`${prefix}_operation`)
    url.searchParams.delete(`${prefix}_run`)
  }
  window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`)
}

/** The operation outlives its project view. Only its opaque identifiers survive reload. */
export function useProjectDeletion(
  onAccepted: (operation: ProjectDeletionOperation) => void,
  onAccessCheck?: (projectId: string) => void,
  scope: 'project' | 'conversation' = 'project',
) {
  const [attempt, setAttempt] = useState(() => importedAttempt(scope))
  const [action, setAction] = useState<Action>(null)
  const attemptRef = useRef(attempt)
  const pending = useRef(false)
  const controller = useRef(new AbortController())
  const acceptedCallback = useRef(onAccepted)
  const accessCheckCallback = useRef(onAccessCheck)
  useEffect(() => {
    acceptedCallback.current = onAccepted
    accessCheckCallback.current = onAccessCheck
  }, [onAccepted, onAccessCheck])
  const update = useCallback((next: DeletionAttempt | null) => {
    attemptRef.current = next
    setAttempt(next)
  }, [])
  const current = useCallback((target: DeletionAttempt, signal: AbortSignal) => !signal.aborted
    && attemptRef.current?.handle.operationId === target.handle.operationId
    && attemptRef.current.handle.projectId === target.handle.projectId, [])

  const accept = useCallback((target: DeletionAttempt, value: ProjectDeletionOperation) => {
    requireDeletionOperation(target.handle, value)
    const previous = attemptRef.current!
    update({ ...previous, operation: value, request: null, accepted: true, phase: 'ready', error: '' })
    if (!previous.accepted || (scope === 'conversation' && previous.operation?.status !== value.status)) acceptedCallback.current(value)
  }, [scope, update])

  const query = useCallback(async (target: DeletionAttempt, signal: AbortSignal) => {
    try {
      const getOperation = scope === 'conversation' ? getConversationDeletionOperation : getProjectDeletionOperation
      const result = await getOperation(target.handle.projectId, target.handle.operationId, signal)
      if (current(target, signal)) accept(target, result)
    } catch (cause) {
      if (!current(target, signal)) return
      const previous = attemptRef.current!
      if (status(cause) === 403 || status(cause) === 404) {
        update({ ...previous, operation: null, request: status(cause) === 403 ? null : previous.request,
          phase: status(cause) === 403 ? 'forbidden' : 'missing', error: '' })
      } else update({ ...previous, error: errorMessage(cause) })
    }
  }, [accept, current, scope, update])

  const refresh = useCallback(async () => {
    const target = attemptRef.current
    if (!target || pending.current || controller.current.signal.aborted) return
    const active = controller.current
    pending.current = true; setAction('query')
    try { await query(target, active.signal) }
    finally { if (!active.signal.aborted) { pending.current = false; setAction(null) } }
  }, [query])

  const submit = useCallback(async (projectId: string, request: ProjectDeletionRequest, runId?: string) => {
    if (pending.current || controller.current.signal.aborted) return
    if (scope === 'conversation' && !runId) return
    const previous = attemptRef.current
    if (previous && (previous.accepted || previous.handle.operationId !== request.operation_id)) return
    const target: DeletionAttempt = { handle: { projectId, operationId: request.operation_id, ...(runId ? { runId } : {}) }, request,
      operation: null, accepted: false, phase: 'unknown', error: '' }
    saveHandle(target.handle, scope)
    update(target)
    const active = controller.current
    pending.current = true; setAction('submit')
    try {
      const result = scope === 'conversation'
        ? await submitConversationDeletion(projectId, runId!, request, active.signal)
        : await submitProjectDeletion(projectId, request, active.signal)
      if (current(target, active.signal)) accept(target, result)
    } catch (cause) {
      if (!current(target, active.signal)) return
      if ([403, 404, 409].includes(status(cause) ?? 0)) {
        update({ ...target, request: null, phase: status(cause) === 403 ? 'forbidden' : 'rejected',
          error: status(cause) === 403 ? '' : errorMessage(cause) })
        if (status(cause) === 403 || status(cause) === 404) accessCheckCallback.current?.(projectId)
      } else {
        update({ ...target, error: errorMessage(cause) })
        // A failed response does not prove the irreversible request failed.
        setAction('query')
        await query(target, active.signal)
      }
    } finally { if (!active.signal.aborted) { pending.current = false; setAction(null) } }
  }, [accept, current, query, scope, update])

  const retry = useCallback(async () => {
    const target = attemptRef.current
    if (!target || pending.current || !target.operation || !['deleting', 'blocked'].includes(target.operation.status) || !target.operation.can_retry
      || controller.current.signal.aborted) return
    const active = controller.current
    pending.current = true; setAction('retry')
    try {
      const retryOperation = scope === 'conversation' ? retryConversationDeletion : retryProjectDeletion
      const result = await retryOperation(target.handle.projectId, target.handle.operationId, active.signal)
      if (current(target, active.signal)) accept(target, result)
    } catch (cause) {
      if (!current(target, active.signal)) return
      update({ ...target, error: errorMessage(cause) })
      setAction('query')
      await query(target, active.signal)
    } finally { if (!active.signal.aborted) { pending.current = false; setAction(null) } }
  }, [accept, current, query, scope, update])

  const dismiss = useCallback(() => {
    const target = attemptRef.current
    if (pending.current || !target || (target.accepted && target.operation?.status !== 'deleted')
      || (!target.accepted && !['missing', 'rejected', 'forbidden'].includes(target.phase))) return
    update(null); saveHandle(null, scope)
  }, [scope, update])

  useEffect(() => {
    if (controller.current.signal.aborted) controller.current = new AbortController()
    const active = controller.current
    queueMicrotask(() => { if (!active.signal.aborted && attemptRef.current) void refresh() })
    return () => { active.abort(); pending.current = false }
  }, [refresh])

  useEffect(() => {
    if (attempt?.operation?.status !== 'deleting') return
    const timer = window.setInterval(() => { if (!document.hidden) void refresh() }, 5000)
    return () => window.clearInterval(timer)
  }, [attempt?.operation?.status, refresh])

  return { attempt, action, submit, refresh, retry, dismiss,
    resend: () => {
      const target = attemptRef.current
      if (target?.phase === 'missing' && !target.accepted && target.request) void submit(target.handle.projectId, target.request, target.handle.runId)
    } }
}
