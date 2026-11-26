import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { RunEvent } from '../api'
import Workspace from './Workspace'

type Call = { method: string; url: string; body?: unknown }

class FakeEventSource {
  static instances: FakeEventSource[] = []
  static readonly CLOSED = 2
  onmessage: ((message: { data: string }) => void) | null = null
  onerror: (() => void) | null = null
  readyState = 1
  constructor(readonly url: string) {
    FakeEventSource.instances.push(this)
  }
  emit(event: Omit<RunEvent, 'at'>) {
    act(() => this.onmessage?.({ data: JSON.stringify({ at: 'now', ...event }) }))
  }
  close() {
    this.readyState = FakeEventSource.CLOSED
  }
}

const project = { id: 'prj_1', name: '宠物用品', created_at: 'now', session_id: null }
const run = {
  id: 'run_1', project_id: 'prj_1', prompt: '我想卖宠物用品', status: 'running',
  created_at: 'now', finished_at: null, error: null,
}
let calls: Call[]
let projects: unknown[]

function respond(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status }))
}

beforeEach(() => {
  calls = []
  projects = []
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  vi.stubGlobal('fetch', (url: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    const body = typeof init?.body === 'string' ? JSON.parse(init.body) : undefined
    calls.push({ method, url, body })
    if (url === '/api/projects' && method === 'GET') return respond(projects)
    if (url === '/api/projects' && method === 'POST') return respond(project, 201)
    if (url === '/api/projects/prj_1/runs') return respond(run, 201)
    if (url === '/api/projects/prj_1') return respond({ ...project, runs: [run], active_run_id: null })
    if (url === '/api/projects/prj_1/files') return respond([{ path: 'plan.md', size: 3, modified_at: 'now' }])
    if (url.startsWith('/api/runs/')) return respond({ ok: true })
    if (url.endsWith('/feedback')) return respond({ at: 'now' }, 201)
    return respond({ detail: 'unexpected' }, 500)
  })
  window.history.replaceState(null, '', '/app')
})

afterEach(() => vi.unstubAllGlobals())

async function startFirstTurn() {
  render(<Workspace />)
  const box = await screen.findByRole('textbox', { name: 'Message' })
  fireEvent.change(box, { target: { value: '我想卖宠物用品' } })
  fireEvent.keyDown(box, { key: 'Enter' })
  await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
  return FakeEventSource.instances[0]
}

test('the first message creates a project, starts a turn and streams the reply', async () => {
  const stream = await startFirstTurn()
  expect(calls.filter((c) => c.method === 'POST').map((c) => [c.url, c.body])).toEqual([
    ['/api/projects', { name: '我想卖宠物用品' }],
    ['/api/projects/prj_1/runs', { prompt: '我想卖宠物用品' }],
  ])
  expect(stream.url).toBe('/api/runs/run_1/events')
  stream.emit({ seq: 1, source: 'host', type: 'run.started', data: {} })
  stream.emit({ seq: 2, source: 'sdk', type: 'AssistantMessage', data: { content: [
    { type: 'ToolUseBlock', id: 't1', name: 'WebSearch', input: { query: 'pet supplies US' } },
  ] } })
  stream.emit({ seq: 3, source: 'sdk', type: 'AssistantMessage', data: { content: [
    { type: 'TextBlock', text: '美国宠物用品市场**很大**' },
  ] } })
  expect(screen.getByText('Working…')).toBeInTheDocument()
  expect(screen.getByText('Steps (1)')).toBeInTheDocument()
  expect(screen.getByText('很大').tagName).toBe('STRONG')
  stream.emit({ seq: 4, source: 'host', type: 'run.finished', data: { status: 'completed', error: null } })
  expect(screen.queryByText('Working…')).not.toBeInTheDocument()
  await waitFor(() => expect(calls.some((c) => c.url === '/api/projects/prj_1')).toBe(true))
})

