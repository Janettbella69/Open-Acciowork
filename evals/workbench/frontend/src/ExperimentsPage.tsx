import { useRemote } from './useRemote'
import { useCallback } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from './api'
import { Pending, Shell, TraceTable } from './shared'

export default function ExperimentsPage() {
  const { id } = useParams()
  const experiments = useRemote(api.experiments)
  const traces = useRemote(useCallback(() => api.traces(id), [id]))
  if (!experiments.data || !traces.data) return <Shell title="实验"><Pending error={experiments.error || traces.error} retry={() => { experiments.reload(); traces.reload() }} /></Shell>
  const current = experiments.data.find(e => e.id === id)
  if (id && !current) return <Shell title="实验不存在"><Link to="/experiments">返回实验列表</Link></Shell>
  const totalItems = experiments.data.reduce((s, e) => s + e.counts.items, 0)
  return <Shell title={current?.name || '实验'} subtitle="从真实运行归档进入 Trace、证据、人工复核与版本对比。">
    <div className="flex flex-wrap gap-3 text-sm">
      <Link className="accio-button" to="/traces">浏览全部 Trace</Link>
      <Link className="accio-button" to={`/compare${id ? `?left=${id}` : ''}`}>实验对比</Link>
      <Link className="accio-button" to="/review">人工复核</Link>
    </div>
    {current ? <>
      <Link className="text-sm" to="/experiments">← 全部实验</Link>
      <div className="accio-panel text-sm flex flex-wrap gap-6">
        <span>{current.provider || 'Provider 未记录'}</span><span>{current.generated_at || '时间未知'}</span>
        <span>{current.counts.completed} / {current.case_count} 次执行完成</span>
        <span>{current.counts.snapshot_unknown} 次无 Snapshot 评分</span>
      </div>
      <TraceTable traces={traces.data} />
    </> : <>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        {[['已归档实验', experiments.data.length], ['任务运行', traces.data.length], ['候选陈述', totalItems]].map(([label, n]) =>
          <div key={label} className="accio-panel"><p className="text-xs text-slate-500">{label}</p><p className="text-3xl font-semibold mt-2">{n}</p></div>)}
      </div>
      <div className="accio-panel overflow-x-auto"><table><thead><tr><th>实验</th><th>Provider</th><th>执行完成</th><th>Snapshot 接受 / 拒绝 / 未评分</th><th>人工标注</th></tr></thead>
        <tbody>{experiments.data.map(e => <tr key={e.id}>
          <td><Link to={`/experiments/${e.id}`}>{e.name}</Link><p className="text-xs text-slate-400 mt-1">{e.generated_at || '时间未知'}</p></td>
          <td>{e.provider || '未知'}</td><td>{e.counts.completed} / {e.case_count}</td>
          <td>{e.counts.snapshot_accepted} / {e.counts.snapshot_rejected} / {e.counts.snapshot_unknown}</td>
          <td>{e.counts.labeled} / {e.counts.items} 条</td>
        </tr>)}</tbody></table>
        {!experiments.data.length && <div className="p-8 text-slate-500">尚未导入归档。按 evals/workbench/README.md 显式选择报告、日志和 Gold 批次导入。</div>}
      </div>
      <p className="text-xs text-slate-500">运行完成、确定性检查、人工质量判定分别记录。未校准 Judge 不作为发布依据。</p>
    </>}
  </Shell>
}
