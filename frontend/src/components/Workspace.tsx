import { useMemo, useState } from 'react'
import Brand from './Brand'
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

  const runAnalysis = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const nextBrief = briefInput.trim()
    if (!nextBrief) return
    setCurrentBrief(nextBrief)
    setNotice('Research brief ready. Connect live sources to begin validation.')
    setActiveView('project')
  }

  const startNewAnalysis = () => {
    setBriefInput('')
    setCurrentBrief('New market opportunity')
    setNotice('Start with a product, market, capability, or customer question.')
    setActiveView('project')
    setRailOpen(false)
  }

  return (
    <div className="ophira-workspace">
      <aside className="workspace-rail" data-open={railOpen}>
        <a className="workspace-brand-link" href="/" aria-label="Back to OPHIRA website"><Brand compact /></a>
        <button className="new-analysis" type="button" onClick={startNewAnalysis}><span>＋</span>New analysis</button>

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
          {projectNames.map((project, index) => <button key={project} type="button" onClick={() => setSelectedId(opportunitySignals[index].id)}>{project}</button>)}
        </div>

        <div className="workspace-profile"><span>JB</span><div><strong>Ophira workspace</strong><small>Prototype mode</small></div></div>
      </aside>

      <main className="workspace-main">
        <header className="workspace-topbar">
          <button className="rail-toggle" type="button" aria-label="Open workspace navigation" onClick={() => setRailOpen((open) => !open)}><span /><span /></button>
          <div><span>Opportunity studio</span><strong>{currentBrief}</strong></div>
          <div className="topbar-actions"><span>Illustrative data</span><a href="/">Website ↗</a></div>
        </header>

        <div className="workspace-content">
          <section className="research-header" aria-labelledby="workspace-title">
            <div><p>Global opportunity research</p><h1 id="workspace-title">Turn a market question into a commercial path.</h1></div>
            <form onSubmit={runAnalysis}>
              <label htmlFor="workspace-brief">Research brief</label>
              <div><input id="workspace-brief" value={briefInput} onChange={(event) => setBriefInput(event.target.value)} placeholder="What market, capability, or customer should we explore?" /><button type="submit">Analyze <span aria-hidden="true">→</span></button></div>
            </form>
            {notice && <p className="analysis-notice" role="status">{notice}</p>}
          </section>

          <div className="research-body">
            <section className="research-surface">
              {activeView === 'project' && (
                <>
                  <div className="surface-heading"><div><span>Opportunity Lead</span><h2>One project truth. One weakest link.</h2></div><button type="button">Stage: Discovery</button></div>
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
              {activeView === 'demand' && <DemandValidation opportunity={selectedOpportunity} />}
              {activeView === 'supply' && <SupplyFit opportunity={selectedOpportunity} />}
              {activeView === 'capital' && <CapitalModel opportunity={selectedOpportunity} />}
              {activeView === 'sales' && <SalesPlan opportunity={selectedOpportunity} />}
            </section>

            <aside className="workspace-inspector">
              <div className="inspector-heading"><span>Decision thread</span><small>Sample</small></div>
              <h2>{selectedOpportunity.title}</h2>
              <dl>
                <div><dt>Market</dt><dd>{selectedOpportunity.region}</dd></div>
                <div><dt>Buyer context</dt><dd>{selectedOpportunity.category}</dd></div>
                <div><dt>Weakest link</dt><dd>{selectedOpportunity.weakestDimension}</dd></div>
              </dl>
              <div className="inspector-next"><span>Next move</span><p>{selectedOpportunity.nextMove}</p></div>
              <ol className="progress-thread">
                {workspaceViews.map((view, index) => <li key={view.id} data-active={activeView === view.id} data-done={workspaceViews.findIndex((item) => item.id === activeView) > index}><span />{view.label}</li>)}
              </ol>
              <button type="button" onClick={() => setActiveView(selectedOpportunity.weakestDimension.toLowerCase() as WorkspaceView)}>Validate weakest link <span aria-hidden="true">→</span></button>
            </aside>
          </div>
        </div>
      </main>
    </div>
  )
}
