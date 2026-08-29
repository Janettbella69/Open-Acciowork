import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { vi } from 'vitest'
import type { RunRecord } from '../api/types'
import { I18nContext } from '../i18n'
import WorkspaceComposer from './WorkspaceComposer'

const completedRun: RunRecord = {
  id: 'run_1',
  trace_id: 'trace_1',
  project_id: 'project_1',
  prompt: 'Review this market',
  agent_id: null,
  mode: 'standard',
  provider_profile_id: null,
  provider_snapshot: null,
  skill_snapshot: [],
  status: 'completed',
  sdk_session_id: 'session_1',
  error: null,
  error_code: null,
  created_at: '2026-08-05T10:00:00Z',
  started_at: '2026-08-05T10:00:00Z',
  finished_at: '2026-08-05T10:01:00Z',
}

function baseProps() {
  return {
    value: '',
    attachedFiles: [] as File[],
    busy: false,
    notice: '',
    error: '',
    projects: [],
    project: null,
    run: null,
    events: [],
    assistantText: '',
    reasoningSummary: '',
    permissions: [],
    connection: 'idle',
    historyLoading: false,
    isActive: false,
    onChange: vi.fn(),
    onAttachedFilesChange: vi.fn(),
    onSubmit: vi.fn(),
    onProjectRefresh: vi.fn(),
    onNewResearch: vi.fn(),
    onSelectProject: vi.fn(),
    onCancel: vi.fn(),
    onDecision: vi.fn(),
  }
}

function StatefulComposer() {
  const [value, setValue] = useState('')
  const [attachedFiles, setAttachedFiles] = useState<File[]>([])
  return (
    <WorkspaceComposer
      {...baseProps()}
      value={value}
      attachedFiles={attachedFiles}
      onChange={setValue}
      onAttachedFilesChange={setAttachedFiles}
    />
  )
}

describe('WorkspaceComposer', () => {
  it('starts the workspace from one primary conversation box', () => {
    const props = baseProps()
    render(<WorkspaceComposer {...props} />)

    expect(screen.getByRole('heading', { level: 1, name: /what can i do for you/i })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: /ask trademind/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /send/i })).toBeDisabled()
    expect(screen.queryByText('For you')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /browse all/i })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 2, name: 'Recents' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /^agents$/i })).toHaveAttribute('href', '/app/agents')
    expect(screen.getByRole('link', { name: /^plugins$/i })).toHaveAttribute('href', '/app/plugins')
    expect(screen.queryByRole('link', { name: /^connectors$/i })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /mobile/i })).toHaveAttribute('href', '/app/mobile')
    expect(screen.getByRole('button', { name: /new task/i })).toBeInTheDocument()
    expect(screen.queryByText('Workspace')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /agent team/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: /run mode/i })).not.toBeInTheDocument()

    expect(screen.getByRole('region', { name: /example opportunities/i })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /find what sells|source with confidence|localize to convert|launch and grow/i })).toHaveLength(4)
    expect(props.onChange).not.toHaveBeenCalled()
  })

  it('keeps add files and skills as separate composer controls', () => {
    render(<StatefulComposer />)

    const attachment = new File(['supplier notes'], 'supplier-notes.pdf', { type: 'application/pdf' })
    fireEvent.change(screen.getByLabelText(/choose files or images/i), { target: { files: [attachment] } })
    expect(screen.getByLabelText(/locally staged attachments/i)).toHaveTextContent('supplier-notes.pdf')

    fireEvent.click(screen.getByRole('button', { name: /skills/i }))
    expect(screen.getByRole('dialog', { name: /choose a trademind skill/i })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /buyer test designer/i }))
    expect(screen.getByRole('textbox', { name: /ask trademind/i })).toHaveValue(
      'Use Buyer Test Designer to turn our sales assumptions into the smallest credible and falsifiable buyer test.',
    )
    expect(screen.queryByRole('dialog', { name: /choose a trademind skill/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /remove supplier-notes.pdf/i }))
    expect(screen.queryByText('supplier-notes.pdf')).not.toBeInTheDocument()
  })

  it('opens the workspace account menu and exposes the settings route', () => {
    render(<WorkspaceComposer {...baseProps()} />)

    fireEvent.click(screen.getByRole('button', { name: /open account menu for trademind workspace/i }))

    expect(screen.getByRole('dialog', { name: /workspace account menu/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /settings permissions, runtime, and language/i })).toHaveAttribute('href', '/app/settings')

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: /workspace account menu/i })).not.toBeInTheDocument()
  })

  it('localizes workspace controls and uses the TradeMind identity', () => {
    const props = baseProps()
    render(
      <I18nContext.Provider value="zh-CN">
        <WorkspaceComposer {...props} />
      </I18nContext.Provider>,
    )

    expect(screen.getByRole('heading', { level: 1, name: 'What can I do for you?' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '询问 TradeMind' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: '智能体' })).toHaveAttribute('href', '/app/agents')
    expect(screen.getByRole('link', { name: '插件' })).toHaveAttribute('href', '/app/plugins')
    expect(screen.getByText('TM')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: '打开 TradeMind 工作台账户菜单' }))
    expect(screen.getByRole('dialog', { name: '工作台账户菜单' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /设置\s+权限、运行时与语言/ })).toHaveAttribute('href', '/app/settings')

    fireEvent.click(screen.getByRole('button', { name: /找到真正卖得动的商品/ }))
    expect(props.onChange).toHaveBeenCalledWith(
      '寻找在 Amazon 和 TikTok Shop 上需求增长的商品',
    )
  })

  it('renders emitted reasoning and complete tool event details in the execution trace', () => {
    render(
      <WorkspaceComposer
        {...baseProps()}
        value="Review this market"
        run={{ ...completedRun, status: 'running', finished_at: null }}
        events={[
          {
            id: 'event_1', sequence: 7, type: 'tool.completed', run_id: 'run_1', project_id: 'project_1', trace_id: 'trace_1',
            sdk_session_id: null, provider_profile_id: null,
            timestamp: '2026-08-05T10:00:05Z', actor: { type: 'subagent', name: 'demand-agent', agent_id: 'agent_1' },
            parent_id: 'parent_1', tool_use_id: 'tool_1',
            data: { tool_name: 'WebSearch', tool_input: { query: 'buyer signals' }, tool_response: { result: 'three verified sources' } },
          },
        ]}
        assistantText={'## Result\n\nResearch completed with three verified sources.'}
        reasoningSummary="I compared the available buyer evidence before selecting the next validation step."
        connection="live"
        isActive={true}
      />,
    )

    const activitySummary = screen.getByText(/working for/i).closest('summary')
    // 调试期过程默认展开：先过程后结果（教材第 11 章），无需点击即可见。
    expect(activitySummary?.parentElement).toHaveAttribute('open')

    expect(screen.getByRole('heading', { name: 'Result' })).toBeInTheDocument()
    expect(screen.getByText(/websearch completed/i)).toBeVisible()
    const reasoningToggle = screen.getByText('Thinking').closest('summary')
    fireEvent.click(reasoningToggle!)
    expect(screen.getByText(/compared the available buyer evidence/i)).toBeVisible()
    expect(screen.getAllByText(/three verified sources/i)).toHaveLength(2)
    expect(screen.getByText(/trace trace_1/i)).toBeVisible()
  })

  it('shows a real history restore state without fabricating project results', () => {
    render(
      <WorkspaceComposer
        {...baseProps()}
        busy
        run={completedRun}
        connection="connecting"
        historyLoading
      />,
    )

    expect(screen.getByRole('status')).toHaveTextContent(/restoring the recorded response/i)
    expect(screen.queryByRole('heading', { name: /recorded assessment/i })).not.toBeInTheDocument()
  })
})

