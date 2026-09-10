import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import type { Trace } from './api'
import { cost, duration, execution, quality } from './format'

export function Shell({ title, subtitle, children }: { title: string; subtitle?: string; children: ReactNode }) {
  return <div className="accio space-y-5">
    <div><p className="text-xs font-semibold tracking-widest text-emerald-700 mb-2">OPEN-ACCIOWORK · 评测工作台</p>
      <h1 className="text-2xl font-semibold">{title}</h1>
      {subtitle && <p className="text-sm text-slate-500 mt-2">{subtitle}</p>}</div>
    {children}
  </div>
}
export function Pending({ error, retry }: { error: string; retry: () => void }) {
  return error ? <div role="alert" className="accio-panel text-red-700">{error} <button onClick={retry}>重试</button></div>
    : <p role="status" className="text-slate-500">正在读取归档…</p>
}
export function JsonView({ value }: { value: unknown }) {
  return <pre className="accio-json">{JSON.stringify(value, null, 2)}</pre>
}
export function Status({ value }: { value: boolean | null }) {
  return <span className={`accio-badge ${value === true ? 'bg-emerald-50 text-emerald-700' : value === false ? 'bg-red-50 text-red-700' : 'bg-slate-100 text-slate-500'}`}>
    {value === true ? '接受' : value === false ? '拒绝' : '未评分'}</span>
}
export function TraceTable({ traces }: { traces: Trace[] }) {
  return <div className="accio-panel overflow-x-auto"><table>
    <thead><tr><th>Case / 运行</th><th>实验</th><th>执行</th><th>Snapshot 检查</th><th>人工质量判定</th><th>耗时 / 成本估计</th></tr></thead>
    <tbody>{traces.map(t => <tr key={t.id}>
      <td><Link to={`/traces/${t.id}`}>{t.case_key}</Link><p className="text-xs text-slate-400 mt-1 font-mono">{t.run_id}</p></td>
      <td><Link to={`/experiments/${t.experiment_id}`}>{t.experiment_name}</Link></td>
      <td>{execution[t.execution_status || ''] || t.execution_status || '未知'}</td>
      <td><Status value={t.snapshot_accepted} />{t.issues.some(i => i.file === 'events') && <p className="text-amber-700 mt-1">日志需检查</p>}</td>
      <td>{quality[t.quality_status]}<p className="text-xs text-slate-500">{t.labeled_count} / {t.item_count} 条陈述</p></td>
      <td>{duration(t.duration_ms)}<p className="text-xs text-slate-500" title={t.cost_basis || '未记录成本口径'}>{cost(t)} · {t.cost_basis === 'sdk_anthropic_list_price' ? 'SDK 标价估计' : t.cost_basis || '口径未知'}</p></td>
    </tr>)}</tbody>
  </table>{traces.length === 0 && <p className="p-8 text-center text-slate-500">没有符合条件的运行。</p>}</div>
}
