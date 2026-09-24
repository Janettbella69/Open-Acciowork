import type { OphiraEvent } from '../api/types'

export type NativeTask = {
  id: string
  title: string
  status: 'pending' | 'in_progress' | 'completed'
}

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null
}

function response(value: unknown): Record<string, unknown> | null {
  const direct = record(value)
  if (!direct) return null
  if (!Array.isArray(direct.content)) return direct
  for (const block of direct.content) {
    const text = record(block)?.text
    if (typeof text !== 'string') continue
    try {
      const parsed = record(JSON.parse(text))
      if (parsed) return parsed
    } catch {
      continue
    }
  }
  return direct
}

function status(value: unknown): NativeTask['status'] | null {
  return value === 'pending' || value === 'in_progress' || value === 'completed'
    ? value : null
}

export function nativeTaskProgress(events: OphiraEvent[], runId: string): NativeTask[] {
  let tasks = new Map<string, NativeTask>()
  for (const event of [...events].sort((left, right) => left.sequence - right.sequence)) {
    if (event.run_id !== runId || event.type !== 'tool.completed' || event.actor.type !== 'agent') continue
    const input = record(event.data.tool_input)
    const output = response(event.data.tool_response)
    if (!input || output?.success === false || output?.is_error === true) continue
    switch (event.data.tool_name) {
      case 'TodoWrite': {
        if (!Array.isArray(input.todos)) break
        tasks = new Map()
        input.todos.slice(0, 100).forEach((item, index) => {
          const todo = record(item)
          const title = todo?.content
          const state = status(todo?.status)
          if (typeof title === 'string' && title.trim() && state) {
            tasks.set(`todo-${index}`, { id: `todo-${index}`, title, status: state })
          }
        })
        break
      }
      case 'TaskCreate': {
        const created = record(output?.task)
        const id = created?.id
        const title = created?.subject ?? input.subject
        if (typeof id === 'string' && typeof title === 'string' && title.trim()) {
          tasks.set(id, { id, title, status: 'pending' })
        }
        break
      }
      case 'TaskUpdate': {
        const id = input.taskId
        if (typeof id !== 'string') break
        if (input.status === 'deleted') {
          tasks.delete(id)
        } else {
          const previous = tasks.get(id)
          const title = typeof input.subject === 'string' ? input.subject : previous?.title
          const state = status(input.status) ?? previous?.status
          if (title?.trim() && state) tasks.set(id, { id, title, status: state })
        }
        break
      }
      case 'TaskGet': {
        const task = record(output?.task)
        const id = task?.id
        const title = task?.subject
        const state = status(task?.status)
        if (typeof id === 'string' && typeof title === 'string' && title.trim() && state) {
          tasks.set(id, { id, title, status: state })
        }
        break
      }
      case 'TaskList': {
        if (!Array.isArray(output?.tasks)) break
        tasks = new Map()
        output.tasks.slice(0, 100).forEach((item) => {
          const task = record(item)
          const id = task?.id
          const title = task?.subject
          const state = status(task?.status)
          if (typeof id === 'string' && typeof title === 'string' && title.trim() && state) {
            tasks.set(id, { id, title, status: state })
          }
        })
        break
      }
    }
  }
  return [...tasks.values()]
}
