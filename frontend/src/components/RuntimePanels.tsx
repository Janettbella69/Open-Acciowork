import type {
  Dimension,
  OpportunityProject,
  OphiraEvent,
  PendingPermission,
  RunRecord,
} from '../api/types'
import { dimensions } from '../api/types'

function label(value: string): string {
  return value.replace(/[._]/g, ' ').replace(/\b\w/g, (character) => character.toUpperCase())
}

function compactJson(value: unknown): string {
  const serialized = JSON.stringify(value, null, 2) ?? ''
  return serialized.length > 3_000 ? `${serialized.slice(0, 3_000)}\n…` : serialized
}

export function LiveProjectTruth({
  project,
  onSelectDimension,
}: {
  project: OpportunityProject
  onSelectDimension: (dimension: Dimension) => void
}) {
  const latestDecision = project.stage_decisions.at(-1)
  return (
    <div className="live-project-truth">
      <div className="live-project-objective">
        <span>Authoritative objective</span>
        <h3>{project.objective}</h3>
        <p>Revision {project.revision} · {project.hypotheses.length} hypotheses · {project.evidence.length} evidence records</p>
      </div>

      <div className="assessment-grid" aria-label="Live dimension assessments">
        {dimensions.map((dimension) => {
          const assessment = project.assessments[dimension]
          return (
            <button key={dimension} type="button" onClick={() => onSelectDimension(dimension)}>
              <span>{label(dimension)}</span>
              <strong>{assessment.score}</strong>
              <small data-status={assessment.status}>{label(assessment.status)}</small>
              <p>{assessment.summary || assessment.gaps[0] || 'Waiting for the specialist Agent.'}</p>
            </button>
          )
        })}
      </div>

      {latestDecision ? (
        <div className="live-stage-decision">
          <div><span>Latest gate</span><strong>{label(latestDecision.decision)}</strong></div>
          <div><span>Weakest dimension</span><strong>{label(latestDecision.weakest_dimension)}</strong></div>
          <p>{latestDecision.rationale}</p>
        </div>
      ) : (
        <p className="module-note">Opportunity Lead has not recorded a stage decision yet.</p>
      )}
    </div>
  )
}

export function LiveDimensionView({
  project,
  dimension,
}: {
  project: OpportunityProject
  dimension: Dimension
}) {
  const assessment = project.assessments[dimension]
  const hypotheses = project.hypotheses.filter((item) => item.dimension === dimension)
  const evidence = project.evidence.filter((item) => item.dimension === dimension)
  return (
    <section className="workspace-module live-dimension" aria-labelledby={`live-${dimension}-heading`}>
      <div className="module-heading">
        <div><span>{label(dimension)} Agent</span><h2 id={`live-${dimension}-heading`}>{assessment.summary || `${label(dimension)} validation`}</h2></div>
        <span className="dimension-score">{assessment.score} / 100</span>
      </div>
      <div className="live-dimension-meta">
        <span data-status={assessment.status}>{label(assessment.status)}</span>
        <small>Updated by {assessment.updated_by}</small>
      </div>

      <div className="live-ledger-section">
        <span>Hypotheses</span>
        {hypotheses.length ? hypotheses.map((hypothesis) => (
          <article key={hypothesis.id}>
            <div><strong>{hypothesis.statement}</strong><small>Priority {hypothesis.priority} · {label(hypothesis.status)}</small></div>
            {hypothesis.validation_criteria.length > 0 && <ul>{hypothesis.validation_criteria.map((criterion) => <li key={criterion}>{criterion}</li>)}</ul>}
          </article>
        )) : <p>No hypothesis has been recorded for this dimension yet.</p>}
      </div>

      <div className="live-ledger-section">
        <span>Evidence</span>
        {evidence.length ? evidence.map((item) => (
          <article key={item.id}>
            <div><strong>{item.claim}</strong><small>{label(item.kind)} · {Math.round(item.confidence * 100)}% confidence</small></div>
            <p>{item.source_url ? <a href={item.source_url} target="_blank" rel="noreferrer">{item.source_title} ↗</a> : item.source_title}</p>
          </article>
        )) : <p>No sourced evidence has been recorded yet.</p>}
      </div>

      <div className="live-gaps">
        <span>Open gaps</span>
        {assessment.gaps.length ? <ul>{assessment.gaps.map((gap) => <li key={gap}>{gap}</li>)}</ul> : <p>No open gap is currently recorded.</p>}
      </div>
    </section>
  )
}

