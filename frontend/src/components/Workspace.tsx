import { useCallback, useEffect, useRef, useState } from 'react'
import { errorMessage, getOrgPolicy, listProviders, saveOrgPolicy } from '../api/client'
import type { OpportunityProject, PermissionModePolicy, ProviderProfile, RunInterjectionMode, RunMode, RunRetryStrategy } from '../api/types'
import { useOpportunityRuntime } from '../api/useOpportunityRuntime'
import { useProjectDeletion } from '../api/useProjectDeletion'
import { requireConversationScope, type ConversationDeletionOperation, type ProjectDeletionOperation } from '../api/projectDeletion'
import { providerIsRunnable } from '../providerEligibility'
import WorkspaceComposer from './WorkspaceComposer'
import ProjectDeletionProgress from './ProjectDeletionProgress'

function importedBrief(): string {
  return new URLSearchParams(window.location.search).get('brief') ?? ''
}

function importedAgentId(): string | null {
  return new URLSearchParams(window.location.search).get('agent')
}

function importedRunLink(): { projectId: string; runId: string } | null {
  const params = new URLSearchParams(window.location.search)
  const projectId = params.get('project')
  const runId = params.get('run')
  return projectId && runId ? { projectId, runId } : null
}

function normalizeWorkspaceUrl(): void {
  const canonicalUrl = new URL(window.location.href)
  canonicalUrl.searchParams.delete('brief')
  canonicalUrl.searchParams.delete('project')
  canonicalUrl.searchParams.delete('run')
  window.history.replaceState({}, '', `${canonicalUrl.pathname}${canonicalUrl.search}${canonicalUrl.hash}`)
}

function recordWorkspaceRunUrl(projectId: string, runId: string | null): void {
  const url = new URL(window.location.href)
  url.searchParams.set('project', projectId)
  if (runId) url.searchParams.set('run', runId)
  else url.searchParams.delete('run')
  window.history.replaceState({}, '', `${url.pathname}${url.search}${url.hash}`)
}

