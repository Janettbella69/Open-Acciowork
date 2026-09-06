import { useEffect, useRef, useState } from 'react'
import { errorMessage, listTaskExperiences, retireTaskExperience } from '../api/client'
import type { TaskExperienceEntry, TaskExperienceLedger } from '../api/types'
import { useLanguage } from '../i18n'
import './task-experience-panel.css'

export default function TaskExperiencePanel({ scheduleId }: { scheduleId: string }) {
  return <ScheduleExperience key={scheduleId} scheduleId={scheduleId} />
}

function ScheduleExperience({ scheduleId }: { scheduleId: string }) {
  const zh = useLanguage() === 'zh-CN'
  const [open, setOpen] = useState(false)
  const [state, setState] = useState<TaskExperienceLedger | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [retiring, setRetiring] = useState<TaskExperienceEntry | null>(null)
  const [reason, setReason] = useState('')
  const mounted = useRef(false)
  const inFlight = useRef(false)
  const ledger = useRef<TaskExperienceLedger | null>(null)

  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const mergeLedger = (incoming: TaskExperienceLedger, reconcileSelection = false) => {
    if (!mounted.current || incoming.schedule_id !== scheduleId) return
    // Read replicas/late responses must not resurrect a retired entry. The
    // whole ledger has one monotonic revision; configuration metadata may be
    // refreshed at the same revision without lowering recorded decisions.
    if (ledger.current && ledger.current.revision > incoming.revision) return
    ledger.current = incoming
    setState(incoming)
    if (reconcileSelection && retiring) {
      const refreshed = incoming.entries.find((entry) => entry.id === retiring.id)
      if (!refreshed || refreshed.retired_at || refreshed.revisions.at(-1)?.version !== retiring.revisions.at(-1)?.version) {
        setRetiring(null)
        setReason('')
        setNotice(zh ? '所选经验已变更或不可用。请查看刷新后的经验，再继续操作。'
          : 'The selected note changed or is unavailable. Review the refreshed notes before continuing.')
      }
    }
  }

  const refresh = async () => {
    if (inFlight.current || !mounted.current) return
    inFlight.current = true
    setBusy(true)
    setError('')
    setNotice('')
    try { mergeLedger(await listTaskExperiences(scheduleId), true) }
    catch (failure) { if (mounted.current) setError(errorMessage(failure)) }
    finally { inFlight.current = false; if (mounted.current) setBusy(false) }
  }

  const retire = async () => {
    if (!retiring || !reason.trim() || inFlight.current || !mounted.current) return
    inFlight.current = true
    setBusy(true)
    setError('')
    try {
      mergeLedger(await retireTaskExperience(scheduleId, retiring.id, {
        expected_version: retiring.revisions.at(-1)!.version, reason: reason.trim(),
      }))
      if (!mounted.current) return
      setRetiring(null)
      setReason('')
    } catch (failure) { if (mounted.current) setError(errorMessage(failure)) }
    finally { inFlight.current = false; if (mounted.current) setBusy(false) }
  }

  return (
    <section className="task-experience-panel" aria-label={zh ? '任务专属经验' : 'Task experience'}>
      <button type="button" aria-expanded={open} onClick={() => {
        setOpen(!open)
        if (!open) void refresh()
      }}>{zh ? '任务专属经验' : 'Task experience'}</button>
      {open && <div>
        <p>{zh
          ? '仅用于本计划的低信任观察，不是全局记忆或策略。无需逐条审批；不会修改触发阈值、权限或预算。来源锚点可追溯，但不等于事实已核实。'
          : 'Low-trust observations for this schedule, not global Memory or policy. No per-note approval; trigger thresholds, permissions and budgets stay unchanged. Source anchors are traceable, not proof of correctness.'}</p>
        <button type="button" disabled={busy} onClick={() => void refresh()}>
          {zh ? '刷新经验' : 'Refresh experience'}
        </button>
        {busy && <p role="status">{zh ? '处理中…' : 'Loading…'}</p>}
        {error && <p role="alert">{error}</p>}
        {notice && <p role="status">{notice}</p>}
        {state && <>
          <p>{zh ? '经验账本版本' : 'Ledger revision'} {state.revision} · {state.entries.length}/{state.limits.entries}</p>
          {!state.entries.length && <p>{zh ? '尚无经验；计划运行后可由主 Agent 记录。' : 'No experience yet. The main Agent can record notes during a scheduled run.'}</p>}
          {state.entries.map((entry) => {
            const latest = entry.revisions.at(-1)!
            const status = entry.retired_at ? (zh ? '已停用' : 'Retired')
              : entry.stale ? (zh ? '配置已变更 · 不注入' : 'Stale configuration · excluded')
                : (zh ? '下次计划可参考' : 'Available for the next scheduled run')
            return <section className="task-experience-entry" key={entry.id}>
              <h4><code>{entry.key}</code> · v{latest.version}</h4>
              <p>{status}</p>
              <p>{latest.observation}</p><p>{latest.lesson}</p>
              <details>
                <summary>{zh ? '版本与来源' : 'Versions and provenance'}</summary>
                {entry.revisions.slice().reverse().map((revision) => <div key={revision.version}>
                  <h5>v{revision.version} · {new Date(revision.created_at).toLocaleString()}</h5>
                  <p>{revision.observation}</p><p>{revision.lesson}</p>
                  <p>Run: <code>{revision.source_run_id}</code></p>
                  <p>Trace: <code>{revision.source_trace_id}</code></p>
                  <p>SDK: <code>{revision.source_session_id}</code></p>
                  <p>{zh ? '记录事件' : 'Write event'}: <code>{revision.write_event_id}</code></p>
                  <ul>{revision.source_event_ids.map((eventId) => <li key={eventId}>
                    <code>{eventId}</code> · SHA256 <code>{revision.source_event_hashes[eventId]}</code>
                  </li>)}</ul>
                  <p>{zh ? '配置指纹' : 'Configuration hash'}: <code>{revision.config_hash}</code></p>
                  <p>{zh ? '内容指纹' : 'Content hash'}: <code>{revision.content_hash}</code></p>
                </div>)}
                {entry.pruned_versions > 0 && <p>{zh ? '已裁剪旧版本' : 'Pruned older versions'}: {entry.pruned_versions}</p>}
              </details>
              {entry.retired_at ? <p>{entry.retirement_reason} · {entry.retired_by}</p> : <button
                type="button" disabled={busy} onClick={() => { setRetiring(entry); setReason(''); setNotice('') }}
              >{zh ? '停用这条经验' : 'Retire this note'}</button>}
            </section>
          })}
          {state.evicted_retired_entries > 0 && <p>{zh ? '已释放停用条目' : 'Evicted retired entries'}: {state.evicted_retired_entries}</p>}
        </>}
        {retiring && <form onSubmit={(event) => { event.preventDefault(); void retire() }}>
          <p className="task-experience-retire-target">{zh ? '停用' : 'Retire'} {retiring.key} · v{retiring.revisions.at(-1)!.version}</p>
          <label>{zh ? '停用原因' : 'Retirement reason'}
            <input required maxLength={500} value={reason} onChange={(event) => setReason(event.target.value)} />
          </label>
          <button type="submit" disabled={busy || !reason.trim()}>{zh ? '确认停用' : 'Confirm retirement'}</button>
          <button type="button" disabled={busy} onClick={() => setRetiring(null)}>{zh ? '取消' : 'Cancel'}</button>
        </form>}
      </div>}
    </section>
  )
}
