import { useRemote } from './useRemote'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api, exportUrl } from './api'
import { Pending, Shell } from './shared'

export default function ReviewPage() {
  const remote = useRemote(api.queue)
  const [judge, setJudge] = useState('all')
  const [state, setState] = useState('unlabeled')
  const [split, setSplit] = useState('all')
  const [query, setQuery] = useState('')
  const items = remote.data?.items.filter(i => (judge === 'all' || i.judge === judge)
    && (split === 'all' || i.split === split)
    && (state === 'all' || (state === 'unlabeled' ? !i.annotation : !!i.annotation))
    && `${i.item} ${i.run_id}`.toLowerCase().includes(query.toLowerCase()))
  return <Shell title="人工复核" subtitle="保留每条陈述的来源与 train / dev / test 划分。Gold 判定由项目负责人本人填写。">
    {remote.data ? <>
      <div className="flex gap-3 flex-wrap">
        <input aria-label="搜索陈述" placeholder="搜索陈述或运行 ID" value={query} onChange={e => setQuery(e.target.value)} />
        <select aria-label="判定维度" value={judge} onChange={e => setJudge(e.target.value)}><option value="all">全部判定维度</option>{Object.keys(remote.data.judges).map(j => <option key={j}>{j}</option>)}</select>
        <select aria-label="标注状态" value={state} onChange={e => setState(e.target.value)}><option value="unlabeled">未标注</option><option value="labeled">已标注</option><option value="all">全部标注状态</option></select>
        <select aria-label="样本划分" value={split} onChange={e => setSplit(e.target.value)}><option value="all">全部划分</option>{['train', 'dev', 'test'].map(s => <option key={s}>{s}</option>)}</select>
      </div>
      <p className="text-sm text-slate-500">{items?.length} 条陈述 · 已保存 {remote.data.items.filter(i => i.annotation).length} / {remote.data.items.length} 条</p>
      <div className="accio-panel overflow-x-auto"><table><thead><tr><th>待判定陈述</th><th>判定维度 / 划分</th><th>人工标签</th><th>来源</th></tr></thead><tbody>
        {items?.map(item => <tr key={item.item_id}>
          <td className="max-w-xl"><Link to={`/traces/${item.trace_ids?.[0]}#gold-${item.item_id}`}>{item.item}</Link><p className="text-xs text-slate-400 mt-1">{item.run_id}</p></td>
          <td>{item.judge}<p className="text-xs text-slate-500">{item.split}</p></td>
          <td>{item.annotation ? remote.data?.judges[item.judge][item.annotation.label ? 'positive' : 'negative'] : '未标注'}</td><td className="text-xs break-all">{item.source}</td>
        </tr>)}
      </tbody></table>{!items?.length && <p className="p-8 text-slate-500">没有符合筛选条件的陈述。</p>}</div>
      <div className="accio-panel text-sm"><p className="font-medium mb-3">导出已保存的人工标签（原 Gold JSONL 字段）</p><div className="flex flex-wrap gap-4">
        {Object.keys(remote.data.judges).map(j => <a key={j} href={exportUrl(j)}>{j} ↓</a>)}
      </div><p className="text-xs text-slate-500 mt-3">标签保存在独立评测库。导出文件保留 item_id、judge、split、batch、理由和标注时间。</p></div>
    </> : <Pending error={remote.error} retry={remote.reload} />}
  </Shell>
}
