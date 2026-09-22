import { useRemote } from './useRemote'
import { useCallback, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api } from './api'
import { JsonView, Pending, Shell, Status } from './shared'

const changes: Record<string, string> = { added: '新增 Case', removed: '移除 Case', multiple_trials: '多次运行，需逐条复核', unknown: '可比性不足', improved: '检查改善', regressed: '检查退化', unchanged: '检查不变' }
const fields: Record<string, string> = { task_prompt_hash: '任务输入', fixture_hash: 'Fixture 版本', grader_version: '评分器版本', grading_profile: '评分 Profile', snapshot_schema: 'Snapshot schema', model: '模型', skill_hash: 'Skill 快照' }
export default function ComparePage() {
  const [params, setParams] = useSearchParams()
  const experiments = useRemote(api.experiments)
  const left = params.get('left') || '', right = params.get('right') || ''
  const [onlyChanges, setOnlyChanges] = useState(false)
  const comparison = useRemote(useCallback(() => left && right ? api.compare(left, right) : Promise.resolve(null), [left, right]))
  return <Shell title="实验对比" subtitle="按相同 Case 检查输入、Fixture、评分器与运行配置。缺失历史版本时只供人工对照。">
    {experiments.data ? <div className="flex gap-4 flex-wrap items-center">
      {(['left', 'right'] as const).map(side => <label key={side} className="text-sm">{side === 'left' ? '基线实验 ' : '对照实验 '}
        <select aria-label={side === 'left' ? '基线实验' : '对照实验'} value={side === 'left' ? left : right} onChange={e => { const next = new URLSearchParams(params); next.set(side, e.target.value); setParams(next) }}>
          <option value="">选择实验</option>{experiments.data?.map(e => <option key={e.id} value={e.id}>{e.name}</option>)}
        </select></label>)}
      <label className="text-sm"><input type="checkbox" checked={onlyChanges} onChange={e => setOnlyChanges(e.target.checked)} /> 只看新增、移除与检查变化</label>
    </div> : <Pending error={experiments.error} retry={experiments.reload} />}
    {!left || !right ? <div className="accio-panel text-slate-500">选择两个实验，逐 Case 打开两侧 Trace。</div>
      : comparison.data ? <>{comparison.data.cases.filter(row => !onlyChanges || ['added', 'removed', 'improved', 'regressed'].includes(row.change)).map(row => <div className="accio-panel" key={row.case_key}>
        <div className="flex justify-between gap-4 mb-4"><h2 className="font-semibold">{row.case_key}</h2><span className="accio-badge bg-amber-50 text-amber-800">{changes[row.change]}</span></div>
        <div className="grid sm:grid-cols-2 gap-6 mb-4">{[row.left, row.right].map((side, i) => <div key={i}><p className="text-xs text-slate-500 mb-2">{i === 0 ? '基线' : '对照'}</p>
          {side.length ? side.map(t => <p key={t.id} className="text-sm mb-2"><Link to={`/traces/${t.id}`}>{t.run_id}</Link> <Status value={t.snapshot_accepted} /></p>) : <p className="text-slate-400">该实验没有此 Case</p>}
        </div>)}</div>
        {row.versions.length > 0 && <div className="flex flex-wrap gap-3">{row.versions.map(v => <details key={v.field} className="text-xs"><summary className={v.state === 'same' ? 'text-emerald-700' : 'text-amber-700'}>{fields[v.field]}：{v.state === 'same' ? '一致' : v.state === 'changed' ? '变化' : '版本未知'}</summary><JsonView value={{ 基线: v.left, 对照: v.right }} /></details>)}</div>}
      </div>)}{onlyChanges && !comparison.data.cases.some(r => ['added', 'removed', 'improved', 'regressed'].includes(r.change)) && <p className="accio-panel text-slate-500">没有可确认的变化。关闭筛选可查看版本缺失的 Case。</p>}</>
      : <Pending error={comparison.error} retry={comparison.reload} />}
  </Shell>
}
