import { useState } from 'react'
import { useOpportunityRuntime } from '../api/useOpportunityRuntime'
import type { RunMode } from '../api/types'
import WorkspaceComposer from './WorkspaceComposer'

function importedBrief(): string {
  return new URLSearchParams(window.location.search).get('brief') ?? ''
}

function normalizeWorkspaceUrl(): void {
  const canonicalUrl = new URL(window.location.href)
  canonicalUrl.searchParams.delete('brief')
  window.history.replaceState({}, '', `${canonicalUrl.pathname}${canonicalUrl.search}${canonicalUrl.hash}`)
}

export default function Workspace() {
  const runtime = useOpportunityRuntime()
  const [briefInput, setBriefInput] = useState(importedBrief)
  const [notice, setNotice] = useState(importedBrief() ? 'Brief imported from the website.' : '')
  const [attachedFiles, setAttachedFiles] = useState<File[]>([])
  const [runMode, setRunMode] = useState<RunMode>('standard')

  const runAnalysis = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const brief = briefInput.trim()
    if (!brief) return

    setNotice('Starting Ophira…')
    try {
      if (runtime.project) {
        await runtime.continueProject(brief, attachedFiles, runMode)
      } else {
        await runtime.start(brief, attachedFiles, runMode)
      }
      normalizeWorkspaceUrl()
      setBriefInput('')
      setAttachedFiles([])
      setNotice('Backend connected. Live activity is streaming into this conversation.')
    } catch {
      setNotice('The research run could not start. Your brief is still here so you can try again.')
    }
  }

  const startNewResearch = async () => {
    if (runtime.isActive) await runtime.cancel()
    runtime.reset()
    setBriefInput('')
    normalizeWorkspaceUrl()
    setNotice('Start with whatever you want to accomplish.')
  }

  const selectSavedProject = async (projectId: string) => {
    const project = runtime.projects.find((item) => item.id === projectId)
    if (!project) return
    setBriefInput('')
    normalizeWorkspaceUrl()
    setNotice('Loading the saved project truth and run history…')
    try {
      const loadedRun = await runtime.selectProject(project)
      setNotice(loadedRun
        ? ''
        : 'This project has no recorded run yet. Send a message to begin.')
    } catch {
      setNotice('The recorded run history could not be loaded. The project truth below is still current.')
    }
  }

  return (
    <WorkspaceComposer
      value={briefInput}
      attachedFiles={attachedFiles}
      mode={runMode}
      busy={runtime.starting || runtime.isActive || runtime.historyLoading}
      notice={notice}
      error={runtime.error}
      projects={runtime.projects}
      project={runtime.project}
      run={runtime.run}
      events={runtime.events}
      assistantText={runtime.assistantText}
      reasoningSummary={runtime.reasoningSummary}
      permissions={runtime.permissions}
      connection={runtime.connection}
      historyLoading={runtime.historyLoading}
      isActive={runtime.isActive}
      onChange={setBriefInput}
      onAttachedFilesChange={setAttachedFiles}
      onModeChange={setRunMode}
      onSubmit={runAnalysis}
      onProjectRefresh={() => void runtime.refreshActiveProject()}
      onNewResearch={() => void startNewResearch()}
      onSelectProject={(projectId) => void selectSavedProject(projectId)}
      onCancel={() => void runtime.cancel()}
      onDecision={(requestId, decision, answers) =>
        void runtime.decidePermission(requestId, decision, answers)}
    />
  )
}
