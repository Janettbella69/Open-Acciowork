import { useRemote } from './useRemote'
import { useState } from 'react'
import { api } from './api'
import { Pending, Shell, TraceTable } from './shared'

export default function TracesPage() {
  const remote = useRemote(api.traces)
  const [query, setQuery] = useState('')
  const [status, setStatus] = useState('all')
  const [review, setReview] = useState('all')
  const filtered = remote.data?.filter(t =>
    `${t.case_key} ${t.run_id} ${t.trace_id || ''} ${t.experiment_name} ${t.review.open_codes.join(' ')}`.toLowerCase().includes(query.toLowerCase())
    && (status === 'all' || (status === 'unknown' ? t.snapshot_accepted === null : t.execution_status === status))
    && (review === 'all' || t.review.status === review))
  return <Shell title="Trace" subtitle="搜索任务、实验、run_id、trace_id 或开放编码。">
    <div className="flex gap-3 flex-wrap">
      <input aria-label="搜索 Trace" placeholder="搜索任务、运行 ID、开放编码…" className="flex-1 min-w-48" value={query} onChange={e => setQuery(e.target.value)} />
      <select aria-label="执行状态" value={status} onChange={e => setStatus(e.target.value)}>
        <option value="all">全部执行状态</option><option value="completed">已完成</option><option value="failed">失败</option><option value="cancelled">已取消</option><option value="unknown">无 Snapshot 评分</option>
      </select>
      <select aria-label="复核状态" value={review} onChange={e => setReview(e.target.value)}>
        <option value="all">全部复核状态</option><option value="pending">待复核</option><option value="reviewed">已复核</option><option value="flagged">需跟进</option>
      </select>
    </div>
    {filtered ? <TraceTable traces={filtered} /> : <Pending error={remote.error} retry={remote.reload} />}
  </Shell>
}
