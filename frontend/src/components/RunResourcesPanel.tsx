import { useEffect, useRef, useState } from 'react'
import { errorMessage, getRunResources } from '../api/client'
import type { RunResources } from '../api/runResources'
import type { RunRecord } from '../api/types'
import { useT } from '../i18n'
import './run-resources.css'

const terminal = new Set(['completed', 'failed', 'cancelled'])
const unitLabels: Record<string, string> = {
  'source-mapper': 'Source mapping',
  'multilingual-researcher': 'Multilingual research',
  'conflict-verifier': 'Conflict verification',
}
const stateLabels: Record<string, string> = {
  running: 'Running', completed: 'Completed', failed: 'Failed', stopped: 'Stopped', unknown: 'Unknown',
}
function duration(seconds: number | null) {
  if (seconds === null) return '—'
  const whole = Math.max(0, Math.floor(seconds))
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`
}
function number(value: number | null) { return value === null ? '—' : value.toLocaleString() }
function cost(value: number | null) { return value === null ? '—' : `$${value.toFixed(4)}` }
function bytes(value: number | undefined) {
  if (value === undefined) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`
  return `${(value / 1024 ** 2).toFixed(1)} MiB`
}

function WorkerResources({ limits }: { limits: RunResources['limits'] }) {
  const t = useT()
  const worker = limits.worker
  if (!worker) return <p>{t('Worker limits have not been recorded for this run.')}</p>
  const outcomes: Record<string, string> = {
    completed: 'Worker finished', terminated: 'Worker stopped', disconnected: 'Worker disconnected',
    descendants_remaining: 'Worker exited with background processes',
    output_limit: 'Output limit reached', wall_clock_limit: 'Original deadline reached',
    worker_failed: 'Worker failed', launch_failed: 'Worker could not start',
    cleanup_failed: 'Worker cleanup could not be confirmed', parent_exited: 'Host exited',
    unconfirmed: 'Worker result could not be confirmed',
  }
  return <div className="worker-resources">
    <p>{t(worker.mode === 'native'
      ? 'Time and output limits are enforced. CPU, memory and process limits require Linux isolation.'
      : limits.worker_kernel_limits_verified
        ? 'Linux CPU, memory and process limits verified at startup.'
        : 'Linux resource limits are configured; enforcement has not been verified for this worker.')}</p>
    <dl className="run-resource-metrics">
      <div><dt>{t('Output limit')}</dt><dd>{bytes(worker.output_bytes)}</dd></div>
      <div><dt>{t('Recorded worker output')}</dt><dd>{bytes(limits.worker_output_bytes)}</dd></div>
      {worker.mode === 'systemd' && <>
        <div><dt>{t('Configured CPU limit')}</dt><dd>{worker.cpu_percent}%</dd></div>
        <div><dt>{t('Configured memory limit')}</dt><dd>{bytes(worker.memory_bytes)}</dd></div>
        <div><dt>{t('Configured process and thread limit')}</dt><dd>{number(worker.tasks_max)}</dd></div>
      </>}
    </dl>
    <small>{t('Output is counted when each worker finishes; the current worker’s usage is not shown yet.')}</small>
    {limits.worker_output_exhausted && <p role="status">{t('The previous output total is unconfirmed. This run cannot resume; start a new run.')}</p>}
    {limits.worker_last_outcome && <small>{t(outcomes[limits.worker_last_outcome] ?? 'Worker result could not be confirmed')}</small>}
  </div>
}

type Props = { run: RunRecord; onCancel: () => void }