export default function Workspace() {
  const agentId = importedAgentId()
  const [briefInput, setBriefInput] = useState(importedBrief)
  const [notice, setNotice] = useState(importedBrief()
    ? 'Brief imported from the website.'
    : agentId ? 'Agent selected. Start a conversation to use it.' : '')
  const [attachedFiles, setAttachedFiles] = useState<File[]>([])
  const [providers, setProviders] = useState<ProviderProfile[]>([])
  const [providerProfileId, setProviderProfileId] = useState<string | null>(null)
  const [approvalMode, setApprovalMode] = useState<PermissionModePolicy | null>(null)
  const [approvalModeError, setApprovalModeError] = useState('')
  const [approvalModeSaving, setApprovalModeSaving] = useState(false)
  const approvalSaveRef = useRef<Promise<void> | null>(null)
  const [runMode, setRunMode] = useState<RunMode>('standard')
  const [planActionBusy, setPlanActionBusy] = useState(false)
  const planActionRef = useRef(false)
  const [interjectionBusy, setInterjectionBusy] = useState(false)
  const [compacting, setCompacting] = useState(false)
  const compactRef = useRef(false)
  const interjectionRequestRef = useRef<{
    id: string; prompt: string; mode: RunInterjectionMode
  } | null>(null)
  const [retrying, setRetrying] = useState<RunRetryStrategy | null>(null)
  const [retryFailure, setRetryFailure] = useState<{ runId: string; message: string } | null>(null)
  const retryInFlightRef = useRef<object | null>(null)
  const navigationRef = useRef(0)
  const importedRunRef = useRef(importedRunLink())
  const onProjectUnavailable = useCallback(() => {
    navigationRef.current += 1
    retryInFlightRef.current = null
    setRetrying(null)
    setRetryFailure(null)
    setNotice('This project is no longer available. Your unsent message and local attachments are still here.')
  }, [])
  const runtime = useOpportunityRuntime(onProjectUnavailable)
  const { discardProject, discardConversation, refreshActiveProject, selectProject, project: currentProject } = runtime
  const onDeletionAccepted = useCallback((operation: ProjectDeletionOperation) => {
    if (discardProject(operation.project_id)) {
      setNotice('Deletion was accepted. Your unsent message and local attachments are still here.')
    }
  }, [discardProject])
  const onDeletionAccessCheck = useCallback((projectId: string) => {
    if (currentProject?.id === projectId) void refreshActiveProject()
  }, [currentProject?.id, refreshActiveProject])
  const deletion = useProjectDeletion(onDeletionAccepted, onDeletionAccessCheck)
  const onConversationDeleted = useCallback((operation: ProjectDeletionOperation) => {
    const conversation = operation as ConversationDeletionOperation
    requireConversationScope(conversation.run_id, conversation)
    if (!discardConversation(conversation.project_id, conversation.run_ids, conversation.status !== 'deleted')) return
    navigationRef.current += 1
    retryInFlightRef.current = null
    setRetrying(null)
    setRetryFailure(null)
    setNotice('Conversation deletion was accepted. Your unsent message and local attachments are still here.')
  }, [discardConversation])
  const conversationDeletion = useProjectDeletion(onConversationDeleted, onDeletionAccessCheck, 'conversation')

  useEffect(() => {
    let active = true
    void listProviders()
      .then((items) => { if (active) setProviders(items) })
      .catch(() => { if (active) setProviders([]) })
    void getOrgPolicy()
      .then((policy) => { if (active) setApprovalMode(policy.permission_mode) })
      .catch((loadError) => { if (active) setApprovalModeError(errorMessage(loadError)) })
    return () => { active = false }
  }, [])

  const changeApprovalMode = (permissionMode: PermissionModePolicy): Promise<void> => {
    if (approvalSaveRef.current) return approvalSaveRef.current
    if (runtime.starting || runtime.isActive || runtime.historyLoading || retryInFlightRef.current) return Promise.resolve()
    setApprovalModeSaving(true)
    const saving = (async () => {
      // Settings edits the same organization policy; save onto its latest copy.
      const latest = await getOrgPolicy()
      const saved = await saveOrgPolicy({ ...latest, permission_mode: permissionMode })
      setApprovalMode(saved.permission_mode)
      setApprovalModeError('')
    })().finally(() => {
      approvalSaveRef.current = null
      setApprovalModeSaving(false)
    })
    approvalSaveRef.current = saving
    return saving
  }

  const runAnalysis = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (approvalSaveRef.current || planActionRef.current || runtime.isActive) return
    const brief = briefInput.trim()
    if (!brief) return
    const navigation = ++navigationRef.current
    if (providerProfileId) {
      const selectedProvider = providers.find((provider) => provider.id === providerProfileId)
      if (!selectedProvider || !providerIsRunnable(selectedProvider)) {
        setNotice('Choose a production-cleared Provider with a successful preflight before starting.')
        return
      }
    }

    setNotice('Starting TradeMind…')
    setRetryFailure(null)
    try {
      const started = runtime.project
        ? await runtime.continueProject(brief, attachedFiles, runMode, agentId, providerProfileId)
        : await runtime.start(brief, attachedFiles, runMode, agentId, providerProfileId)
      if (!started || navigationRef.current !== navigation) return
      normalizeWorkspaceUrl()
      setBriefInput('')
      setAttachedFiles([])
      setNotice('Backend connected. Live activity is streaming into this conversation.')
    } catch {
      if (navigationRef.current === navigation) {
        setNotice('The research run could not start. Your brief is still here so you can try again.')
      }
    }
  }

  const sendInterjection = async (mode: RunInterjectionMode) => {
    const prompt = briefInput.trim()
    if (!prompt || interjectionBusy || !runtime.run || runtime.run.status !== 'running') return
    const existing = interjectionRequestRef.current
    const request = existing?.prompt === prompt && existing.mode === mode
      ? existing : { id: crypto.randomUUID(), prompt, mode }
    interjectionRequestRef.current = request
    const navigation = navigationRef.current
    const runId = runtime.run.id
    setInterjectionBusy(true)
    setNotice('Sending your direction…')
    try {
      const receipt = await runtime.interject(prompt, mode, request.id)
      if (navigationRef.current !== navigation || runtime.run?.id !== runId) return
      interjectionRequestRef.current = null
      setBriefInput('')
      setNotice(receipt.status === 'injected'
        ? 'Direction sent to the active run.'
        : receipt.status === 'uncertain'
          ? 'Direction delivery could not be confirmed. Review the activity before retrying.'
        : receipt.status === 'blocked'
          ? 'This direction was blocked because device access changed.'
        : receipt.status === 'dispatching'
          ? 'Interrupt requested. Review the activity to confirm delivery.'
          : 'Direction saved for the next turn.')
    } catch {
      if (navigationRef.current === navigation) {
        setNotice('The direction could not be confirmed. Your message is still here; retry it.')
      }
    } finally {
      setInterjectionBusy(false)
    }
  }

  const compactContext = async () => {
    if (compactRef.current || runtime.starting || runtime.isActive
      || runtime.run?.status !== 'completed' || !runtime.run.sdk_session_id) return
    compactRef.current = true
    setCompacting(true)
    setNotice('Compacting conversation context…')
    try {
      const next = await runtime.compact()
      if (next) setNotice('Context compaction started in the same session.')
    } catch (cause) {
      setNotice(`Context compaction could not start: ${errorMessage(cause)}`)
    } finally {
      compactRef.current = false
      setCompacting(false)
    }
  }

  const continuePlan = async (changes?: string) => {
    const source = runtime.run
    if (planActionRef.current || approvalSaveRef.current || runtime.starting || runtime.isActive
      || runtime.historyLoading || source?.mode !== 'plan' || source.status !== 'completed'
      || !source.sdk_session_id || source.project_id !== runtime.project?.id) return
    if (changes !== undefined && !changes.trim()) return
    planActionRef.current = true
    setPlanActionBusy(true)
    const navigation = navigationRef.current
    try {
      const prompt = changes === undefined
        ? 'I approve the plan you just presented. Continue in this session and execute it. Ask for separate approval before any external action that requires it.'
        : `Revise the plan you just presented using my changes below. Do not execute it yet.\n\n${changes.trim()}`
      const next = await runtime.continueProject(
        prompt, [], changes === undefined ? 'standard' : 'plan',
        source.agent_id, source.provider_profile_id, source.id,
      )
      if (next && navigationRef.current === navigation) {
        setRunMode(changes === undefined ? 'standard' : 'plan')
        setNotice(changes === undefined ? 'Approved plan is running in the same session.' : 'Revising the plan in the same session.')
      }
    } catch (planError) {
      if (navigationRef.current === navigation) setNotice(errorMessage(planError))
    } finally {
      planActionRef.current = false
      setPlanActionBusy(false)
    }
  }

  const retryRun = async (strategy: RunRetryStrategy) => {
    const source = runtime.run
    if (!source || retryInFlightRef.current || approvalSaveRef.current) return
    const operation = {}
    const navigation = navigationRef.current
    retryInFlightRef.current = operation
    setRetrying(strategy)
    setRetryFailure(null)
    try {
      const retried = await runtime.retry(strategy)
      if (retried && navigationRef.current === navigation) {
        setNotice(strategy === 'resume'
          ? 'Resuming the recorded SDK session…'
          : 'Starting a new branch from the persisted run configuration…')
      }
    } catch (retryError) {
      if (navigationRef.current === navigation) setRetryFailure({ runId: source.id, message: errorMessage(retryError) })
    } finally {
      if (retryInFlightRef.current === operation) {
        retryInFlightRef.current = null
        setRetrying(null)
      }
    }
  }

  const startNewResearch = () => {
    navigationRef.current += 1
    const keepRunning = runtime.isActive
    runtime.reset()
    setRetryFailure(null)
    setBriefInput('')
    setAttachedFiles([])
    normalizeWorkspaceUrl()
    setNotice(keepRunning
      ? 'The previous task continues in the background. Start another independent task.'
      : 'Start with whatever you want to accomplish.')
  }

  const selectProjectRecord = useCallback(async (project: OpportunityProject, runId?: string) => {
    const navigation = ++navigationRef.current
    setBriefInput('')
    setAttachedFiles([])
    setRetryFailure(null)
    normalizeWorkspaceUrl()
    setNotice('Loading the saved project truth and run history…')
    try {
      const loadedRun = await selectProject(project, runId)
      if (navigationRef.current !== navigation) return
      setProviderProfileId(loadedRun?.provider_profile_id ?? null)
      recordWorkspaceRunUrl(project.id, loadedRun?.id ?? null)
      setNotice(loadedRun
        ? ''
        : 'This project has no recorded run yet. Send a message to begin.')
    } catch {
      if (navigationRef.current === navigation) {
        setNotice('The recorded run history could not be loaded. The project truth below is still current.')
      }
    }
  }, [selectProject])

  useEffect(() => {
    const link = importedRunRef.current
    if (!link) return
    const project = runtime.projects.find((item) => item.id === link.projectId)
    if (!project) return
    importedRunRef.current = null
    void selectProjectRecord(project, link.runId)
  }, [runtime.projects, selectProjectRecord])

  const selectSavedProject = (projectId: string) => {
    const project = runtime.projects.find((item) => item.id === projectId)
    if (project) void selectProjectRecord(project)
  }

  return (
    <WorkspaceComposer
      value={briefInput}
      attachedFiles={attachedFiles}
      busy={runtime.starting || runtime.isActive || runtime.historyLoading || planActionBusy}
      notice={notice}
      error={runtime.error}
      projects={runtime.projects}
      project={runtime.project}
      run={runtime.run}
      telemetry={runtime.telemetry}
      events={runtime.events}
      assistantText={runtime.assistantText}
      reasoningSummary={runtime.reasoningSummary}
      permissions={runtime.permissions}
      connection={runtime.connection}
      historyLoading={runtime.historyLoading}
      isActive={runtime.isActive}
      providers={providers}
      providerProfileId={providerProfileId}
      approvalMode={approvalMode}
      approvalModeError={approvalModeError}
      approvalModeSaving={approvalModeSaving}
      runMode={runMode}
      onChange={setBriefInput}
      onAttachedFilesChange={setAttachedFiles}
      onProviderProfileChange={setProviderProfileId}
      onApprovalModeChange={changeApprovalMode}
      onRunModeChange={setRunMode}
      onApprovePlan={() => void continuePlan()}
      onRevisePlan={(changes) => void continuePlan(changes)}
      onSubmit={runAnalysis}
      onInterject={(mode) => void sendInterjection(mode)}
      interjectionBusy={interjectionBusy}
      compacting={compacting}
      onCompact={() => void compactContext()}
      onProjectRefresh={() => void runtime.refreshActiveProject()}
      onProjectCreated={(project) => void selectProjectRecord(project)}
      deletionProgress={<><ProjectDeletionProgress deletion={deletion} /><ProjectDeletionProgress deletion={conversationDeletion} /></>}
      deletionPending={!!deletion.attempt}
      deletionCanReviewAgain={!!deletion.attempt && !deletion.action && !deletion.attempt.accepted
        && deletion.attempt.handle.projectId === runtime.project?.id
        && ['missing', 'rejected', 'forbidden'].includes(deletion.attempt.phase)}
      onDeletionReviewAgain={deletion.dismiss}
      onDeleteProject={deletion.submit}
      conversationDeletionPending={!!conversationDeletion.attempt || !!deletion.attempt}
      conversationDeletionCanReviewAgain={!!conversationDeletion.attempt && !conversationDeletion.action && !conversationDeletion.attempt.accepted
        && conversationDeletion.attempt.handle.projectId === runtime.project?.id
        && ['missing', 'rejected', 'forbidden'].includes(conversationDeletion.attempt.phase)}
      onConversationDeletionReviewAgain={conversationDeletion.dismiss}
      onDeleteConversation={conversationDeletion.submit}
      onNewResearch={startNewResearch}
      onSelectProject={(projectId) => void selectSavedProject(projectId)}
      onSelectRun={(runId) => {
        if (runtime.project) void selectProjectRecord(runtime.project, runId)
      }}
      onCancel={() => void runtime.cancel()}
      onDecision={(requestId, decision, answers) =>
        void runtime.decidePermission(requestId, decision, answers)}
      permissionBusy={runtime.permissionBusy}
      onResumePermission={(requestId) => void runtime.resumePermission(requestId)}
      onRefreshPermissions={() => void runtime.refreshPermissions()}
      retrying={retrying}
      retryError={retryFailure && retryFailure.runId === runtime.run?.id
        ? retryFailure.message
        : ''}
      onRetry={(strategy) => void retryRun(strategy)}
    />
  )
}
