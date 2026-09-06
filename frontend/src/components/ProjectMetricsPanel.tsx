import { useEffect, useId, useState } from 'react'
import { errorMessage, getProjectMetrics } from '../api/client'
import type { InvocationMetricsGroup, MetricDistribution, MetricRate, ProjectMetrics } from '../api/projectMetrics'
import { useLanguage, useT, type Translate } from '../i18n'
import './project-metrics.css'

function duration(value: number | null | undefined, t: Translate): string {
  if (value == null || !Number.isFinite(value)) return t('Unavailable')
  return value < 1000 ? `${Math.round(value)} ms` : `${(value / 1000).toFixed(1)} s`
}

function rate(value: MetricRate, t: Translate): string {
  return value.value == null ? t('Unavailable') : `${(value.value * 100).toFixed(1)}%`
}

function cost(metric: MetricDistribution | null, t: Translate): string {
  return !metric?.observed_count || metric.observed_total == null
    ? t('Unavailable') : `$${metric.observed_total.toFixed(4)}`
}

function Coverage({ metric }: { metric: MetricDistribution }) {
  const t = useT()
  return <small>{t('{observed} measured · {missing} unavailable', {
    observed: metric.observed_count, missing: metric.unavailable_count,
  })}</small>
}

function InvocationTable({ title, groups }: { title: string; groups: InvocationMetricsGroup[] }) {
  const t = useT()
  return <details className="project-metrics-group">
    <summary>{t(title)} <span>{groups.length}</span></summary>
    <div className="project-metrics-table"><table>
      <caption>{t(title)}</caption>
      <thead><tr><th>{t('Name')}</th><th>{t('Runs')}</th><th>{t('Calls')}</th><th>{t('Completed')}</th><th>{t('Failed')}</th><th>{t('Unknown / pending')}</th><th>{t('Tool duration P95')}</th></tr></thead>
      <tbody>{groups.map((group, index) => <tr key={`${group.identifier}:${index}`}>
        <th scope="row">{group.identifier ?? t('Unknown')}</th>
        <td>{group.run_count}</td><td>{group.invocations.invocation_count}</td>
        <td>{group.invocations.completed_count}</td><td>{group.invocations.failed_count}</td>
        <td>{group.invocations.unknown_count} / {group.invocations.pending_count}</td>
        <td>{duration(group.invocations.duration_ms.p95, t)}<Coverage metric={group.invocations.duration_ms} /></td>
      </tr>)}</tbody>
    </table></div>
    {groups.length === 0 && <p>{t('No recorded calls in the available logs.')}</p>}
  </details>
}