describe('AskUserQuestion card', () => {
  const questionPermission = {
    id: 'perm_q1',
    toolName: 'AskUserQuestion',
    toolInput: {
      questions: [
        {
          question: '重构的主要目的是什么？',
          header: '目的',
          options: [
            { label: '提升性能', description: '优化热点路径' },
            { label: '改善维护性', description: '降低复杂度' },
          ],
          multiSelect: false,
        },
      ],
    },
    actor: { type: 'agent' as const, name: 'sdk-permission', agent_id: null },
  }

  it('renders options as a choice card and submits selected answers', () => {
    const onDecision = vi.fn()
    render(
      <WorkspaceComposer
        {...baseProps()}
        run={{ ...completedRun, status: 'running', finished_at: null }}
        isActive
        permissions={[questionPermission]}
        onDecision={onDecision}
      />,
    )

    // 选择题形态，不是 Allow/Deny 授权卡
    expect(screen.queryByText('Allow once')).toBeNull()
    const submit = screen.getByText('Submit answers')
    expect(submit).toBeDisabled()

    fireEvent.click(screen.getByText('改善维护性'))
    expect(submit).not.toBeDisabled()
    fireEvent.click(submit)

    expect(onDecision).toHaveBeenCalledWith('perm_q1', 'allow_once', {
      '重构的主要目的是什么？': '改善维护性',
    })
  })

  it('skip falls back to deny so the agent proceeds on stated assumptions', () => {
    const onDecision = vi.fn()
    render(
      <WorkspaceComposer
        {...baseProps()}
        run={{ ...completedRun, status: 'running', finished_at: null }}
        isActive
        permissions={[questionPermission]}
        onDecision={onDecision}
      />,
    )
    fireEvent.click(screen.getByText('Skip — continue with what is known'))
    expect(onDecision).toHaveBeenCalledWith('perm_q1', 'deny')
  })
})
