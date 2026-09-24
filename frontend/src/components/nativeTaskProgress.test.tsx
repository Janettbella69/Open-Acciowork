import { render, screen } from '@testing-library/react'
import type { OphiraEvent } from '../api/types'
import NativeTaskProgress from './NativeTaskProgress'
import { nativeTaskProgress } from './nativeTasks'

function event(sequence: number, toolName: string, toolInput: object, toolResponse: unknown = {}, overrides: Partial<OphiraEvent> = {}): OphiraEvent {
  return {
    id: `run_1:${sequence}`, sequence, type: 'tool.completed', run_id: 'run_1', project_id: 'project_1',
    trace_id: 'trace_1', sdk_session_id: null, provider_profile_id: null,
    timestamp: '2026-09-23T00:00:00Z', actor: { type: 'agent', name: 'ophira', agent_id: null },
    parent_id: null, tool_use_id: `tool_${sequence}`,
    data: { tool_name: toolName, tool_input: toolInput, tool_response: toolResponse }, ...overrides,
  }
}

describe('SDK task progress', () => {
  it('replays a TodoWrite snapshot and ignores failed or unrelated calls', () => {
    const original = event(1, 'TodoWrite', { todos: [
      { content: 'Research demand', status: 'in_progress' },
      { content: 'Compare suppliers', status: 'pending' },
    ] })
    const failed = event(2, 'TodoWrite', { todos: [{ content: 'Wrong', status: 'completed' }] }, {}, { type: 'tool.failed' })
    const otherRun = event(3, 'TodoWrite', { todos: [{ content: 'Private', status: 'completed' }] }, {}, { run_id: 'run_2' })
    const branch = event(4, 'TodoWrite', { todos: [{ content: 'Branch', status: 'completed' }] }, {}, {
      actor: { type: 'subagent', name: 'branch', agent_id: 'branch_1' },
    })
    expect(nativeTaskProgress([branch, failed, original, otherRun], 'run_1')).toEqual([
      { id: 'todo-0', title: 'Research demand', status: 'in_progress' },
      { id: 'todo-1', title: 'Compare suppliers', status: 'pending' },
    ])
  })

  it('applies confirmed native task creations and updates in replay order', () => {
    const events = [
      event(3, 'TaskUpdate', { taskId: '1', status: 'completed' }),
      event(1, 'TaskCreate', { subject: 'Check market' }, { task: { id: '1', subject: 'Check market' } }),
      event(2, 'TaskCreate', { subject: 'Write report' }, { content: [{ text: '{"task":{"id":"2","subject":"Write report"}}' }] }),
      event(4, 'TaskUpdate', { taskId: '2', status: 'completed' }, { success: false }),
    ]
    expect(nativeTaskProgress(events, 'run_1')).toEqual([
      { id: '1', title: 'Check market', status: 'completed' },
      { id: '2', title: 'Write report', status: 'pending' },
    ])
    render(<NativeTaskProgress events={events} runId="run_1" />)
    expect(screen.getByRole('region', { name: 'Task progress' })).toHaveTextContent('Check market')
    expect(screen.getByText('Write report')).toBeVisible()
  })

  it('uses TaskList as the SDK truth after a resumed run', () => {
    expect(nativeTaskProgress([event(1, 'TaskList', {}, { tasks: [
      { id: '7', subject: 'Continue review', status: 'in_progress' },
    ] })], 'run_1')).toEqual([{ id: '7', title: 'Continue review', status: 'in_progress' }])
  })

  it('refreshes a task from TaskGet and ignores unsuccessful tool responses', () => {
    const events = [
      event(1, 'TaskGet', { taskId: '7' }, { task: { id: '7', subject: 'Review quotes', status: 'completed' } }),
      event(2, 'TaskUpdate', { taskId: '7', status: 'pending' }, { is_error: true }),
    ]
    expect(nativeTaskProgress(events, 'run_1')).toEqual([
      { id: '7', title: 'Review quotes', status: 'completed' },
    ])
  })
})
