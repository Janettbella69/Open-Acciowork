import { useMemo, useState } from 'react'
import { dimensions, type Dimension } from '../api/types'
import { useOpportunityRuntime } from '../api/useOpportunityRuntime'
import Brand from './Brand'
import { LiveDimensionView, LiveProjectTruth, RunActivity } from './RuntimePanels'
import WorkspaceComposer from './WorkspaceComposer'
import { opportunitySignals, type Opportunity } from '../data'

type WorkspaceView = 'project' | 'demand' | 'supply' | 'capital' | 'sales'

const workspaceViews: Array<{ id: WorkspaceView; label: string; eyebrow: string }> = [
  { id: 'project', label: 'Project truth', eyebrow: '00' },
  { id: 'demand', label: 'Demand', eyebrow: '01' },
  { id: 'supply', label: 'Supply', eyebrow: '02' },
  { id: 'capital', label: 'Capital', eyebrow: '03' },
  { id: 'sales', label: 'Sales', eyebrow: '04' },
]

const projectNames = ['Gulf hospitality', 'Retail resilience', 'Independent living']

function OpportunityDetail({ opportunity }: { opportunity: Opportunity }) {
  return (
    <section className="workspace-detail" aria-labelledby="opportunity-detail-title">
      <div className="detail-kicker"><span>{opportunity.region}</span><span>{opportunity.category}</span></div>
      <h2 id="opportunity-detail-title">{opportunity.title}</h2>
      <div className="detail-columns">
        <article><span>Demand hypothesis</span><p>{opportunity.signal}</p></article>
        <article><span>Supply hypothesis</span><p>{opportunity.supplyFit}</p></article>
        <article><span>Capital question</span><p>{opportunity.capitalQuestion}</p></article>
        <article><span>Sales test</span><p>{opportunity.salesTest}</p></article>
      </div>
      <div className="detail-route">
        <div><span>Suggested route</span><strong>{opportunity.route}</strong></div>
        <div><span>Next validation</span><p>{opportunity.nextMove}</p></div>
      </div>
      <div className="detail-tags">{opportunity.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>
    </section>
  )
}

function DemandValidation({ opportunity }: { opportunity: Opportunity }) {
  const evidenceKinds = [
    ['Observed fact', 'Connect an official or primary source that directly supports the demand claim.', 'Needed'],
    ['User statement', 'Capture the buyer or operator language without promoting it into a fact.', 'Needed'],
    ['Community signal', 'Record recurring discussion patterns and their sampling limits.', 'Needed'],
    ['Inference', opportunity.signal, 'Current'],
  ]

  return (
    <section className="workspace-module" aria-labelledby="demand-heading">
      <div className="module-heading"><div><span>Demand Agent</span><h2 id="demand-heading">What would disprove this demand?</h2></div><button type="button">Add evidence</button></div>
      <div className="hypothesis-card"><span>Active hypothesis</span><p>{opportunity.signal}</p><small>Unverified · Priority 1</small></div>
      <div className="fit-table" role="table" aria-label="Demand evidence ledger">
        {evidenceKinds.map(([kind, description, status]) => (
          <div role="row" key={kind}>
            <strong role="cell">{kind}</strong>
            <p role="cell">{description}</p>
            <span role="cell">{status}</span>
          </div>
        ))}
      </div>
      <p className="module-note">Evidence types stay distinct in the ledger. An inference does not become a validated fact by repetition.</p>
    </section>
  )
}

function SupplyFit({ opportunity }: { opportunity: Opportunity }) {
  const criteria = [
    ['Capability', opportunity.supplyFit, 'Strong fit'],
    ['Commercial terms', 'Confirm minimum order, sample terms, and lead time against the buyer brief.', 'Validate'],
    ['Market readiness', `Prepare ${opportunity.region} packaging, documentation, and service expectations.`, 'Validate'],
    ['Partner model', opportunity.route, 'Route fit'],
  ]

  return (
    <section className="workspace-module" aria-labelledby="supply-fit-heading">
      <div className="module-heading"><div><span>Supply fit</span><h2 id="supply-fit-heading">Can the capability travel?</h2></div><button type="button">Add supplier</button></div>
      <div className="fit-table" role="table" aria-label="Illustrative supply fit criteria">
        {criteria.map(([label, description, status]) => (
          <div role="row" key={label}>
            <strong role="cell">{label}</strong>
            <p role="cell">{description}</p>
            <span role="cell">{status}</span>
          </div>
        ))}
      </div>
      <p className="module-note">Supplier records are intentionally empty in this first frontend. Connect a live supplier source before using this for a real decision.</p>
    </section>
  )
}

function CapitalModel({ opportunity }: { opportunity: Opportunity }) {
  const capitalFrames = [
    ['First-order investment', 'Samples, compliance, inventory, packaging, logistics, and launch activity.', 'Not calculated'],
    ['Cash-cycle peak', 'The highest cash exposure before customer payment returns to the project.', 'Not calculated'],
    ['Maximum loss', opportunity.capitalQuestion, 'Weakest link'],
  ]

  return (
    <section className="workspace-module" aria-labelledby="capital-heading">
      <div className="module-heading"><div><span>Capital Agent</span><h2 id="capital-heading">Make the downside visible before it becomes real.</h2></div><button type="button">Add cost input</button></div>
      <div className="route-list">
        {capitalFrames.map(([title, description, state], index) => (
          <article key={title} data-active={index === 2}>
            <span>{String(index + 1).padStart(2, '0')}</span>
            <div><h3>{title}</h3><p>{description}</p></div>
            <small>{state}</small>
          </article>
        ))}
      </div>
      <div className="route-context"><span>Capital gap</span><strong>{opportunity.title}</strong><p>No invented precision: currency, quantity, payment terms, and sensitivity inputs are required before a score can be calculated.</p></div>
    </section>
  )
}

function SalesPlan({ opportunity }: { opportunity: Opportunity }) {
  return (
    <section className="workspace-module" aria-labelledby="sales-heading">
      <div className="module-heading"><div><span>Sales Agent</span><h2 id="sales-heading">Design the smallest test that can earn the next step.</h2></div><button type="button">Copy sales test</button></div>
      <div className="outreach-layout">
        <article>
          <span>Falsifiable sales test</span>
          <p>{opportunity.salesTest}</p>
        </article>
        <article>
          <span>Validation questions</span>
          <ol>
            <li>Which buyer constraint most often blocks a purchase?</li>
            <li>Would {opportunity.route.toLowerCase()} make the offer easier or harder to buy?</li>
            <li>What result would tell the team to stop?</li>
          </ol>
        </article>
      </div>
      <p className="module-note">This draft is illustrative. Confirm the target customer, channel, fulfillment path, permission, and stop condition before real outreach.</p>
    </section>
  )
}

export default function Workspace() {
  const runtime = useOpportunityRuntime()
  const initialBrief = new URLSearchParams(window.location.search).get('brief') ?? ''
  const [activeView, setActiveView] = useState<WorkspaceView>('project')
  const [selectedId, setSelectedId] = useState(opportunitySignals[0].id)
  const [briefInput, setBriefInput] = useState(initialBrief)
  const [currentBrief, setCurrentBrief] = useState(initialBrief || 'Explore a global product opportunity')
  const [notice, setNotice] = useState(initialBrief ? 'Brief imported from the website.' : '')
  const [railOpen, setRailOpen] = useState(false)

  const selectedOpportunity = useMemo(
    () => opportunitySignals.find((opportunity) => opportunity.id === selectedId) ?? opportunitySignals[0],
    [selectedId],
  )

  const latestDecision = runtime.project?.stage_decisions.at(-1)
  const weakestDimension: Dimension = latestDecision?.weakest_dimension ?? dimensions.reduce(
    (weakest, dimension) => {
      if (!runtime.project) return weakest
      return runtime.project.assessments[dimension].score < runtime.project.assessments[weakest].score
        ? dimension
        : weakest
    },
    'demand' as Dimension,
  )
  const weakestLabel = weakestDimension[0].toUpperCase() + weakestDimension.slice(1)

  const runAnalysis = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const nextBrief = briefInput.trim()
    if (!nextBrief) return
    setCurrentBrief(nextBrief)
    setNotice('Creating the shared project ledger and starting Opportunity Lead…')
    setActiveView('project')
    try {
      await runtime.start(nextBrief)
      setNotice('Live analysis started. The observable SDK trace is streaming below.')
    } catch {
      setNotice('The live analysis could not start. Check the backend connection and credentials.')
    }
  }

  const startNewAnalysis = async () => {
    if (runtime.isActive) await runtime.cancel()
    runtime.reset()
    setBriefInput('')
    setCurrentBrief('New market opportunity')
    setNotice('Start with a product, market, capability, or customer question.')
    setActiveView('project')
    setRailOpen(false)
  }

  const selectSavedProject = (projectId: string) => {
    const project = runtime.projects.find((item) => item.id === projectId)
    if (!project) return
    runtime.selectProject(project)
    setCurrentBrief(project.objective)
    setBriefInput(project.objective)
    setActiveView('project')
  }

  if (!runtime.project && !runtime.run) {
    return (
      <WorkspaceComposer
        value={briefInput}
        busy={runtime.starting || runtime.isActive}
        notice={notice}
        error={notice.includes('could not start') ? runtime.error : ''}
        projects={runtime.projects}
        onChange={setBriefInput}
        onSubmit={runAnalysis}
        onSelectProject={selectSavedProject}
      />
    )
  }

  return (
    <div className="ophira-workspace">
      <aside className="workspace-rail" data-open={railOpen}>
        <a className="workspace-brand-link" href="/" aria-label="Back to OPHIRA website"><Brand compact /></a>
        <button className="new-analysis" type="button" onClick={() => void startNewAnalysis()}><span>＋</span>New analysis</button>

        <nav aria-label="Workspace views">
          <p>Commerce system</p>
          {workspaceViews.map((view) => (
            <button key={view.id} type="button" aria-label={view.label} data-active={activeView === view.id} onClick={() => { setActiveView(view.id); setRailOpen(false) }}>
              <span>{view.eyebrow}</span>{view.label}
            </button>
          ))}
        </nav>

        <div className="project-list">
          <p>Recent studies</p>
          {runtime.projects.slice(0, 6).map((project) => (
            <button key={project.id} type="button" onClick={() => {
              runtime.selectProject(project)
              setCurrentBrief(project.objective)
              setActiveView('project')
              setRailOpen(false)
            }}>{project.name}</button>
          ))}
          {runtime.projects.length === 0 && <small>No live projects yet</small>}
          <p className="example-projects-label">Examples</p>
          {projectNames.map((project, index) => <button key={project} type="button" onClick={() => {
            runtime.reset()
            setSelectedId(opportunitySignals[index].id)
            setCurrentBrief(opportunitySignals[index].title)
            setActiveView('project')
            setRailOpen(false)
          }}>{project}</button>)}
        </div>

        <div className="workspace-profile"><span>JB</span><div><strong>Ophira workspace</strong><small>{runtime.error ? 'Backend offline' : 'Backend connected'}</small></div></div>
      </aside>

      <main className="workspace-main">
        <header className="workspace-topbar">
          <button className="rail-toggle" type="button" aria-label="Open workspace navigation" onClick={() => setRailOpen((open) => !open)}><span /><span /></button>
          <div><span>Opportunity studio</span><strong>{currentBrief}</strong></div>
          <div className="topbar-actions"><span>{runtime.project ? 'Live project' : 'Illustrative data'}</span><a href="/">Website ↗</a></div>
        </header>

        <div className="workspace-content">
          <section className="research-header" aria-labelledby="workspace-title">
            <div><p>Global opportunity research</p><h1 id="workspace-title">Turn a market question into a commercial path.</h1></div>
            <form onSubmit={runAnalysis}>
              <label htmlFor="workspace-brief">Research brief</label>
              <div><input id="workspace-brief" value={briefInput} onChange={(event) => setBriefInput(event.target.value)} placeholder="What market, capability, or customer should we explore?" disabled={runtime.starting || runtime.isActive} /><button type="submit" disabled={runtime.starting || runtime.isActive}>{runtime.starting ? 'Starting…' : runtime.isActive ? 'Running…' : 'Analyze'} <span aria-hidden="true">→</span></button></div>
            </form>
            {notice && <p className="analysis-notice" role="status">{notice}</p>}
            {runtime.error && <p className="analysis-error" role="alert">{runtime.error}</p>}
          </section>

          <div className="research-body">
            <section className="research-surface">
              {activeView === 'project' && (
                <>
                  <div className="surface-heading"><div><span>Opportunity Lead</span><h2>One project truth. One weakest link.</h2></div><button type="button">Stage: {runtime.project ? runtime.project.stage.replaceAll('_', ' ') : 'Discovery'}</button></div>
                  {runtime.project ? (
                    <LiveProjectTruth project={runtime.project} onSelectDimension={(dimension) => setActiveView(dimension)} />
                  ) : (
                    <>
                      <div className="signal-list">
                        {opportunitySignals.map((opportunity, index) => (
                          <button key={opportunity.id} type="button" data-active={selectedId === opportunity.id} onClick={() => setSelectedId(opportunity.id)}>
                            <span>{String(index + 1).padStart(2, '0')}</span>
                            <div><small>{opportunity.region} · {opportunity.category}</small><strong>{opportunity.title}</strong></div>
                            <i aria-hidden="true">→</i>
                          </button>
                        ))}
                      </div>
                      <OpportunityDetail opportunity={selectedOpportunity} />
                    </>
                  )}
                </>
              )}
              {activeView !== 'project' && runtime.project && <LiveDimensionView project={runtime.project} dimension={activeView} />}
              {activeView === 'demand' && !runtime.project && <DemandValidation opportunity={selectedOpportunity} />}
              {activeView === 'supply' && !runtime.project && <SupplyFit opportunity={selectedOpportunity} />}
              {activeView === 'capital' && !runtime.project && <CapitalModel opportunity={selectedOpportunity} />}
              {activeView === 'sales' && !runtime.project && <SalesPlan opportunity={selectedOpportunity} />}
            </section>

            <aside className="workspace-inspector">
              <div className="inspector-heading"><span>Decision thread</span><small>{runtime.project ? runtime.run?.status ?? 'Saved' : 'Sample'}</small></div>
              <h2>{runtime.project?.name ?? selectedOpportunity.title}</h2>
              {runtime.project ? (
                <dl>
                  <div><dt>Stage</dt><dd>{runtime.project.stage.replaceAll('_', ' ')}</dd></div>
                  <div><dt>Revision</dt><dd>{runtime.project.revision}</dd></div>
                  <div><dt>Weakest link</dt><dd>{weakestLabel}</dd></div>
                </dl>
              ) : (
                <dl>
                  <div><dt>Market</dt><dd>{selectedOpportunity.region}</dd></div>
                  <div><dt>Buyer context</dt><dd>{selectedOpportunity.category}</dd></div>
                  <div><dt>Weakest link</dt><dd>{selectedOpportunity.weakestDimension}</dd></div>
                </dl>
              )}
              <div className="inspector-next"><span>Next move</span><p>{latestDecision?.next_actions[0] ?? (runtime.project ? 'Wait for Opportunity Lead to record the next gate.' : selectedOpportunity.nextMove)}</p></div>
              <ol className="progress-thread">
                {workspaceViews.map((view, index) => <li key={view.id} data-active={activeView === view.id} data-done={runtime.project && view.id !== 'project' ? runtime.project.assessments[view.id].status === 'validated' : workspaceViews.findIndex((item) => item.id === activeView) > index}><span />{view.label}</li>)}
              </ol>
              <button type="button" onClick={() => setActiveView(runtime.project ? weakestDimension : selectedOpportunity.weakestDimension.toLowerCase() as WorkspaceView)}>Validate weakest link <span aria-hidden="true">→</span></button>
            </aside>
          </div>

          {runtime.run && (
            <RunActivity
              run={runtime.run}
              events={runtime.events}
              assistantText={runtime.assistantText}
              reasoningSummary={runtime.reasoningSummary}
              permissions={runtime.permissions}
              connection={runtime.connection}
              isActive={runtime.isActive}
              onCancel={() => void runtime.cancel()}
              onDecision={(requestId, decision) => void runtime.decidePermission(requestId, decision)}
            />
          )}
        </div>
      </main>
    </div>
  )
}