function eventTitle(event: OphiraEvent): string {
  const toolName = typeof event.data.tool_name === 'string' ? event.data.tool_name : ''
  const agentType = typeof event.data.agent_type === 'string' ? event.data.agent_type : ''
  const taskDescription = typeof event.data.description === 'string' ? event.data.description : ''
  if (event.type.startsWith('tool.')) return `${label(event.type)} · ${toolName || 'Tool'}`
  if (event.type.startsWith('subagent.')) return `${label(event.type)} · ${agentType || event.actor.name}`
  if (event.type.startsWith('task.')) return `${label(event.type)} · ${taskDescription || 'Task'}`
  if (event.type === 'sdk.system' && typeof event.data.subtype === 'string') return `SDK · ${label(event.data.subtype)}`
  return label(event.type)
}

export function RunActivity({
  run,
  events,
  assistantText,
  reasoningSummary,
  permissions,
  connection,
  isActive,
  onCancel,
  onDecision,
}: {
  run: RunRecord
  events: OphiraEvent[]
  assistantText: string
  reasoningSummary: string
  permissions: PendingPermission[]
  connection: string
  isActive: boolean
  onCancel: () => void
  onDecision: (requestId: string, decision: 'allow_once' | 'allow_run' | 'deny') => void
}) {
  const activity = events.filter((event) => ![
    'assistant.message.completed',
    'assistant.reasoning_summary.completed',
  ].includes(event.type)).slice(-50)

  return (
    <section className="run-activity" aria-labelledby="run-activity-heading">
      <header>
        <div><span>Claude Agent SDK · Live trace</span><h2 id="run-activity-heading">Opportunity team activity</h2></div>
        <div className="run-state"><span data-status={run.status}>{label(run.status)}</span><small>{connection}</small>{isActive && <button type="button" onClick={onCancel}>Cancel run</button>}</div>
      </header>
      <div className="run-identifiers"><span>Run {run.id}</span><span>Trace {run.trace_id}</span>{run.sdk_session_id && <span>Session {run.sdk_session_id}</span>}</div>

      {permissions.map((permission) => (
        <article className="permission-card" key={permission.id}>
          <div><span>Approval required</span><strong>{permission.actor.name} wants to use {permission.toolName}</strong></div>
          <pre>{compactJson(permission.toolInput)}</pre>
          <div className="permission-actions">
            <button type="button" onClick={() => onDecision(permission.id, 'allow_once')}>Allow once</button>
            <button type="button" onClick={() => onDecision(permission.id, 'allow_run')}>Allow for run</button>
            <button type="button" onClick={() => onDecision(permission.id, 'deny')}>Deny</button>
          </div>
        </article>
      ))}

      <div className="run-output-grid">
        <article>
          <span>Visible reasoning summary</span>
          <p>{reasoningSummary || (isActive ? 'Waiting for a user-visible summary…' : 'No reasoning summary was emitted.')}</p>
        </article>
        <article>
          <span>Agent output</span>
          <p>{assistantText || (isActive ? 'Opportunity Lead is starting…' : 'No assistant output was emitted.')}</p>
        </article>
      </div>

      <div className="activity-timeline">
        <div><span>Observable event stream</span><small>{activity.length} recent events</small></div>
        {activity.length ? activity.map((event) => (
          <details key={event.id} className="activity-event" data-event={event.type}>
            <summary><i /><span>{eventTitle(event)}</span><small>{event.actor.name} · #{event.sequence}</small></summary>
            <pre>{compactJson(event.data)}</pre>
          </details>
        )) : <p>Runtime events will appear here as soon as the run starts.</p>}
      </div>
    </section>
  )
}
