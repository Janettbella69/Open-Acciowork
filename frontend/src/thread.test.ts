import type { RunEvent } from './api'
import { buildTurn, describeStep } from './thread'

let seq = 0
const sdk = (type: string, data: Record<string, unknown>): RunEvent => ({
  seq: ++seq, at: '2026-11-26T00:00:00Z', source: 'sdk', type, data,
})
const host = (type: string, data: Record<string, unknown>): RunEvent => ({
  seq: ++seq, at: '2026-11-26T00:00:00Z', source: 'host', type, data,
})

test('text, folded steps with results, and the end status', () => {
  const view = buildTurn([
    host('run.started', { prompt: 'x' }),
    sdk('AssistantMessage', { content: [
      { type: 'TextBlock', text: '先查一下' },
      { type: 'ToolUseBlock', id: 't1', name: 'WebFetch', input: { url: 'https://example.com' } },
      { type: 'ToolUseBlock', id: 't2', name: 'Bash', input: { command: 'python calc.py', description: '算落地成本' } },
    ] }),
    sdk('UserMessage', { content: [
      { type: 'ToolResultBlock', tool_use_id: 't1', content: [{ type: 'text', text: 'page' }] },
      { type: 'ToolResultBlock', tool_use_id: 't2', content: 'boom', is_error: true },
    ] }),
    sdk('AssistantMessage', { content: [{ type: 'TextBlock', text: '结论：可以做' }] }),
    sdk('ResultMessage', { total_cost_usd: 0.12, is_error: false }),
    host('run.finished', { status: 'completed', error: null }),
  ])
  expect(view.items.map((item) => item.kind)).toEqual(['text', 'steps', 'text'])
  const steps = view.items[1].kind === 'steps' ? view.items[1].steps : []
  expect(steps.map((s) => [s.tool, s.result, s.isError])).toEqual([
    ['WebFetch', 'page', false],
    ['Bash', 'boom', true],
  ])
  expect(describeStep(steps[1])).toBe('算落地成本')
  expect(describeStep({ id: 'w', tool: 'Write', input: { file_path: '/srv/data/projects/prj_1/workspace/plan.md' } }))
    .toBe('plan.md')
  expect(describeStep(steps[0])).toBe('https://example.com')
  expect(view.status).toBe('completed')
  expect(view.costUsd).toBe(0.12)
})

test('a request appears where it was asked and records the answer', () => {
  const view = buildTurn([
    host('permission.requested', { request_id: 'req_1', tool_name: 'Bash', tool_input: { command: 'rm a' } }),
    host('permission.resolved', { request_id: 'req_1', allow: false, message: '先别删' }),
  ])
  expect(view.items).toEqual([{ kind: 'request', request: {
    requestId: 'req_1', tool: 'Bash', input: { command: 'rm a' },
    resolved: { allow: false, message: '先别删', answers: undefined },
  } }])
  expect(view.status).toBe('running')
})

test('subtask internals stay inside their step', () => {
  const view = buildTurn([
    sdk('AssistantMessage', { parent_tool_use_id: 'agent-1', content: [{ type: 'TextBlock', text: 'inner' }] }),
  ], 'completed')
  expect(view.items).toEqual([])
  expect(view.status).toBe('completed')
})
