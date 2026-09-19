import { useCallback, useEffect, useRef, useState } from 'react'
import {
  cancelRun,
  createProject,
  errorMessage,
  getProject,
  getProtocol,
  getRun,
  getRunTelemetry,
  listProjectRuns,
  listPendingPermissions,
  listProjects,
  resolvePermission,
  resumePermissionRun,
  retryRun as requestRunRetry,
  runEventUrl,
  startRun,
  uploadProjectFile,
} from './client'
import type {
  OpportunityProject,
  OphiraEvent,
  PendingPermission,
  RunRecord,
  RunMode,
  RunRetryStrategy,
  RunStatus,
  RunTelemetry,
  RunStreamEnd,
} from './types'
import { permissionConflict, permissionLifecycleState } from '../permissionLifecycle'

type ConnectionState = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'closed'

const terminalStatuses = new Set<RunStatus>(['completed', 'failed', 'cancelled'])
const runStatusEvents = new Set([
  'run.created', 'run.started', 'run.status', 'run.completed', 'run.failed',
  'run.cancel_requested', 'run.cancelled',
])
const projectRecordEvents = new Set(['evidence.added', 'decision.recorded'])
const projectRefreshError = 'Could not refresh the saved project. Refresh before using this view.'
const streamGapError = 'The runtime event stream has a gap. Reopen this project to restore its history.'

function accessUnavailable(reason: unknown): boolean {
  return !!reason && typeof reason === 'object' && 'status' in reason
    && (reason.status === 403 || reason.status === 404)
}

type ProjectRefresh = {
  projectId: string
  selection: number
  revision: number
  controller: AbortController
  promise: Promise<void>
}

