import { useEffect, useRef, useState } from 'react'
import { errorMessage, getOrgPolicy, listProviders, saveOrgPolicy } from '../api/client'
import type { OpportunityProject, PermissionModePolicy, ProviderProfile, RunMode, RunRetryStrategy } from '../api/types'
import { useOpportunityRuntime } from '../api/useOpportunityRuntime'
import { providerIsRunnable } from '../providerEligibility'
import WorkspaceComposer from './WorkspaceComposer'

function importedBrief(): string {
  return new URLSearchParams(window.location.search).get('brief') ?? ''
}

function importedAgentId(): string | null {
  return new URLSearchParams(window.location.search).get('agent')
}

function normalizeWorkspaceUrl(): void {
  const canonicalUrl = new URL(window.location.href)
  canonicalUrl.searchParams.delete('brief')
  window.history.replaceState({}, '', `${canonicalUrl.pathname}${canonicalUrl.search}${canonicalUrl.hash}`)
}

export default function Workspace() {
  const runtime = useOpportunityRuntime()
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
  const [retrying, setRetrying] = useState<RunRetryStrategy | null>(null)
  const [retryFailure, setRetryFailure] = useState<{ runId: string; message: string } | null>(null)
  const retryInFlightRef = useRef(false)
  const navigationRef = useRef(0)

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
    if (approvalSaveRef.current) return
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

  const retryRun = async (strategy: RunRetryStrategy) => {
    const source = runtime.run
    if (!source || retryInFlightRef.current || approvalSaveRef.current) return
    retryInFlightRef.current = true
    setRetrying(strategy)
    setRetryFailure(null)
    try {
      const retried = await runtime.retry(strategy)
      if (retried) {
        setNotice(strategy === 'resume'
          ? 'Resuming the recorded SDK session…'
          : 'Starting a new branch from the persisted run configuration…')
      }
    } catch (retryError) {
      setRetryFailure({ runId: source.id, message: errorMessage(retryError) })
    } finally {
      retryInFlightRef.current = false
      setRetrying(null)
    }
  }

  const startNewResearch = async () => {
    const navigation = ++navigationRef.current
    if (runtime.isActive) await runtime.cancel()
    if (navigationRef.current !== navigation) return
    runtime.reset()
    setRetryFailure(null)
    setBriefInput('')
    setAttachedFiles([])
    normalizeWorkspaceUrl()
    setNotice('Start with whatever you want to accomplish.')
  }

  const selectProjectRecord = async (project: OpportunityProject) => {
    const navigation = ++navigationRef.current
    setBriefInput('')
    setAttachedFiles([])
    setRetryFailure(null)
    normalizeWorkspaceUrl()
    setNotice('Loading the saved project truth and run history…')
    try {
      const loadedRun = await runtime.selectProject(project)
      if (navigationRef.current !== navigation) return
      setProviderProfileId(loadedRun?.provider_profile_id ?? null)
      setNotice(loadedRun
        ? ''
        : 'This project has no recorded run yet. Send a message to begin.')
    } catch {
      if (navigationRef.current === navigation) {
        setNotice('The recorded run history could not be loaded. The project truth below is still current.')
      }
    }
  }

  const selectSavedProject = (projectId: string) => {
    const project = runtime.projects.find((item) => item.id === projectId)
    if (project) void selectProjectRecord(project)
  }

  return (
    <WorkspaceComposer
      value={briefInput}
      attachedFiles={attachedFiles}
      busy={runtime.starting || runtime.isActive || runtime.historyLoading}
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
      onSubmit={runAnalysis}
      onProjectRefresh={() => void runtime.refreshActiveProject()}
      onProjectCreated={(project) => void selectProjectRecord(project)}
      onNewResearch={() => void startNewResearch()}
      onSelectProject={(projectId) => void selectSavedProject(projectId)}
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
