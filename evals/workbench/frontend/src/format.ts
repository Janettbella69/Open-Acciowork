import type { Trace } from './api'

export function duration(value: number | null) { return value === null ? '未知' : `${(value / 1000).toFixed(1)} s` }
export function cost(trace: Trace) { return trace.cost_usd === null ? '未知' : `$${trace.cost_usd.toFixed(4)}` }
export const execution: Record<string, string> = { completed: '已完成', failed: '失败', cancelled: '已取消', running: '运行中', queued: '排队中' }
export const quality: Record<string, string> = { no_items: '无抽样条目', unlabeled: '未标注', partial: '部分标注', labeled: '已标注' }