test('a risky action waits for the person; their answer is sent back', async () => {
  const stream = await startFirstTurn()
  stream.emit({ seq: 1, source: 'host', type: 'permission.requested', data: {
    request_id: 'req_1', tool_name: 'Bash', tool_input: { command: 'rm old.csv', description: 'Delete the old sheet' },
  } })
  expect(screen.getByText('Needs your OK')).toBeInTheDocument()
  expect(screen.getByText('rm old.csv')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Deny' }))
  await waitFor(() => expect(calls.at(-1)).toEqual({
    method: 'POST', url: '/api/runs/run_1/permissions/req_1', body: { allow: false },
  }))
  stream.emit({ seq: 2, source: 'host', type: 'permission.resolved', data: { request_id: 'req_1', allow: false } })
  expect(screen.getByText(/Denied/)).toBeInTheDocument()
})

test('a clarifying question is answered by picking an option', async () => {
  const stream = await startFirstTurn()
  stream.emit({ seq: 1, source: 'host', type: 'permission.requested', data: {
    request_id: 'req_2', tool_name: 'AskUserQuestion', tool_input: { questions: [{
      question: '目标市场先选哪个？', header: '市场', multiSelect: false,
      options: [{ label: '美国', description: '市场大' }, { label: '欧洲', description: '合规多' }],
    }] },
  } })
  const submit = screen.getByRole('button', { name: 'Submit answer' })
  expect(submit).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: /美国/ }))
  fireEvent.click(submit)
  await waitFor(() => expect(calls.at(-1)?.body).toEqual({
    allow: true, answers: { '目标市场先选哪个？': '美国' },
  }))
})

test('Enter while picking characters in an input method does not send', async () => {
  render(<Workspace />)
  const box = await screen.findByRole('textbox', { name: 'Message' })
  fireEvent.change(box, { target: { value: 'chong' } })
  fireEvent.keyDown(box, { key: 'Enter', isComposing: true })
  fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
  await new Promise((resolve) => setTimeout(resolve, 20))
  expect(calls.filter((c) => c.method === 'POST')).toEqual([])
})

test('an idea typed on the landing page starts the first turn on arrival', async () => {
  window.history.replaceState(null, '', '/app?brief=%E5%8D%96%E7%8C%AB%E7%A0%82%E5%88%B0%E5%BE%B7%E5%9B%BD')
  render(<Workspace />)
  await waitFor(() => expect(calls.some((c) => c.url === '/api/projects/prj_1/runs')).toBe(true))
  expect(calls.find((c) => c.url === '/api/projects/prj_1/runs')?.body).toEqual({ prompt: '卖猫砂到德国' })
  expect(window.location.search).toBe('?project=prj_1')
})

test('returning opens the most recent project with its history and files', async () => {
  projects = [project]
  render(<Workspace />)
  await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
  expect(await screen.findByRole('heading', { name: '宠物用品' })).toBeInTheDocument()
  expect(screen.getByText('我想卖宠物用品')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: /Files/ }))
  expect(screen.getByRole('link', { name: 'plan.md' })).toHaveAttribute('href', '/api/projects/prj_1/files/plan.md')
})

test('feedback on a finished turn is sent with its run', async () => {
  const stream = await startFirstTurn()
  stream.emit({ seq: 1, source: 'host', type: 'run.finished', data: { status: 'completed', error: null } })
  fireEvent.click(screen.getByRole('button', { name: 'Something wrong?' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'What is wrong?' }), { target: { value: '运费算错了' } })
  fireEvent.click(screen.getByRole('button', { name: 'Send feedback' }))
  await waitFor(() => expect(screen.getByText('Thanks, noted.')).toBeInTheDocument())
  expect(calls.find((c) => c.url.endsWith('/feedback'))?.body).toEqual({ text: '运费算错了', run_id: 'run_1' })
})

test('a 401 from the site gate is explained, not shown as an outage', async () => {
  vi.stubGlobal('fetch', () => respond({ detail: 'Unauthorized' }, 401))
  render(<Workspace />)
  expect(await screen.findByRole('alert')).toHaveTextContent('The workspace needs you to sign in')
})