function Resources({ run, onCancel }: Props) {
  const t = useT()
  const [data, setData] = useState<RunResources | null>(null)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [tick, setTick] = useState(0)
  const receivedAt = useRef(0)
  useEffect(() => {
    let active = true
    let timer: number | undefined
    const controller = new AbortController()
    const poll = async () => {
      let finished = terminal.has(run.status)
      try {
        const value = await getRunResources(run.id, controller.signal)
        if (!active) return
        if (value.run_id !== run.id || value.project_id !== run.project_id || value.trace_id !== run.trace_id) {
          throw new Error('Resource response did not match this run.')
        }
        receivedAt.current = performance.now()
        setData(value); setError(''); setTick(0)
        finished = finished || terminal.has(value.status)
      } catch (reason) {
        if (active) { setData(null); setError(errorMessage(reason)) }
      } finally {
        if (active && !finished) timer = window.setTimeout(() => void poll(), 5000)
      }
    }
    void poll()
    return () => { active = false; controller.abort(); window.clearTimeout(timer) }
  }, [run.id, run.project_id, run.trace_id, run.status, refresh])

  useEffect(() => {
    if (!data || terminal.has(data.status)) return
    const timer = window.setInterval(() => setTick((value) => value + 1), 1000)
    return () => window.clearInterval(timer)
  }, [data])
  // Anchor interpolation to the server's remaining duration, not the user's
  // potentially skewed wall clock. The server alone enforces the deadline.
  const delta = tick && data && !terminal.has(data.status) ? (performance.now() - receivedAt.current) / 1000 : 0
  const elapsed = data?.elapsed_seconds === null || !data ? null : data.elapsed_seconds + delta
  const remaining = data?.remaining_seconds === null || !data ? null : Math.max(0, data.remaining_seconds - delta)
  const active = !terminal.has(run.status) && (!data || !terminal.has(data.status))
  const research = run.mode === 'deep_research'
  return <section className="run-resources" aria-label={t(research ? 'Deep Research resources' : 'Run resources')}>
    <header><strong>{t(research ? 'Deep Research resources' : 'Run resources')}</strong>
      {active && <button type="button" onClick={onCancel} disabled={run.status === 'cancelling'}>{t(research ? 'Cancel research' : 'Cancel run')}</button>}
    </header>
    {research && <p>{t('Three read-only research units share this run’s budget. The main agent verifies and merges their sources. Cancel stops the whole research run.')}</p>}
    {error && <div role="alert"><p>{t('Resource updates are unavailable. Previous values have been cleared.')}</p>
      <small>{t(error)}</small><button type="button" onClick={() => setRefresh((value) => value + 1)}>{t('Refresh resources')}</button>
    </div>}
    {!data && !error && <p role="status">{t('Loading run resources…')}</p>}
    {data && <>
      {!data.limits.frozen && <p role="status">{t('The run’s resource limits have not been initialized yet.')}</p>}
      <dl className="run-resource-metrics">
        <div><dt>{t('Shared SDK budget')}</dt><dd>{cost(data.limits.sdk_budget_usd)}</dd></div>
        <div><dt>{t('Reported SDK cost')}</dt><dd>{cost(data.cost.estimated_cost_usd.value)}</dd></div>
        <div><dt>{t('Remaining reported budget')}</dt><dd>{cost(data.remaining_budget_usd)}</dd></div>
        <div><dt>{t('Elapsed time')}</dt><dd>{duration(elapsed)}</dd></div>
        <div><dt>{t('Time to deadline')}</dt><dd>{duration(remaining)}</dd></div>
        <div><dt>{t('Turn limit')}</dt><dd>{!data.limits.frozen ? '—' : data.limits.turn_limit === null ? t('Unlimited turns') : number(data.limits.turn_limit)}</dd></div>
      </dl>
      <small>{t('Costs use SDK estimates, not billing data. Unreported spend is unknown; reported budget remaining may be out of date.')}</small>
      {data.limits.wall_clock_deadline && <small>{t('Original deadline: {time}', { time: new Date(data.limits.wall_clock_deadline).toLocaleString() })}</small>}
      <WorkerResources limits={data.limits} />
      {!data.observations_available ? <p role="status">{t('The event history is incomplete or exceeds the replay limit. Task and usage observations are unavailable.')}</p> : <>
        <dl className="run-resource-metrics">
          <div><dt>{t('Reported input tokens')}</dt><dd>{number(data.tokens.input_tokens.value)}</dd></div>
          <div><dt>{t('Reported output tokens')}</dt><dd>{number(data.tokens.output_tokens.value)}</dd></div>
        </dl>
        <small>{t(data.tokens.input_tokens.scope === 'main_agent_only'
          ? 'These token counters cover the main agent only.'
          : 'Run token totals use reported model usage when available. Task snapshots below are not added to them.')}</small>
        {research && <ul className="research-unit-observations">{data.research_units.map((unit) => {
          const observed = data.agents.filter((agent) => agent.agent_type === unit.id)
          return <li key={unit.id}><strong>{t(unitLabels[unit.id] ?? unit.id)}</strong><small>{observed.length
            ? t('{count} native invocations observed', { count: observed.length })
            : t('No invocation reported')}</small></li>
        })}</ul>}
        <details className="research-task-observations"><summary>{t('Reported subtasks · {count}', { count: data.tasks.length })}</summary>
          {data.tasks.length === 0 && <p>{t('No native task progress has been reported yet.')}</p>}
          <ul>{data.tasks.map((task) => <li key={task.task_id}>
            <header><strong>{t(unitLabels[task.requested_agent_type ?? ''] ?? task.task_type ?? 'Native task')}</strong>
              <span>{t(stateLabels[task.status])}</span></header>
            {task.description && <p>{task.description}</p>}
            <dl className="run-resource-metrics">
              <div><dt>{t('Reported task tokens')}</dt><dd>{number(task.usage.total_tokens)}</dd></div>
              <div><dt>{t('Reported tool uses')}</dt><dd>{number(task.usage.tool_uses)}</dd></div>
              <div><dt>{t('Reported task duration')}</dt><dd>{duration(task.usage.duration_ms === null ? null : task.usage.duration_ms / 1000)}</dd></div>
            </dl>
            {task.last_tool_name && <small>{t('Last reported tool: {tool}', { tool: task.last_tool_name })}</small>}
            <small>{t('Last observed: {time}', { time: new Date(task.last_observed_at).toLocaleString() })}</small>
            {task.usage.source_sequence !== null && <small>{t('Usage snapshot: event {sequence}', { sequence: task.usage.source_sequence })}</small>}
            {task.warnings.length > 0 && <p>{t('Some task observations conflict or are incomplete. Check the activity trace.')}</p>}
            {terminal.has(data.status) && task.status === 'running' && <p>{t('The run has ended; this task’s terminal observation is missing.')}</p>}
          </li>)}</ul>
        </details>
      </>}
      <small>{t('Resources observed at {time}', { time: new Date(data.observed_at).toLocaleString() })}</small>
    </>}
  </section>
}

function Disclosure(props: Props) {
  const t = useT()
  const [open, setOpen] = useState(false)
  if (props.run.mode === 'deep_research') return <Resources {...props} />
  return <details className="run-resources-disclosure" onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>{t('Run resources')}</summary>
    {open && <Resources {...props} />}
  </details>
}

export default function RunResourcesPanel(props: Props) {
  return <Disclosure key={`${props.run.project_id}:${props.run.id}`} {...props} />
}