export function useOpportunityRuntime(onProjectUnavailable?: (projectId: string) => void) {
  const [projects, setProjects] = useState<OpportunityProject[]>([])
  const [project, setProject] = useState<OpportunityProject | null>(null)
  const [run, setRun] = useState<RunRecord | null>(null)
  const [telemetry, setTelemetry] = useState<RunTelemetry | null>(null)
  const [events, setEvents] = useState<OphiraEvent[]>([])
  const [assistantText, setAssistantText] = useState('')
  const [reasoningSummary, setReasoningSummary] = useState('')
  const [permissions, setPermissions] = useState<PendingPermission[]>([])
  const [connection, setConnection] = useState<ConnectionState>('idle')
  const [error, setError] = useState('')
  const [starting, setStarting] = useState(false)
  const eventSourceRef = useRef<EventSource | null>(null)
  const projectRef = useRef<OpportunityProject | null>(null)
  const projectRefreshRef = useRef<ProjectRefresh | null>(null)
  const sawTextDeltaRef = useRef(false)
  const sawReasoningDeltaRef = useRef(false)
  const runRef = useRef<RunRecord | null>(null)
  const runStatusRevisionRef = useRef(0)
  const permissionInFlightRef = useRef(new Set<string>())
  const permissionRevisionRef = useRef(0)
  const [permissionBusy, setPermissionBusy] = useState(false)
  const retryInFlightRef = useRef<object | null>(null)
  const discardedProjectsRef = useRef(new Set<string>())
  const selectionRef = useRef(0)
  const [historyLoading, setHistoryLoading] = useState(false)
  const activeRunId = run?.id
  const activeRunStatus = run?.status

  const closeStream = useCallback(() => {
    eventSourceRef.current?.close()
    eventSourceRef.current = null
    projectRefreshRef.current?.controller.abort()
    projectRefreshRef.current = null
  }, [])

  const reset = useCallback(() => {
    selectionRef.current += 1
    closeStream()
    projectRef.current = null
    runRef.current = null
    permissionRevisionRef.current += 1
    permissionInFlightRef.current.clear()
    retryInFlightRef.current = null
    sawTextDeltaRef.current = false
    sawReasoningDeltaRef.current = false
    setProject(null)
    setRun(null)
    setTelemetry(null)
    setEvents([])
    setAssistantText('')
    setReasoningSummary('')
    setPermissions([])
    setPermissionBusy(false)
    setConnection('idle')
    setError('')
    setStarting(false)
    setHistoryLoading(false)
  }, [closeStream])

  const discardProject = useCallback((projectId: string): boolean => {
    discardedProjectsRef.current.add(projectId)
    setProjects((items) => items.filter((item) => item.id !== projectId))
    if (projectRef.current?.id !== projectId) return false
    reset()
    onProjectUnavailable?.(projectId)
    return true
  }, [onProjectUnavailable, reset])

  const refreshProject = useCallback((projectId: string): Promise<void> => {
    if (projectRef.current?.id !== projectId) return Promise.resolve()
    const selection = selectionRef.current
    const pending = projectRefreshRef.current
    if (pending?.selection === selection && pending.projectId === projectId) {
      pending.revision += 1
      return pending.promise
    }
    const refresh: ProjectRefresh = {
      projectId, selection, revision: 0, controller: new AbortController(),
      promise: Promise.resolve(),
    }
    projectRefreshRef.current = refresh
    const current = () => selectionRef.current === selection
      && projectRef.current?.id === projectId && projectRefreshRef.current === refresh
      && !refresh.controller.signal.aborted
    refresh.promise = (async () => {
      // One request runs at a time. Events received during that request require
      // one fresh read before its result can become the displayed ledger view.
      while (current()) {
        const revision = refresh.revision
        try {
          const updated = await getProject(projectId, refresh.controller.signal)
          if (!current()) return
          if (revision !== refresh.revision) continue
          if (updated.id !== projectId) throw new Error('Project response identity mismatch')
          projectRef.current = updated
          setProject(updated)
          setProjects((items) => [updated, ...items.filter((item) => item.id !== updated.id)])
          setError((message) => message === projectRefreshError ? '' : message)
          return
        } catch (cause) {
          if (!current()) return
          if (accessUnavailable(cause)) { discardProject(projectId); return }
          if (revision !== refresh.revision) continue
          setError(projectRefreshError)
          throw cause
        }
      }
    })().finally(() => {
      if (projectRefreshRef.current === refresh) projectRefreshRef.current = null
    })
    return refresh.promise
  }, [discardProject])

  const checkProjectAccess = useCallback(async (reason: unknown, projectId: string) => {
    // A missing run or denied write does not prove that project reads were revoked.
    if (accessUnavailable(reason)) await refreshProject(projectId).catch(() => undefined)
  }, [refreshProject])

  const refreshPermissions = useCallback(async () => {
    const currentRun = runRef.current
    if (!currentRun || terminalStatuses.has(currentRun.status) || permissionInFlightRef.current.size) return
    const selection = selectionRef.current
    const revision = ++permissionRevisionRef.current
    const current = () => selectionRef.current === selection && runRef.current?.id === currentRun.id && revision === permissionRevisionRef.current
    try {
      const pending = await listPendingPermissions()
      if (!current()) return
      setPermissions(pending.filter((item) => item.run_id === currentRun.id).map((item) => ({
        id: item.id, toolName: item.tool_name, toolInput: item.tool_input,
        actor: { type: 'agent', name: item.agent_id ?? 'TradeMind', agent_id: item.agent_id },
        destructive: item.destructive, persistable: item.persistable, scope: item.scope,
        timeoutSeconds: item.timeout_seconds, durable: item.durable, state: item.state,
        toolUseId: item.tool_use_id, expiresAt: item.expires_at,
      })))
    } catch (cause) {
      if (!current()) return
      await checkProjectAccess(cause, currentRun.project_id)
      if (current()) {
        setPermissions((items) => items.map((item) => item.durable ? { ...item, state: 'unknown' } : item))
        setError('Could not refresh approvals. The previous view may be stale; retry before deciding.')
      }
    }
  }, [checkProjectAccess])

  const handleEvent = useCallback((event: OphiraEvent, validStatuses: Set<string>) => {
    const currentRun = runRef.current
    if (!currentRun || event.run_id !== currentRun.id || event.project_id !== currentRun.project_id) return
    const status = event.data.status
    if (runStatusEvents.has(event.type) && typeof status === 'string' && validStatuses.has(status)
      && (!terminalStatuses.has(currentRun.status) || status === currentRun.status)) {
      runStatusRevisionRef.current += 1
      const next = { ...currentRun, status: status as RunStatus }
      runRef.current = next
      setRun(next)
      if (terminalStatuses.has(next.status)) setHistoryLoading(true)
    }

    if (event.type === 'assistant.text.delta' && typeof event.data.text === 'string') {
      sawTextDeltaRef.current = true
      setAssistantText((current) => current + event.data.text)
    } else if (
      event.type === 'assistant.message.completed'
      && !sawTextDeltaRef.current
      && typeof event.data.text === 'string'
    ) {
      setAssistantText((current) => `${current}${current ? '\n\n' : ''}${event.data.text}`)
    }

    if (
      event.type === 'assistant.reasoning_summary.delta'
      && typeof event.data.text === 'string'
    ) {
      sawReasoningDeltaRef.current = true
      setReasoningSummary((current) => current + event.data.text)
    } else if (
      event.type === 'assistant.reasoning_summary.completed'
      && !sawReasoningDeltaRef.current
      && typeof event.data.text === 'string'
    ) {
      setReasoningSummary((current) => `${current}${current ? '\n\n' : ''}${event.data.text}`)
    }

    if (
      event.type === 'run.result'
      && !sawTextDeltaRef.current
      && typeof event.data.result === 'string'
    ) {
      setAssistantText(event.data.result)
    }

    if (event.type === 'permission.requested' && typeof event.data.request_id === 'string'
      && !terminalStatuses.has(currentRun.status)) {
      permissionRevisionRef.current += 1
      setPermissions((current) => [
        ...current.filter((item) => item.id !== event.data.request_id),
        {
          id: event.data.request_id as string,
          toolName: typeof event.data.tool_name === 'string' ? event.data.tool_name : 'Tool',
          toolInput: event.data.tool_input,
          actor: event.actor,
          destructive: event.data.destructive === true,
          persistable: event.data.persistable === true,
          scope: typeof event.data.scope === 'string' ? event.data.scope : '',
          durable: event.data.durable === true,
          state: typeof event.data.state === 'string' ? event.data.state : undefined,
          toolUseId: typeof event.data.tool_use_id === 'string' ? event.data.tool_use_id : null,
          expiresAt: typeof event.data.expires_at === 'string' ? event.data.expires_at : null,
          timeoutSeconds: typeof event.data.timeout_seconds === 'number'
            ? event.data.timeout_seconds
            : undefined,
        },
      ])
      if (event.data.durable === true) void refreshPermissions()
    }
    if (
      (event.type === 'permission.resolved' || event.type === 'permission.timed_out')
      && typeof event.data.request_id === 'string'
    ) {
      permissionRevisionRef.current += 1
      if (event.data.durable === true) void refreshPermissions()
      else setPermissions((current) => current.filter((item) => item.id !== event.data.request_id))
    }

    if (!event.type.endsWith('.delta')) {
      setEvents((current) => [...current, event])
    }

    const toolName = event.data.tool_name
    const replayingTerminalRun = runRef.current ? terminalStatuses.has(runRef.current.status) : false
    if (
      projectRecordEvents.has(event.type)
      || (
        event.type === 'tool.completed'
        && typeof toolName === 'string'
        && toolName.startsWith('mcp__ophira_')
        && !replayingTerminalRun
      )
    ) {
      void refreshProject(event.project_id).catch(() => undefined)
    }

  }, [refreshProject, refreshPermissions])

  const connectToRun = useCallback(async (nextRun: RunRecord) => {
    const selection = selectionRef.current
    closeStream()
    setConnection('connecting')
    const protocol = await getProtocol()
    if (selectionRef.current !== selection || runRef.current?.id !== nextRun.id) return
    if (!protocol.stream_end_event || protocol.event_types.includes(protocol.stream_end_event)) {
      throw new Error('The runtime stream completion protocol is unavailable.')
    }
    const source = new EventSource(runEventUrl(nextRun.id))
    eventSourceRef.current = source
    const validStatuses = new Set(protocol.run_statuses)
    let lastSequence = 0
    const selected = () => selectionRef.current === selection && runRef.current?.id === nextRun.id
      && runRef.current.project_id === nextRun.project_id && projectRef.current?.id === nextRun.project_id
    const current = () => selected() && eventSourceRef.current === source

    const listener = (eventType: string) => (message: Event) => {
      if (!current()) return
      try {
        const event = JSON.parse((message as MessageEvent<string>).data) as OphiraEvent
        if (!event || event.run_id !== nextRun.id || event.project_id !== nextRun.project_id) return
        if (event.type !== eventType || !Number.isSafeInteger(event.sequence) || event.sequence <= 0
          || event.id !== `${nextRun.id}:${event.sequence}`
          || !event.data || typeof event.data !== 'object' || Array.isArray(event.data)) {
          throw new Error('Invalid runtime event envelope')
        }
        if (event.sequence <= lastSequence) return
        if (event.sequence !== lastSequence + 1) {
          setError(streamGapError)
          return
        }
        handleEvent(event, validStatuses)
        lastSequence = event.sequence
      } catch {
        setError('A runtime event could not be decoded.')
      }
    }
    protocol.event_types.forEach((eventType) => source.addEventListener(eventType, listener(eventType)))
    source.addEventListener(protocol.stream_end_event, (message: Event) => {
      if (!current()) return
      try {
        const end = JSON.parse((message as MessageEvent<string>).data) as RunStreamEnd
        if (!end || end.run_id !== nextRun.id || end.project_id !== nextRun.project_id) return
        if (!Number.isSafeInteger(end.last_sequence) || end.last_sequence < 0
          || end.last_sequence !== lastSequence) {
          setError(streamGapError)
          return
        }
      } catch {
        setError('A runtime event could not be decoded.')
        return
      }
      // Execution may finish before ledger projections are appended. Only this
      // transport frame confirms that the complete durable tail was consumed.
      source.close()
      eventSourceRef.current = null
      setConnection('closed')
      setHistoryLoading(true)
      setError((message) => message === streamGapError ? '' : message)
      void Promise.allSettled([
        refreshProject(nextRun.project_id),
        getRun(nextRun.id).then((record) => {
          if (!selected()) return
          if (record.id !== nextRun.id || record.project_id !== nextRun.project_id) {
            throw new Error('Run response identity mismatch')
          }
          runStatusRevisionRef.current += 1
          runRef.current = record
          setRun(record)
        }).catch(async (cause) => {
          if (!selected()) return
          await checkProjectAccess(cause, nextRun.project_id)
          if (selected()) setError('Could not refresh the saved run. Reopen this project to retry.')
        }),
      ]).then(() => { if (selected()) setHistoryLoading(false) })
    })
    source.onopen = () => {
      if (current()) setConnection('live')
    }
    source.onerror = () => {
      if (!current()) return
      setConnection('reconnecting')
      void refreshProject(nextRun.project_id).catch(() => undefined)
    }
  }, [checkProjectAccess, closeStream, handleEvent, refreshProject])

  const start = useCallback(async (
    brief: string,
    files: File[] = [],
    mode: RunMode = 'standard',
    agentId?: string | null,
    providerProfileId?: string | null,
  ) => {
    const selection = ++selectionRef.current
    closeStream()
    setStarting(true)
    setError('')
    setEvents([])
    setAssistantText('')
    setReasoningSummary('')
    setPermissions([])
    setHistoryLoading(false)
    sawTextDeltaRef.current = false
    sawReasoningDeltaRef.current = false
    try {
      const nextProject = await createProject(brief)
      if (selectionRef.current !== selection) return null
      if (discardedProjectsRef.current.has(nextProject.id)) return null
      projectRef.current = nextProject
      setProject(nextProject)
      setProjects((current) => [nextProject, ...current])
      if (files.length) await Promise.all(files.map((file) => uploadProjectFile(nextProject.id, file)))
      if (selectionRef.current !== selection) return null
      const nextRun = await startRun(nextProject.id, brief, null, mode, providerProfileId, agentId)
      if (selectionRef.current !== selection) return null
      runRef.current = nextRun
      setRun(nextRun)
      await connectToRun(nextRun)
      return selectionRef.current === selection ? nextRun : null
    } catch (startError) {
      if (selectionRef.current !== selection) return null
      if (projectRef.current) await checkProjectAccess(startError, projectRef.current.id)
      if (selectionRef.current !== selection) return null
      setConnection('closed')
      setError(errorMessage(startError))
      throw startError
    } finally {
      if (selectionRef.current === selection) setStarting(false)
    }
  }, [checkProjectAccess, closeStream, connectToRun])

  const continueProject = useCallback(async (
    brief: string,
    files: File[] = [],
    mode: RunMode = 'standard',
    agentId?: string | null,
    providerProfileId?: string | null,
  ) => {
    if (!project) return start(brief, files, mode, agentId, providerProfileId)
    if (projectRef.current?.id !== project.id || discardedProjectsRef.current.has(project.id)) return null

    const selection = ++selectionRef.current
    closeStream()
    setStarting(true)
    setError('')
    setEvents([])
    setAssistantText('')
    setReasoningSummary('')
    setPermissions([])
    setHistoryLoading(false)
    sawTextDeltaRef.current = false
    sawReasoningDeltaRef.current = false
    try {
      if (files.length) await Promise.all(files.map((file) => uploadProjectFile(project.id, file)))
      if (selectionRef.current !== selection) return null
      const nextRun = await startRun(project.id, brief, run?.sdk_session_id, mode, providerProfileId, agentId)
      if (selectionRef.current !== selection) return null
      runRef.current = nextRun
      setRun(nextRun)
      await connectToRun(nextRun)
      return selectionRef.current === selection ? nextRun : null
    } catch (continueError) {
      if (selectionRef.current !== selection) return null
      await checkProjectAccess(continueError, project.id)
      if (selectionRef.current !== selection) return null
      setConnection('closed')
      setError(errorMessage(continueError))
      throw continueError
    } finally {
      if (selectionRef.current === selection) setStarting(false)
    }
  }, [checkProjectAccess, closeStream, connectToRun, project, run?.sdk_session_id, start])

  const cancel = useCallback(async () => {
    if (!run || runRef.current?.id !== run.id || projectRef.current?.id !== run.project_id) return
    const selection = selectionRef.current
    try {
      const cancelled = await cancelRun(run.id)
      if (selectionRef.current !== selection) return
      runRef.current = cancelled
      setRun(cancelled)
    } catch (cancelError) {
      if (selectionRef.current !== selection) return
      await checkProjectAccess(cancelError, run.project_id)
      if (selectionRef.current === selection) setError(errorMessage(cancelError))
    }
  }, [checkProjectAccess, run])

  const retry = useCallback(async (strategy: RunRetryStrategy): Promise<RunRecord | null> => {
    if (
      !run
      || runRef.current?.id !== run.id
      || projectRef.current?.id !== run.project_id
      || !terminalStatuses.has(run.status)
      || run.status === 'completed'
      || retryInFlightRef.current
    ) return null

    const operation = {}
    retryInFlightRef.current = operation
    const sourceRunId = run.id
    const selection = ++selectionRef.current
    setStarting(true)
    setError('')
    try {
      const nextRun = await requestRunRetry(sourceRunId, strategy)
      if (selectionRef.current !== selection) return null
      closeStream()
      setEvents([])
      setAssistantText('')
      setReasoningSummary('')
      setPermissions([])
      setHistoryLoading(false)
      sawTextDeltaRef.current = false
      sawReasoningDeltaRef.current = false
      runRef.current = nextRun
      setRun(nextRun)
      await connectToRun(nextRun)
      return selectionRef.current === selection ? nextRun : null
    } catch (retryError) {
      if (selectionRef.current !== selection) return null
      await checkProjectAccess(retryError, run.project_id)
      if (selectionRef.current !== selection) return null
      setConnection('closed')
      setError(errorMessage(retryError))
      throw retryError
    } finally {
      if (retryInFlightRef.current === operation) retryInFlightRef.current = null
      if (selectionRef.current === selection) setStarting(false)
    }
  }, [checkProjectAccess, closeStream, connectToRun, run])

  const decidePermission = useCallback(async (
    requestId: string,
    decision: 'allow_once' | 'allow_run' | 'allow_persistent' | 'deny',
    answers?: Record<string, string | string[]>,
  ) => {
    const permission = permissions.find((item) => item.id === requestId)
    if (!run || runRef.current?.id !== run.id || projectRef.current?.id !== run.project_id
      || !permission || permissionLifecycleState(permission) !== 'pending' || permissionInFlightRef.current.size) return
    const selection = selectionRef.current
    permissionInFlightRef.current.add(requestId)
    permissionRevisionRef.current += 1
    setPermissionBusy(true)
    setError('')
    try {
      await resolvePermission(run.id, requestId, decision, answers)
      if (selectionRef.current !== selection) return
      setPermissions((current) => current.filter((item) => item.id !== requestId))
    } catch (permissionError) {
      if (selectionRef.current !== selection) return
      await checkProjectAccess(permissionError, run.project_id)
      if (selectionRef.current === selection) setError(permissionConflict(permissionError)
        ? `The approval could not continue yet. Refresh approvals and retry when the runtime is ready. ${errorMessage(permissionError)}`
        : errorMessage(permissionError))
    } finally {
      permissionInFlightRef.current.delete(requestId)
      if (selectionRef.current === selection) {
        setPermissionBusy(false)
        if (permission.durable) await refreshPermissions()
      }
    }
  }, [checkProjectAccess, run, permissions, refreshPermissions])

  const resumePermission = useCallback(async (requestId: string) => {
    const permission = permissions.find((item) => item.id === requestId)
    if (!run || runRef.current?.id !== run.id || projectRef.current?.id !== run.project_id
      || !permission || permissionLifecycleState(permission) !== 'resolved' || permissionInFlightRef.current.size) return
    const selection = selectionRef.current
    const statusRevision = runStatusRevisionRef.current
    permissionInFlightRef.current.add(requestId)
    permissionRevisionRef.current += 1
    setPermissionBusy(true)
    setError('')
    try {
      const next = await resumePermissionRun(run.id)
      if (selectionRef.current !== selection) return
      // The same stream can reach running, another approval, or completion
      // before this queued acknowledgement arrives over HTTP.
      if (runStatusRevisionRef.current === statusRevision) {
        runRef.current = next
        setRun(next)
      }
      setPermissions((items) => items.filter((item) => item.id !== requestId))
      // The existing event stream stays on this same run; do not replay the
      // entire SDK transcript or create a second run for a saved decision.
    } catch (cause) {
      if (selectionRef.current !== selection) return
      await checkProjectAccess(cause, run.project_id)
      if (selectionRef.current === selection) setError(permissionConflict(cause)
        ? `The approval could not continue yet. Refresh approvals and retry when the runtime is ready. ${errorMessage(cause)}`
        : errorMessage(cause))
    } finally {
      permissionInFlightRef.current.delete(requestId)
      if (selectionRef.current === selection) { setPermissionBusy(false); await refreshPermissions() }
    }
  }, [checkProjectAccess, run, permissions, refreshPermissions])

  const selectProject = useCallback(async (selected: OpportunityProject): Promise<RunRecord | null> => {
    if (discardedProjectsRef.current.has(selected.id)) return null
    const selection = ++selectionRef.current
    closeStream()
    projectRef.current = selected
    setProject(selected)
    setProjects((current) => [selected, ...current.filter((item) => item.id !== selected.id)])
    setRun(null)
    runRef.current = null
    setEvents([])
    setAssistantText('')
    setReasoningSummary('')
    setPermissions([])
    setConnection('idle')
    setError('')
    setStarting(false)
    setHistoryLoading(true)
    sawTextDeltaRef.current = false
    sawReasoningDeltaRef.current = false
    try {
      const projectRuns = await listProjectRuns(selected.id).catch((cause) => {
        if (selectionRef.current === selection && accessUnavailable(cause)) discardProject(selected.id)
        throw cause
      })
      if (selectionRef.current !== selection) return null
      const latestRun = projectRuns[0] ?? null
      if (!latestRun) {
        setHistoryLoading(false)
        return null
      }
      runRef.current = latestRun
      setRun(latestRun)
      await connectToRun(latestRun)
      if (selectionRef.current !== selection) {
        return null
      }
      if (terminalStatuses.has(latestRun.status)) return latestRun
      setHistoryLoading(false)
      return latestRun
    } catch (selectError) {
      if (selectionRef.current === selection) {
        setHistoryLoading(false)
        setError(errorMessage(selectError))
        throw selectError
      }
      return null
    }
  }, [closeStream, connectToRun, discardProject])

  const refreshActiveProject = useCallback(async () => {
    if (!project) return
    try {
      await refreshProject(project.id)
    } catch {
      // The ledger view refresh is best-effort; the drawer shows its own errors.
    }
  }, [project, refreshProject])

  useEffect(() => {
    setPermissionBusy(false)
    if (!activeRunId || !activeRunStatus || terminalStatuses.has(activeRunStatus)) { setPermissions([]); return }
    void refreshPermissions()
    const interval = window.setInterval(() => void refreshPermissions(), 5_000)
    const focus = () => { void refreshPermissions() }
    window.addEventListener('focus', focus)
    return () => { permissionRevisionRef.current += 1; window.clearInterval(interval); window.removeEventListener('focus', focus) }
  }, [activeRunId, activeRunStatus, refreshPermissions])

  useEffect(() => {
    if (!run || !terminalStatuses.has(run.status)) {
      setTelemetry(null)
      return
    }
    let active = true
    const selection = selectionRef.current
    const current = () => active && selectionRef.current === selection && runRef.current?.id === run.id
    void getRunTelemetry(run.id)
      .then((result) => {
        if (current()) setTelemetry(result)
      })
      .catch(async (cause) => {
        if (!current()) return
        await checkProjectAccess(cause, run.project_id)
        if (current()) setTelemetry(null)
      })
    return () => {
      active = false
    }
  }, [checkProjectAccess, run])

  useEffect(() => {
    const check = () => {
      if (projectRef.current) void refreshProject(projectRef.current.id).catch(() => undefined)
    }
    window.addEventListener('focus', check)
    return () => window.removeEventListener('focus', check)
  }, [refreshProject])

  useEffect(() => {
    let active = true
    void Promise.all([getProtocol(), listProjects()])
      .then(([, storedProjects]) => {
        if (active) setProjects((current) => [...current, ...storedProjects.filter((item) =>
          !discardedProjectsRef.current.has(item.id) && !current.some((known) => known.id === item.id))])
      })
      .catch((loadError) => {
        if (active) setError(`Backend unavailable: ${errorMessage(loadError)}`)
      })
    return () => {
      active = false
      selectionRef.current += 1
      closeStream()
    }
  }, [closeStream])

  return {
    projects,
    project,
    run,
    telemetry,
    events,
    assistantText,
    reasoningSummary,
    permissions,
    permissionBusy,
    connection,
    error,
    starting,
    historyLoading,
    isActive: run ? !terminalStatuses.has(run.status) : false,
    start,
    continueProject,
    retry,
    cancel,
    decidePermission,
    resumePermission,
    refreshPermissions,
    selectProject,
    refreshActiveProject,
    discardProject,
    reset,
  }
}
