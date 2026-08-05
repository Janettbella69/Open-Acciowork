import { useCallback, useEffect, useRef, useState } from 'react'
import {
  cancelRun,
  createProject,
  errorMessage,
  getProject,
  getProtocol,
  getRun,
  listProjectRuns,
  listProjects,
  resolvePermission,
  runEventUrl,
  startRun,
} from './client'
import type {
  OpportunityProject,
  OphiraEvent,
  PendingPermission,
  RunRecord,
  RunStatus,
} from './types'

type ConnectionState = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'closed'

const terminalStatuses = new Set<RunStatus>(['completed', 'failed', 'cancelled'])

export function useOpportunityRuntime() {
  const [projects, setProjects] = useState<OpportunityProject[]>([])
  const [project, setProject] = useState<OpportunityProject | null>(null)
  const [run, setRun] = useState<RunRecord | null>(null)
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
  const selectionRef = useRef(0)
  const [historyLoading, setHistoryLoading] = useState(false)

  const closeStream = useCallback(() => {
    eventSourceRef.current?.close()
    eventSourceRef.current = null
  }, [])

  const refreshProject = useCallback(async (projectId: string) => {
    const updated = await getProject(projectId)
    setProject(updated)
    setProjects((current) => [updated, ...current.filter((item) => item.id !== updated.id)])
  }, [])

  const handleEvent = useCallback((event: OphiraEvent) => {
    const status = event.data.status
    if (typeof status === 'string') {
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
      setPermissions((current) => [
        ...current,
        {
          id: event.data.request_id as string,
          toolName: typeof event.data.tool_name === 'string' ? event.data.tool_name : 'Tool',
          toolInput: event.data.tool_input,
          actor: event.actor,
        },
      ])
    }
    if (
      (event.type === 'permission.resolved' || event.type === 'permission.timed_out')
      && typeof event.data.request_id === 'string'
    ) {
      setPermissions((current) => current.filter((item) => item.id !== event.data.request_id))
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
        && toolName.startsWith('mcp__ophira__')
        && !replayingTerminalRun
      )
    ) {
      void refreshProject(event.project_id).catch(() => undefined)
    }

    if (['run.completed', 'run.failed', 'run.cancelled'].includes(event.type)) {
      terminalRef.current = true
      setConnection('closed')
      setHistoryLoading(false)
      eventSourceRef.current?.close()
      void Promise.all([
        refreshProject(event.project_id),
        getRun(event.run_id).then((record) => {
          runRef.current = record
          setRun(record)
        }),
      ]).catch(
        () => undefined,
      )
    }
  }, [refreshProject])

  const connectToRun = useCallback(async (nextRun: RunRecord) => {
    closeStream()
    terminalRef.current = false
    setConnection('connecting')
    const protocol = await getProtocol()
    const source = new EventSource(runEventUrl(nextRun.id))
    eventSourceRef.current = source

    const listener = (message: Event) => {
      try {
        handleEvent(JSON.parse((message as MessageEvent<string>).data) as OphiraEvent)
      } catch {
        setError('A runtime event could not be decoded.')
      }
    }
    protocol.event_types.forEach((eventType) => source.addEventListener(eventType, listener))
    source.onopen = () => setConnection('live')
    source.onerror = () => {
      if (!terminalRef.current) setConnection('reconnecting')
    }
  }, [closeStream, handleEvent])

  const start = useCallback(async (brief: string) => {
    selectionRef.current += 1
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
      setProject(nextProject)
      setProjects((current) => [nextProject, ...current])
      const nextRun = await startRun(nextProject.id, brief)
      runRef.current = nextRun
      setRun(nextRun)
      await connectToRun(nextRun)
      return nextRun
    } catch (startError) {
      setConnection('closed')
      setError(errorMessage(startError))
      throw startError
    } finally {
      setStarting(false)
    }
  }, [closeStream, connectToRun])

  const continueProject = useCallback(async (brief: string) => {
    if (!project) return start(brief)

    selectionRef.current += 1
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
      const nextRun = await startRun(project.id, brief, run?.sdk_session_id)
      runRef.current = nextRun
      setRun(nextRun)
      await connectToRun(nextRun)
      return nextRun
    } catch (continueError) {
      setConnection('closed')
      setError(errorMessage(continueError))
      throw continueError
    } finally {
      setStarting(false)
    }
  }, [closeStream, connectToRun, project, run?.sdk_session_id, start])

  const cancel = useCallback(async () => {
    if (!run) return
    try {
      const cancelled = await cancelRun(run.id)
      runRef.current = cancelled
      setRun(cancelled)
    } catch (cancelError) {
      setError(errorMessage(cancelError))
    }
  }, [run])

  const decidePermission = useCallback(async (
    requestId: string,
    decision: 'allow_once' | 'allow_run' | 'deny',
  ) => {
    if (!run) return
    try {
      await resolvePermission(run.id, requestId, decision)
    } catch (permissionError) {
      setError(errorMessage(permissionError))
    }
  }, [run])

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
        closeStream()
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
    setHistoryLoading(false)
  }, [closeStream])

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
      closeStream()
    }
  }, [closeStream])

  return {
    projects,
    project,
    run,
    events,
    assistantText,
    reasoningSummary,
    permissions,
    connection,
    error,
    starting,
    historyLoading,
    isActive: run ? !terminalStatuses.has(run.status) : false,
    start,
    continueProject,
    cancel,
    decidePermission,
    selectProject,
    reset,
  }
}
