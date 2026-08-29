import { opportunitySignals } from '../data'

export default function OpportunityPreview() {
  const lead = opportunitySignals[0]

  return (
    <div className="ophira-product-window" aria-label="TradeMind opportunity workspace preview">
      <div className="preview-topbar">
        <div className="preview-dots" aria-hidden="true"><span /><span /><span /></div>
        <div className="preview-project"><strong>New market study</strong><span>Illustrative workspace</span></div>
        <span className="preview-status">Opportunity brief</span>
      </div>

      <div className="preview-layout">
        <aside className="preview-rail">
          <p>Workspace</p>
          {['Project', 'Demand', 'Supply', 'Capital', 'Sales'].map((item, index) => (
            <span key={item} data-active={index === 0}><i>{String(index + 1).padStart(2, '0')}</i>{item}</span>
          ))}
        </aside>

        <div className="preview-canvas">
          <div className="preview-heading">
            <div><span>{lead.region} · {lead.category}</span><h3>{lead.title}</h3></div>
            <button type="button">Build brief <span aria-hidden="true">→</span></button>
          </div>
          <div className="preview-evidence">
            <article>
              <span>Demand signal</span>
              <p>{lead.signal}</p>
            </article>
            <article>
              <span>Supply fit</span>
              <p>{lead.supplyFit}</p>
            </article>
          </div>
          <div className="preview-opportunity-list">
            {opportunitySignals.map((opportunity, index) => (
              <div key={opportunity.id} data-active={index === 0}>
                <span>{String(index + 1).padStart(2, '0')}</span>
                <strong>{opportunity.title}</strong>
                <small>{opportunity.region}</small>
              </div>
            ))}
          </div>
        </div>

        <aside className="preview-action-panel">
          <span>Weakest link</span>
          <h4>{lead.weakestDimension} needs the next proof.</h4>
          <p>{lead.nextMove}</p>
          <div>{lead.tags.map((tag) => <small key={tag}>{tag}</small>)}</div>
        </aside>
      </div>
    </div>
  )
}
