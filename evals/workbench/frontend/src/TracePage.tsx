import { useRemote } from './useRemote'
import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { api, fileUrl } from './api'
import type { GoldItem, Judge, Review, TraceDetail } from './api'
import { JsonView, Pending, Shell, Status } from './shared'
import { cost, duration, execution } from './format'

function Markdown({ text }: { text: string }) {
  return <div className="accio-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} components={{ img: ({ alt }) => <span>{alt || '图片（归档未包含图片字节）'}</span> }}>{text}</ReactMarkdown></div>
}

function Timeline({ trace }: { trace: TraceDetail }) {
  const [kind, setKind] = useState('key')
  const [query, setQuery] = useState('')
  const [sequence, setSequence] = useState('')
  const [offset, setOffset] = useState(0)
  const remote = useRemote(useCallback(() => {
    const params = new URLSearchParams({ kind, q: query, offset: String(offset), limit: '100' })
    if (/^[1-9]\d*$/.test(sequence)) params.set('sequence', sequence)
    return api.events(trace.id, params)
  }, [trace.id, kind, query, sequence, offset]))
  return <section id="timeline" className="accio-panel space-y-4">
    <h2>执行时间线 <span className="text-sm text-slate-400 font-normal">{trace.event_count.toLocaleString()} 条原始事件</span></h2>
    <div className="flex flex-wrap gap-3">
      <select aria-label="事件类型" value={kind} onChange={e => { setKind(e.target.value); setOffset(0) }}>
        <option value="key">关键事件（折叠流式增量）</option><option value="all">全部原始事件</option>
        {Object.entries(trace.event_types).map(([name, n]) => <option key={name} value={name}>{name} ({n})</option>)}
      </select>
      <input aria-label="搜索事件" placeholder="搜索工具、输入输出、错误…" value={query} onChange={e => { setQuery(e.target.value); setOffset(0) }} />
      <input aria-label="定位 sequence" type="number" min="1" placeholder="sequence" className="w-36" value={sequence} onChange={e => { setSequence(e.target.value); setOffset(0) }} />
    </div>
    {remote.data ? <>
      <p className="text-xs text-slate-500">匹配 {remote.data.total.toLocaleString()} 条 · 依照原始 sequence 排序，展开查看完整事件与工具输入输出。</p>
      <div className="space-y-2">{remote.data.events.map((event, i) => <details key={`${event.sequence}-${i}`} id={`event-${event.sequence}`} className="accio-event">
        <summary><span className="font-mono text-xs text-slate-400 w-16 inline-block">#{event.sequence}</span>
          <span className="font-medium">{event.type}</span>
          {typeof event.data.tool_name === 'string' && <span className="ml-3 text-emerald-700">{event.data.tool_name}</span>}
          <time className="ml-3 text-xs text-slate-400">{event.timestamp}</time>
          <p className="text-xs text-slate-500 truncate mt-1 pl-16">{String(event.data.text || event.data.status || event.data.result || JSON.stringify(event.data)).slice(0, 180)}</p>
        </summary>
        <div className="mt-3 text-xs text-slate-500">事件 ID：{event.id} {event.tool_use_id && ` · tool_use_id：${event.tool_use_id}`}</div>
        <JsonView value={event} />
      </details>)}</div>
      {!remote.data.events.length && <p className="text-slate-500">没有匹配事件。文件缺失或损坏详情见归档来源。</p>}
      <div className="flex gap-3 items-center"><button disabled={offset === 0} onClick={() => setOffset(o => Math.max(0, o - 100))}>上一页</button>
        <span className="text-xs text-slate-500">第 {Math.floor(offset / 100) + 1} 页</span><button disabled={offset + 100 >= remote.data.total} onClick={() => setOffset(o => o + 100)}>下一页</button></div>
    </> : <Pending error={remote.error} retry={remote.reload} />}
  </section>
}

