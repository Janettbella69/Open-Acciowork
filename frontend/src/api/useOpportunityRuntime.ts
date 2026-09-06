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
} from './types'
import { permissionConflict, permissionLifecycleState } from '../permissionLifecycle'

type ConnectionState = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'closed'

const terminalStatuses = new Set<RunStatus>(['completed', 'failed', 'cancelled'])

export function useOpportunityRuntime() {
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
  const terminalRef = useRef(false)
  const sawTextDeltaRef = useRef(false)
  const sawReasoningDeltaRef = useRef(false)
  const runRef = useRef<RunRecord | null>(null)
  const runStatusRevisionRef = useRef(0)
  const permissionInFlightRef = useRef(new Set<string>())
  const permissionRevisionRef = useRef(0)
  const [permissionBusy, setPermissionBusy] = useState(false)
  const retryInFlightRef = useRef(false)
  const selectionRef = useRef(0)
  const [historyLoading, setHistoryLoading] = useState(false)
  const activeRunId = run?.id
  const activeRunStatus = run?.status

  const closeStream = useCallback(() => {
    eventSourceRef.current?.close()
    eventSourceRef.current = null
  }, [])

  const refreshProject = useCallback(async (projectId: string) => {
    const selection = selectionRef.current
    const updated = await getProject(projectId)
    if (selectionRef.current !== selection) return
    setProject((current) => current?.id === projectId ? updated : current)
    setProjects((current) => [updated, ...current.filter((item) => item.id !== updated.id)])
  }, [])

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
    } catch {
      if (current()) {
        setPermissions((items) => items.map((item) => item.durable ? { ...item, state: 'unknown' } : item))
        setError('Could not refresh approvals. The previous view may be stale; retry before deciding.')
      }
    }
  }, [])

  const handleEvent = useCallback((event: OphiraEvent) => {
    if (event.run_id !== runRef.current?.id) return
    const status = event.data.status
    if (typeof status === 'string') {
      runStatusRevisionRef.current += 1
      setRun((current) => {
        const next = current ? { ...current, status: status as RunStatus } : current
        runRef.current = next
        return next
      })
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

    if (event.type === 'permission.requested' && typeof event.data.request_id === 'string') {
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
      event.type === 'run.completed'
      || (
        event.type === 'tool.completed'
        && typeof toolName === 'string'
        && toolName.startsWith('mcp__ophira_')
        && !replayingTerminalRun
      )
    ) {
      void refreshProject(event.project_id).catch(() => undefined)
    }

    if (['run.completed', 'run.failed', 'run.cancelled'].includes(event.type)) {
      const selection = selectionRef.current
      terminalRef.current = true
      setConnection('closed')
      setHistoryLoading(false)
      eventSourceRef.current?.close()
      void Promise.all([
        refreshProject(event.project_id),
        getRun(event.run_id).then((record) => {
          if (selectionRef.current !== selection || runRef.current?.id !== record.id) return
          runRef.current = record
          setRun(record)
        }),
      ]).catch(
        () => undefined,
      )
    }
  }, [refreshProject, refreshPermissions])

  const connectToRun = useCallback(async (nextRun: RunRecord) => {
    const selection = selectionRef.current
    closeStream()
    terminalRef.current = false
    setConnection('connecting')
    const protocol = await getProtocol()
    if (selectionRef.current !== selection || runRef.current?.id !== nextRun.id) return
    const source = new EventSource(runEventUrl(nextRun.id))
    eventSourceRef.current = source

    const listener = (message: Event) => {
      if (selectionRef.current !== selection || eventSourceRef.current !== source) return
      try {
        handleEvent(JSON.parse((message as MessageEvent<string>).data) as OphiraEvent)
      } catch {
        setError('A runtime event could not be decoded.')
      }
    }
    protocol.event_types.forEach((eventType) => source.addEventListener(eventType, listener))
    source.onopen = () => {
      if (selectionRef.current === selection && eventSourceRef.current === source) {
        setConnection('live')
      }
    }
    source.onerror = () => {
      if (selectionRef.current === selection && eventSourceRef.current === source && !terminalRef.current) {
        setConnection('reconnecting')
      }
    }
  }, [closeStream, handleEvent])

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
      setProject(nextProject)
      setProjects((current) => [nextProject, ...current])
      if (files.length) await Promise.all(files.map((file) => uploadProjectFile(nextProject.id, file)))
      if (selectionRef.current !== selection) return null
      const nextRun = await startRun(nextProject.id, brief, null, mode, providerProfileId, agentId)
      if (selectionRef.current !== selection) return null
      runRef.current = nextRun
      setRun(nextRun)
      await connectToRun(nextRun)
      return nextRun
    } catch (startError) {
      if (selectionRef.current !== selection) return null
      setConnection('closed')
      setError(errorMessage(startError))
      throw startError
    } finally {
      if (selectionRef.current === selection) setStarting(false)
    }
  }, [closeStream, connectToRun])

  const continueProject = useCallback(async (
    brief: string,
    files: File[] = [],
    mode: RunMode = 'standard',
    agentId?: string | null,
    providerProfileId?: string | null,
  ) => {
    if (!project) return start(brief, files, mode, agentId, providerProfileId)

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
      return nextRun
    } catch (continueError) {
      if (selectionRef.current !== selection) return null
      setConnection('closed')
      setError(errorMessage(continueError))
      throw continueError
    } finally {
      if (selectionRef.current === selection) setStarting(false)
    }
  }, [closeStream, connectToRun, project, run?.sdk_session_id, start])

  const cancel = useCallback(async () => {
    if (!run) return
    const selection = selectionRef.current
    try {
      const cancelled = await cancelRun(run.id)
      if (selectionRef.current !== selection) return
      runRef.current = cancelled
      setRun(cancelled)
    } catch (cancelError) {
      if (selectionRef.current === selection) setError(errorMessage(cancelError))
    }
  }, [run])

  const retry = useCallback(async (strategy: RunRetryStrategy): Promise<RunRecord | null> => {
    if (
      !run
      || !terminalStatuses.has(run.status)
      || run.status === 'completed'
      || retryInFlightRef.current
    ) return null

    retryInFlightRef.current = true
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
      return nextRun
    } catch (retryError) {
      if (selectionRef.current !== selection) return null
      setConnection('closed')
      setError(errorMessage(retryError))
      throw retryError
    } finally {
      retryInFlightRef.current = false
      if (selectionRef.current === selection) setStarting(false)
    }
  }, [closeStream, connectToRun, run])

  const decidePermission = useCallback(async (
    requestId: string,
    decision: 'allow_once' | 'allow_run' | 'allow_persistent' | 'deny',
    answers?: Record<string, string | string[]>,
  ) => {
    const permission = permissions.find((item) => item.id === requestId)
    if (!run || !permission || permissionLifecycleState(permission) !== 'pending' || permissionInFlightRef.current.size) return
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
  }, [run, permissions, refreshPermissions])

  const resumePermission = useCallback(async (requestId: string) => {
    const permission = permissions.find((item) => item.id === requestId)
    if (!run || !permission || permissionLifecycleState(permission) !== 'resolved' || permissionInFlightRef.current.size) return
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
      if (selectionRef.current === selection) setError(permissionConflict(cause)
        ? `The approval could not continue yet. Refresh approvals and retry when the runtime is ready. ${errorMessage(cause)}`
        : errorMessage(cause))
    } finally {
      permissionInFlightRef.current.delete(requestId)
      if (selectionRef.current === selection) { setPermissionBusy(false); await refreshPermissions() }
    }
  }, [run, permissions, refreshPermissions])

  const selectProject = useCallback(async (selected: OpportunityProject): Promise<RunRecord | null> => {
    const selection = ++selectionRef.current
    closeStream()
    setProject(selected)
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
      const projectRuns = await listProjectRuns(selected.id)
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
  }, [closeStream, connectToRun])

  const refreshActiveProject = useCallback(async () => {
    if (!project) return
    try {
      await refreshProject(project.id)
    } catch {
      // The ledger view refresh is best-effort; the drawer shows its own errors.
    }
  }, [project, refreshProject])

  const reset = useCallback(() => {
    selectionRef.current += 1
    closeStream()
    setProject(null)
    setRun(null)
    runRef.current = null
    setEvents([])
    setAssistantText('')
    setReasoningSummary('')
    setPermissions([])
    setConnection('idle')
    setError('')
    setStarting(false)
    setHistoryLoading(false)
  }, [closeStream])

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
    void getRunTelemetry(run.id)
      .then((result) => {
        if (active) setTelemetry(result)
      })
      .catch(() => {
        if (active) setTelemetry(null)
      })
    return () => {
      active = false
    }
  }, [run])

  useEffect(() => {
    let active = true
    void Promise.all([getProtocol(), listProjects()])
      .then(([, storedProjects]) => {
        if (active) setProjects(storedProjects)
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
    reset,
  }
}
