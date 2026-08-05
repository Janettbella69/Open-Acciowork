import { useState } from 'react'
import { useOpportunityRuntime } from '../api/useOpportunityRuntime'
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

  const runAnalysis = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const brief = briefInput.trim()
    if (!brief) return

    setNotice('Creating one shared project truth and starting Opportunity Lead…')
    try {
      await runtime.start(brief)
      normalizeWorkspaceUrl()
      setNotice('Backend connected. Live Agent activity is streaming into this conversation.')
    } catch {
      setNotice('The research run could not start. Your brief is still here so you can try again.')
    }
  }

  const startNewResearch = async () => {
    if (runtime.isActive) await runtime.cancel()
    runtime.reset()
    setBriefInput('')
    normalizeWorkspaceUrl()
    setNotice('Start with a product, market, capability, or customer question.')
  }

  const selectSavedProject = (projectId: string) => {
    const project = runtime.projects.find((item) => item.id === projectId)
    if (!project) return
    runtime.selectProject(project)
    setBriefInput(project.objective)
    normalizeWorkspaceUrl()
    setNotice('Saved project truth loaded from the backend.')
  }

  return (
    <WorkspaceComposer
      value={briefInput}
      busy={runtime.starting || runtime.isActive}
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
      isActive={runtime.isActive}
      onChange={setBriefInput}
      onSubmit={runAnalysis}
      onNewResearch={() => void startNewResearch()}
      onSelectProject={selectSavedProject}
      onCancel={() => void runtime.cancel()}
      onDecision={(requestId, decision) => void runtime.decidePermission(requestId, decision)}
    />
  )
}
