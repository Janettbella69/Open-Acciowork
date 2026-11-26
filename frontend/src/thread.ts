// Turn a turn's event log (SDK messages as sent, plus host events) into what the thread shows.
import type { Run, RunEvent } from './api'

export type Step = {
  id: string
  tool: string
  input: Record<string, unknown>
  result?: string
  isError?: boolean
}

export type Question = {
  question: string
  header?: string
  multiSelect?: boolean
  options: { label: string; description?: string }[]
}

export type Request = {
  requestId: string
  tool: string
  input: Record<string, unknown>
  resolved?: { allow: boolean; message?: string; answers?: Record<string, unknown> }
}

export type Item =
  | { kind: 'text'; text: string }
  | { kind: 'thinking'; text: string }
  | { kind: 'steps'; steps: Step[] }
  | { kind: 'request'; request: Request }

export type TurnView = {
  items: Item[]
  status: Run['status']
  error: string | null
  costUsd: number | null
}

type Block = { type?: string; [key: string]: unknown }

function resultText(content: unknown): string {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content
      .map((part) => (part && typeof part === 'object' && 'text' in part ? String(part.text) : ''))
      .join('\n')
  }
  return content == null ? '' : JSON.stringify(content)
}

export function buildTurn(events: RunEvent[], fallback: Run['status'] = 'running'): TurnView {
  const view: TurnView = { items: [], status: fallback, error: null, costUsd: null }
  const steps = new Map<string, Step>()
  const requests = new Map<string, Request>()

  for (const event of events) {
    const data = event.data
    if (event.source === 'sdk' && event.type === 'AssistantMessage') {
      if (data.parent_tool_use_id) continue // inside a delegated subtask; its outcome comes back as a step result
      for (const block of (data.content as Block[] | undefined) ?? []) {
        if (block.type === 'TextBlock' && String(block.text ?? '').trim()) {
          view.items.push({ kind: 'text', text: String(block.text) })
        } else if (block.type === 'ThinkingBlock' && String(block.thinking ?? '').trim()) {
          view.items.push({ kind: 'thinking', text: String(block.thinking) })
        } else if (block.type === 'ToolUseBlock') {
          const step: Step = {
            id: String(block.id),
            tool: String(block.name),
            input: (block.input as Record<string, unknown>) ?? {},
          }
          steps.set(step.id, step)
          const last = view.items.at(-1)
          if (last?.kind === 'steps') last.steps.push(step)
          else view.items.push({ kind: 'steps', steps: [step] })
        }
      }
    } else if (event.source === 'sdk' && event.type === 'UserMessage') {
      for (const block of Array.isArray(data.content) ? (data.content as Block[]) : []) {
        if (block.type !== 'ToolResultBlock') continue
        const step = steps.get(String(block.tool_use_id))
        if (step) {
          step.result = resultText(block.content)
          step.isError = Boolean(block.is_error)
        }
      }
    } else if (event.source === 'sdk' && event.type === 'ResultMessage') {
      if (typeof data.total_cost_usd === 'number') view.costUsd = data.total_cost_usd
    } else if (event.type === 'permission.requested') {
      const request: Request = {
        requestId: String(data.request_id),
        tool: String(data.tool_name),
        input: (data.tool_input as Record<string, unknown>) ?? {},
      }
      requests.set(request.requestId, request)
      view.items.push({ kind: 'request', request })
    } else if (event.type === 'permission.resolved') {
      const request = requests.get(String(data.request_id))
      if (request) {
        request.resolved = {
          allow: Boolean(data.allow),
          message: data.message as string | undefined,
          answers: data.answers as Record<string, unknown> | undefined,
        }
      }
    } else if (event.type === 'run.finished') {
      view.status = data.status as Run['status']
      view.error = (data.error as string | null) ?? null
    }
  }
  return view
}

/** A short, plain description of what a step did. Files are named, not located: the
 * server's folder layout means nothing to the person and is not theirs to see. */
export function describeStep(step: Step): string {
  const input = step.input
  if (step.tool === 'Bash') return String(input.description ?? input.command ?? '')
  if (step.tool === 'Skill') return String(input.skill ?? input.command ?? '')
  const file = input.file_path ?? input.notebook_path ?? input.path
  if (typeof file === 'string') return file.split('/').filter(Boolean).at(-1) ?? file
  const target = input.url ?? input.query ?? input.pattern
  return target == null ? '' : String(target)
}