function ReviewForm({ trace, saved }: { trace: TraceDetail; saved: () => void }) {
  const [review, setReview] = useState<Review>(trace.review)
  const [codes, setCodes] = useState(trace.review.open_codes.join(', '))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  return <section className="accio-panel space-y-3"><h2>错误分析与开放编码</h2>
    <form className="space-y-3" onSubmit={async e => {
      e.preventDefault(); setBusy(true); setError('')
      try { await api.review(trace.id, { ...review, open_codes: codes.split(/[,，\n]/).map(s => s.trim()).filter(Boolean) }); saved() }
      catch (e) { setError((e as Error).message) } finally { setBusy(false) }
    }}>
      <div className="flex flex-wrap gap-3"><select aria-label="保存复核状态" value={review.status} onChange={e => setReview({ ...review, status: e.target.value })}>
        <option value="pending">待复核</option><option value="reviewed">已复核</option><option value="flagged">需跟进</option>
      </select><input aria-label="开放编码" placeholder="开放编码，逗号分隔" className="flex-1" value={codes} onChange={e => setCodes(e.target.value)} /></div>
      <textarea aria-label="复核记录" placeholder="失败归因、证据位置、后续检查…" value={review.notes} onChange={e => setReview({ ...review, notes: e.target.value })} rows={3} />
      {error && <p role="alert" className="text-red-700">{error}</p>}<button disabled={busy} type="submit">{busy ? '保存中…' : '保存复核记录'}</button>
      <p className="text-xs text-slate-500">开放编码记录在本条 Trace 上，逐陈述 Gold 标签由下方表单单独保存。</p>
    </form>
  </section>
}

function sourceAnchor(item: GoldItem) {
  if (item.source?.startsWith('artifact:')) return '#artifact-' + encodeURIComponent(item.source.slice(9))
  if (item.source?.startsWith('ledger:')) return '#evidence-' + encodeURIComponent(item.source.slice(7))
  return '#final-result'
}