function MetricsView({ metrics }: { metrics: ProjectMetrics }) {
  const t = useT()
  const language = useLanguage()
  const { summary, coverage, window: period } = metrics
  return <>
    <p className="project-metrics-coverage">{t('{shown} of {total} runs in this period', { shown: coverage.selected_runs, total: coverage.matching_runs })}
      <small>{new Date(period.created_since).toLocaleString(language)} — {new Date(period.created_until).toLocaleString(language)}</small>
    </p>
    {coverage.omitted_by_run_limit > 0 && <p>{t('{n} older runs are outside this result limit.', { n: coverage.omitted_by_run_limit })}</p>}
    {(coverage.unavailable_event_replays > 0 || coverage.invalid_created_at_runs > 0) && <p className="project-metrics-incomplete" role="status">
      {t('{logs} run logs and {dates} creation dates could not be included.', { logs: coverage.unavailable_event_replays, dates: coverage.invalid_created_at_runs })}
    </p>}
    {((coverage.unattributed_tool_events ?? 0) > 0 || (coverage.unclassified_tool_invocations ?? 0) > 0 || (coverage.group_overflow_associations ?? 0) > 0) && <p className="project-metrics-incomplete">
      {t('{events} tool events lack an anchor, {calls} calls lack a category, and {groups} group associations exceed the display limit.', {
        events: coverage.unattributed_tool_events ?? 0, calls: coverage.unclassified_tool_invocations ?? 0, groups: coverage.group_overflow_associations ?? 0,
      })}
    </p>}
    {summary.run_count === 0 ? <p>{t('No runs were created in this period.')}</p> : <>
      <dl className="project-metrics-summary">
        <div><dt>{t('Recorded runs')}</dt><dd>{summary.run_count}</dd><small>{t('{completed} completed · {failed} failed · {cancelled} cancelled', { completed: summary.status_counts.completed ?? 0, failed: summary.status_counts.failed ?? 0, cancelled: summary.status_counts.cancelled ?? 0 })}</small></div>
        <div><dt>{t('Execution failure rate')}</dt><dd>{rate(summary.execution_failure_rate, t)}</dd><small>{t('{n} terminal executions', { n: summary.execution_failure_rate.denominator })}</small></div>
        <div><dt>{t('Task duration P95')}</dt><dd>{duration(summary.duration_ms.p95, t)}</dd><Coverage metric={summary.duration_ms} /></div>
        <div><dt>{t('Queue duration P95')}</dt><dd>{duration(summary.queue_duration_ms.p95, t)}</dd><Coverage metric={summary.queue_duration_ms} /></div>
        <div><dt>{t('First token P95')}</dt><dd>{duration(summary.ttft_ms.p95, t)}</dd><Coverage metric={summary.ttft_ms} /></div>
        <div><dt>{t('Observed SDK estimate')}</dt><dd>{cost(summary.cost_estimate_usd, t)}</dd>{summary.cost_estimate_usd && <Coverage metric={summary.cost_estimate_usd} />}</div>
      </dl>
      <p className="project-metrics-note">{t('Execution statistics describe recorded activity. Business usefulness requires user confirmation.')}</p>
      <details className="project-metrics-group">
        <summary>{t('By Provider and configured model')} <span>{metrics.providers.length}</span></summary>
        <div className="project-metrics-table"><table>
          <caption>{t('By Provider and configured model')}</caption>
          <thead><tr><th>{t('Provider')}</th><th>{t('Configured model')}</th><th>{t('Runs')}</th><th>{t('Failed')}</th><th>{t('Task duration P95')}</th><th>{t('Observed SDK estimate')}</th></tr></thead>
          <tbody>{metrics.providers.map((group, index) => <tr key={index}>
            <th scope="row">{group.dimensions.provider_profile_id ?? t('Unknown')}</th><td>{group.dimensions.configured_model ?? t('Unknown')}</td>
            <td>{group.runs.run_count}</td><td>{group.runs.status_counts.failed ?? 0}</td><td>{duration(group.runs.duration_ms.p95, t)}</td>
            <td>{cost(group.runs.cost_estimate_usd, t)}{group.runs.cost_estimate_usd && <Coverage metric={group.runs.cost_estimate_usd} />}</td>
          </tr>)}</tbody>
        </table></div>
      </details>
      <details className="project-metrics-group">
        <summary>{t('By observed model')} <span>{metrics.models.length}</span></summary>
        <p>{t('A run can appear under multiple observed models; group counts are not additive.')}</p>
        <div className="project-metrics-table"><table>
          <caption>{t('By observed model')}</caption>
          <thead><tr><th>{t('Observed model')}</th><th>{t('Runs')}</th><th>{t('Failed')}</th><th>{t('Task duration P95')}</th></tr></thead>
          <tbody>{metrics.models.map((group, index) => <tr key={index}>
            <th scope="row">{group.dimensions.observed_model ?? t('Unknown')}</th><td>{group.runs.run_count}</td>
            <td>{group.runs.status_counts.failed ?? 0}</td><td>{duration(group.runs.duration_ms.p95, t)}</td>
          </tr>)}</tbody>
        </table></div>
      </details>
      <InvocationTable title="Skill calls" groups={metrics.skills} />
      <InvocationTable title="MCP tool calls" groups={metrics.mcp_tools} />
      <details className="project-metrics-group">
        <summary>{t('Recorded error categories')} <span>{metrics.errors.length}</span></summary>
        {metrics.errors.length ? <ul>{metrics.errors.map((group, index) => <li key={index}>{group.code ?? t('Unknown')} <strong>{group.failed_run_count}</strong></li>)}</ul> : <p>{t('No recorded error categories.')}</p>}
      </details>
    </>}
  </>
}

function MetricsRequest({ projectId, days }: { projectId: string; days: number }) {
  const t = useT()
  const [result, setResult] = useState<{ metrics: ProjectMetrics | null; error: string }>({ metrics: null, error: '' })
  useEffect(() => {
    const controller = new AbortController()
    let active = true
    void getProjectMetrics(projectId, days, controller.signal).then((metrics) => {
      if (!active) return
      if (metrics.project_id !== projectId) {
        setResult({ metrics: null, error: 'Metrics response did not match this project.' })
        return
      }
      setResult({ metrics, error: '' })
    }).catch((failure: unknown) => {
      if (active) setResult({ metrics: null, error: errorMessage(failure) })
    })
    return () => { active = false; controller.abort() }
  }, [projectId, days])
  if (result.error) return <p role="alert">{t(result.error)}</p>
  if (!result.metrics) return <p role="status">{t('Loading project metrics…')}</p>
  return <MetricsView metrics={result.metrics} />
}

function ScopedPanel({ projectId }: { projectId: string }) {
  const t = useT()
  const bodyId = useId()
  const [open, setOpen] = useState(false)
  const [days, setDays] = useState(7)
  const [refresh, setRefresh] = useState(0)
  return <section className="project-metrics">
    <button className="project-metrics-toggle" type="button" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(!open)}>
      <span>{t('Project run metrics')}</span><span aria-hidden="true">{open ? '−' : '+'}</span>
    </button>
    {open && <div id={bodyId}>
      <div className="project-metrics-controls">
        <label>{t('Metrics period')} <select value={days} onChange={(event) => setDays(Number(event.target.value))}>
          <option value={7}>{t('Last 7 days')}</option><option value={30}>{t('Last 30 days')}</option>
        </select></label>
        <button type="button" onClick={() => setRefresh(refresh + 1)}>{t('Refresh metrics')}</button>
      </div>
      <MetricsRequest key={`${projectId}:${days}:${refresh}`} projectId={projectId} days={days} />
    </div>}
  </section>
}

export default function ProjectMetricsPanel({ projectId }: { projectId: string }) {
  return <ScopedPanel key={projectId} projectId={projectId} />
}