function LabelForm({ item, judge, owner, saved }: { item: GoldItem; judge: Judge; owner: boolean; saved: () => void }) {
  const [label, setLabel] = useState<string>(item.annotation ? String(item.annotation.label) : '')
  const [rationale, setRationale] = useState(item.annotation?.rationale || '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  return <details id={`gold-${item.item_id}`} className="accio-event" open={window.location.hash === `#gold-${item.item_id}`}>
    <summary><span className="accio-badge bg-slate-100 text-slate-600 mr-2">{item.annotation ? judge[item.annotation.label ? 'positive' : 'negative'] : '未标注'}</span>{item.item}
      <p className="text-xs text-slate-400 mt-2">{item.judge} · {item.split} · {item.item_id}</p></summary>
    <form className="space-y-3 mt-4" onSubmit={async e => {
      e.preventDefault(); setBusy(true); setError('')
      try { await api.label(item.item_id, { label: label === 'true', rationale, revision: item.revision, owner_confirmed: owner }); saved() }
      catch (e) { setError((e as Error).message) } finally { setBusy(false) }
    }}>
      <p className="font-medium">{judge.question}</p>
      <p className="text-xs text-slate-500">来源：<a href={sourceAnchor(item)}>{item.source}</a> · 批次：{item.batch}</p>
      {judge.positive_is_failure && <p className="text-sm text-amber-700">此维度的 label=true 表示“存在混淆”。</p>}
      <div className="flex gap-5">{(['true', 'false'] as const).map(v => <label key={v} className="text-sm flex gap-2 items-center">
        <input type="radio" name={`label-${item.item_id}`} value={v} checked={label === v} onChange={e => setLabel(e.target.value)} />{judge[v === 'true' ? 'positive' : 'negative']}
      </label>)}</div>
      <textarea aria-label={`标注理由 ${item.item_id}`} placeholder="写一句判定理由，并指向证据或原文" required value={rationale} onChange={e => setRationale(e.target.value)} rows={2} />
      {error && <p role="alert" className="text-red-700">{error}</p>}<button disabled={!owner || !label || !rationale.trim() || busy} type="submit">{busy ? '保存中…' : '保存人工标签'}</button>
    </form>
  </details>
}

export default function TracePage() {
  const { id = '' } = useParams()
  const remote = useRemote(useCallback(() => api.trace(id), [id]))
  const queue = useRemote(api.queue)
  const [owner, setOwner] = useState(false)
  const [notice, setNotice] = useState('')
  const [selectedPath, setSelectedPath] = useState('')
  useEffect(() => {
    if (remote.data && window.location.hash) {
      document.getElementById(window.location.hash.slice(1))?.scrollIntoView()
    }
  }, [remote.data, queue.data])
  const trace = remote.data
  const judges = queue.data?.judges
  if (!trace) return <Shell title="Trace"><Pending error={remote.error} retry={remote.reload} /></Shell>
  const saved = () => { setNotice('已保存，刷新后可恢复。'); remote.reload() }
  let selectedValue: unknown = trace.snapshot
  for (const key of selectedPath.replace(/\[(\d+)\]/g, '.$1').split('.').filter(Boolean)) {
    selectedValue = selectedValue && typeof selectedValue === 'object' ? (selectedValue as Record<string, unknown>)[key] : undefined
  }
  return <Shell title={trace.case_key} subtitle={`${trace.run_id} · ${trace.trace_id || 'trace_id 未记录'}`}>
    <div className="flex flex-wrap gap-3 text-sm"><Link to={`/experiments/${trace.experiment_id}`}>← {trace.experiment_name}</Link>
      {[['timeline', '执行时间线'], ['evidence', '证据与产物'], ['grading', '评分依据'], ['gold', '人工复核'], ['sources', '归档来源']].map(([anchor, label]) => <a key={anchor} href={`#${anchor}`}>{label}</a>)}
      <Link to={`/compare?left=${trace.experiment_id}`}>实验对比</Link></div>
    {notice && <p role="status" className="text-emerald-700">{notice}</p>}
    <div className="accio-panel grid sm:grid-cols-4 gap-4 text-sm">
      <div><p className="text-xs text-slate-500 mb-2">执行状态</p>{execution[trace.execution_status || ''] || trace.execution_status || '未知'}</div>
      <div><p className="text-xs text-slate-500 mb-2">Snapshot 确定性检查</p><Status value={trace.snapshot_accepted} /></div>
      <div><p className="text-xs text-slate-500 mb-2">人工标注</p>{trace.labeled_count} / {trace.item_count} 条陈述</div>
      <div><p className="text-xs text-slate-500 mb-2">耗时 / 成本估计</p>{duration(trace.duration_ms)} · {cost(trace)}<p className="text-xs text-slate-400">{trace.cost_basis || '成本口径未记录'}</p></div>
    </div>
    <section className="accio-panel"><h2>任务输入</h2><p className="whitespace-pre-wrap text-sm mt-4">{trace.prompt || '归档未保存任务输入'}</p></section>
    <section id="final-result" className="accio-panel"><h2>最终结果</h2><p className="text-xs text-slate-400 my-3">来源：{trace.final_text_source === 'events' ? 'run.result / 可观察消息事件' : trace.final_text_source === 'gold_batch' ? 'Gold 抽样档案' : '未记录'}</p>
      {trace.final_text ? <Markdown text={trace.final_text} /> : <p className="text-slate-500">未记录最终文本。</p>}</section>
    <Timeline trace={trace} />
    <section id="evidence" className="accio-panel space-y-3"><h2>证据与产物</h2>
      <p className="text-xs text-slate-500">{trace.evidence_source === 'snapshot' ? '来源：该次评分的冻结 Snapshot。' : trace.evidence_source === 'gold_batch' ? '来源：Gold 抽样时保存的证据与产物，采集时间见批次原件。此运行没有冻结的评分 Snapshot。' : '归档中没有证据或产物快照。'}</p>
      {trace.evidence.map((e, i) => <details className="accio-event" id={`evidence-${encodeURIComponent(String(e.id))}`} key={i}><summary><span className="accio-badge bg-blue-50 text-blue-700 mr-2">{String(e.kind || '类型未知')}</span>{String(e.claim || e.id || '证据')}</summary><JsonView value={e} /></details>)}
      {trace.artifacts.map((a, i) => <details className="accio-event" id={`artifact-${encodeURIComponent(String(a.name))}`} key={i}><summary>产物 · {String(a.name || a.id || '未命名')}</summary>
        {typeof a.text === 'string' && a.text ? <div className="mt-4"><Markdown text={a.text} /></div> : <p className="text-slate-500 mt-3">归档没有保存可预览正文。</p>}<details><summary className="text-xs text-slate-400 mt-3">产物元数据</summary><JsonView value={a} /></details>
      </details>)}
    </section>
    <section id="grading" className="accio-panel space-y-3"><h2>评分依据</h2>
      <p className="text-sm">旧报告执行判定：{trace.legacy_passed === null ? '未知' : trace.legacy_passed ? '通过' : '未通过'}。Snapshot 检查：{trace.snapshot_accepted === null ? '未评分' : trace.snapshot_accepted ? '接受' : '拒绝'}。</p>
      {trace.snapshot_eval.findings?.map((finding, i) => <div className="p-3 bg-amber-50 rounded-lg text-sm" key={i}><strong>{finding.code}</strong> · {finding.message}
        {finding.path && <a href="#snapshot" className="block text-xs mt-2" onClick={() => setSelectedPath(finding.path!)}>{finding.path} → 查看冻结字段</a>}</div>)}
      <details><summary>原始结果与确定性评分记录</summary><JsonView value={{ result: trace.result, snapshot_eval: trace.snapshot_eval, summary: trace.summary }} /></details>
      <details><summary>模型、Skill 快照与可比性字段</summary><JsonView value={{ versions: trace.versions, skills: trace.skill_snapshot, usage: trace.usage }} /></details>
      <details id="snapshot" open={!!selectedPath}><summary>评分 Snapshot{selectedPath && ` · ${selectedPath}`}</summary>
        <JsonView value={selectedValue === undefined ? { missing_path: selectedPath } : selectedValue} /></details>
    </section>
    <ReviewForm key={`${trace.id}-${trace.review.revision}`} trace={trace} saved={saved} />
    <section id="gold" className="accio-panel space-y-4"><h2>逐陈述 Gold 标注</h2>
      <label className="flex gap-2 items-center text-sm"><input type="checkbox" checked={owner} onChange={e => setOwner(e.target.checked)} />我是项目负责人，本次标签由我本人判定。</label>
      {judges ? trace.items.map(item => <LabelForm key={`${item.item_id}-${item.revision}`} item={item} judge={judges[item.judge]} owner={owner} saved={saved} />) : <Pending error={queue.error} retry={queue.reload} />}
      {!trace.items.length && <p className="text-slate-500 text-sm">该运行没有导入候选陈述，质量状态保持未知。</p>}
    </section>
    <section id="sources" className="accio-panel space-y-3"><h2>归档来源与完整性</h2>
      <p className="text-xs text-slate-500">导入副本保留原始字节与 SHA-256。下载读取评测库中的副本。</p>
      {Object.entries(trace.sources).map(([name, ref]) => <details className="accio-event" key={name}><summary>{name} <span className={ref.status === 'available' ? 'text-emerald-700' : 'text-amber-700'}>· {ref.status}</span>{ref.sequence_warning && <span className="text-amber-700"> · 序号不连续</span>}</summary>
        <JsonView value={ref} />{ref.sha256 && <a className="text-sm" href={fileUrl(ref.sha256)}>下载原始归档副本 ↓</a>}</details>)}
    </section>
  </Shell>
}
