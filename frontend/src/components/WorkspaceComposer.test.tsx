import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { vi } from 'vitest'
import type { PendingPermission, ProviderProfile, RunRecord, RunTelemetry, TelemetryMetric } from '../api/types'
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
    providers: [],
    providerProfileId: null,
    approvalMode: 'default' as const,
    runMode: 'standard' as const,
    onChange: vi.fn(),
    onAttachedFilesChange: vi.fn(),
    onProviderProfileChange: vi.fn(),
    onApprovalModeChange: vi.fn(async () => {}),
    onRunModeChange: vi.fn(),
    onSubmit: vi.fn(),
    onProjectRefresh: vi.fn(),
    onNewResearch: vi.fn(),
    onSelectProject: vi.fn(),
    onCancel: vi.fn(),
    onDecision: vi.fn(),
  }
}

describe('durable approval presentation', () => {
  const saved: PendingPermission = {
    id: 'approval-1', toolName: 'Bash', toolInput: { command: 'touch report.txt' }, actor: { type: 'agent', name: 'TradeMind', agent_id: null },
    destructive: false, persistable: false, scope: 'report.txt', durable: true, state: 'resolved', expiresAt: '2099-01-01T00:00:00Z',
  }

  it('shows saved decisions separately from approval buttons and resumes only when clicked', () => {
    const props = baseProps()
    const resume = vi.fn()
    const refresh = vi.fn()
    render(<WorkspaceComposer {...props} run={{ ...completedRun, status: 'waiting_permission' }} permissions={[saved]} onResumePermission={resume} onRefreshPermissions={refresh} />)
    expect(screen.getByText(/Decision saved, awaiting continuation/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Allow once' })).not.toBeInTheDocument()
    expect(resume).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Continue saved decision' }))
    expect(resume).toHaveBeenCalledWith(saved.id)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh approvals' }))
    expect(refresh).toHaveBeenCalledOnce()
  })

  it('disables expired saved decisions, even when the last server snapshot still said resolved', () => {
    const props = baseProps()
    render(<WorkspaceComposer {...props} run={{ ...completedRun, status: 'waiting_permission' }} permissions={[{ ...saved, expiresAt: '2020-01-01T00:00:00Z' }]} />)
    expect(screen.getByText(/This approval has expired/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Continue saved decision' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Allow once' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel run' }))
    expect(props.onCancel).toHaveBeenCalledOnce()
  })

  it('locks all approval actions while a decision or continuation is in flight', () => {
    const props = baseProps()
    render(<WorkspaceComposer {...props} run={{ ...completedRun, status: 'waiting_permission' }} permissions={[{ ...saved, state: 'pending' }]} permissionBusy />)
    expect(screen.getByRole('button', { name: 'Allow once' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Deny' })).toBeDisabled()
  })
})

function telemetryMetric<T>(value: T): TelemetryMetric<T> {
  return {
    value,
    availability: 'observed',
    unit: 'test',
    source: 'event_ledger',
    source_event_sequences: [],
    scope: null,
    unavailable_reason: null,
  }
}

const runTelemetry: RunTelemetry = {
  schema_version: '1',
  run_id: completedRun.id,
  project_id: completedRun.project_id,
  trace_id: completedRun.trace_id,
  computed_through_sequence: 12,
  event_log_complete_and_unambiguous: telemetryMetric(true),
  prompt_prefix_hash: telemetryMetric('a'.repeat(64)),
  latency: {
    run_queue_duration_ms: telemetryMetric(25),
    total_task_duration_ms: telemetryMetric(60_000),
    sdk_duration_ms: telemetryMetric(59_000),
    sdk_api_duration_ms: telemetryMetric(58_000),
    ttft_ms: telemetryMetric(1_200),
    first_visible_text_ms: telemetryMetric(1_200),
    first_progress_ms: telemetryMetric(800),
    first_useful_typed_card_ms: telemetryMetric(2_500),
    first_useful_typed_card_component: telemetryMetric('supplier_comparison'),
  },
  tools: {
    timings: [{
      tool_use_id: 'tool_use_1',
      tool_name: telemetryMetric('search_public_web'),
      lifecycle_status: 'completed',
      planned_event_sequences: [2],
      started_event_sequences: [3],
      terminal_event_sequences: [4],
      queue_duration_ms: telemetryMetric(25),
      run_duration_ms: telemetryMetric(300),
      quality_warnings: [],
    }],
    unattributed_event_sequences: [],
    queue_durations: {
      observed_count: 1,
      unavailable_count: 0,
      observed_total_ms: telemetryMetric(25),
      observed_max_ms: telemetryMetric(25),
    },
    run_durations: {
      observed_count: 1,
      unavailable_count: 0,
      observed_total_ms: telemetryMetric(300),
      observed_max_ms: telemetryMetric(300),
    },
  },
  tokens: {
    input_tokens: telemetryMetric(100),
    output_tokens: telemetryMetric(50),
    cache_read_input_tokens: telemetryMetric(80),
    cache_creation_input_tokens: telemetryMetric(20),
    cache_hit: telemetryMetric(true),
  },
  cost: {
    estimated_cost_usd: telemetryMetric(0.012),
    cost_basis: telemetryMetric('sdk_result'),
  },
  provider: {
    provider_profile_id: telemetryMetric('provider_a'),
    provider_display_name: telemetryMetric('Provider A'),
    configured_model: telemetryMetric('configured-model'),
    observed_models: telemetryMetric(['served-model']),
  },
  outcome: {
    run_status: 'completed',
    completed: telemetryMetric(true),
    result_subtype: telemetryMetric('success'),
    result_is_error: telemetryMetric(false),
    turn_count: telemetryMetric(4),
  },
  quality_warnings: [],
}

function StatefulComposer() {
  const [value, setValue] = useState('')
  const [attachedFiles, setAttachedFiles] = useState<File[]>([])
  const [runMode, setRunMode] = useState<'standard' | 'deep_research'>('standard')
  return (
    <WorkspaceComposer
      {...baseProps()}
      value={value}
      attachedFiles={attachedFiles}
      runMode={runMode}
      onChange={setValue}
      onAttachedFilesChange={setAttachedFiles}
      onRunModeChange={setRunMode}
    />
  )
}

describe('WorkspaceComposer', () => {
  it('shows server-replayed latency, turn, and cost telemetry for a terminal run', () => {
    render(
      <WorkspaceComposer
        {...baseProps()}
        run={completedRun}
        telemetry={runTelemetry}
      />,
    )

    const metrics = screen.getByLabelText('Run telemetry')
    expect(metrics).toHaveTextContent('Task time1m 0s')
    expect(metrics).toHaveTextContent('First text1s')
    expect(metrics).toHaveTextContent('First useful card2s')
    expect(metrics).toHaveTextContent('Turns4')
    expect(metrics).toHaveTextContent('Observed cost$0.01')
  })

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

  it('opens files, experts, skills, and connectors from one composer tools menu', () => {
    render(<StatefulComposer />)

    const attachment = new File(['supplier notes'], 'supplier-notes.pdf', { type: 'application/pdf' })
    fireEvent.change(screen.getByLabelText(/choose files or images/i), { target: { files: [attachment] } })
    expect(screen.getByLabelText(/locally staged attachments/i)).toHaveTextContent('supplier-notes.pdf')

    expect(screen.queryByRole('link', { name: /^skills$/i })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /open tools menu/i }))

    const menu = screen.getByRole('dialog', { name: /composer tools/i })
    expect(screen.getByRole('button', { name: /^add files$/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^mode$/i })).not.toBeInTheDocument()
    expect(within(menu).queryByRole('button', { name: /deep research/i })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: /^experts$/i })).toHaveAttribute('href', '/app/agents')
    expect(screen.getByRole('link', { name: /^skills$/i })).toHaveAttribute('href', '/app/plugins/skills')
    expect(screen.getByRole('link', { name: /^connectors$/i })).toHaveAttribute('href', '/app/plugins/connectors')

    fireEvent.click(screen.getByRole('button', { name: /remove supplier-notes.pdf/i }))
    expect(screen.queryByText('supplier-notes.pdf')).not.toBeInTheDocument()
  })

  it('turns Deep Research on and off from the composer as an explicit choice', () => {
    render(<StatefulComposer />)

    const toggle = screen.getByRole('button', { name: /deep research/i })
    expect(toggle).toHaveAttribute('aria-pressed', 'false')

    fireEvent.click(toggle)
    expect(screen.getByRole('button', { name: /deep research/i })).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(screen.getByRole('button', { name: /deep research/i }))
    expect(screen.getByRole('button', { name: /deep research/i })).toHaveAttribute('aria-pressed', 'false')
  })

  it('locks the Deep Research choice while a task is running', () => {
    render(<WorkspaceComposer {...baseProps()} busy />)

    expect(screen.getByRole('button', { name: /deep research/i })).toBeDisabled()
  })

  it('switches the workspace approval mode without offering unsandboxed full access', async () => {
    const onApprovalModeChange = vi.fn(async () => {})
    render(<WorkspaceComposer {...baseProps()} onApprovalModeChange={onApprovalModeChange} />)

    const trigger = screen.getByRole('button', { name: /open approval mode selector/i })
    expect(trigger).toHaveTextContent('Ask for approval')
    fireEvent.click(trigger)

    const menu = screen.getByRole('dialog', { name: /choose an approval mode/i })
    expect(within(menu).getByRole('link', { name: 'Settings' })).toHaveAttribute('href', '/app/settings')
    expect(within(menu).getByRole('button', { name: /ask for approval/i })).toHaveAttribute('data-active', 'true')
    expect(within(menu).getAllByRole('button')).toHaveLength(2)
    expect(within(menu).queryByText(/full access/i)).not.toBeInTheDocument()
    fireEvent.click(within(menu).getByRole('button', { name: /approve for me/i }))

    expect(onApprovalModeChange).toHaveBeenCalledWith('auto')
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /choose an approval mode/i })).not.toBeInTheDocument())
  })

  it('keeps the approval menu open with the reason when saving fails', async () => {
    const onApprovalModeChange = vi.fn(async () => { throw new Error('Policy store unavailable') })
    render(<WorkspaceComposer {...baseProps()} onApprovalModeChange={onApprovalModeChange} />)

    fireEvent.click(screen.getByRole('button', { name: /open approval mode selector/i }))
    fireEvent.click(screen.getByRole('button', { name: /approve for me/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Policy store unavailable')
    expect(screen.getByRole('dialog', { name: /choose an approval mode/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /open approval mode selector/i })).toHaveTextContent('Ask for approval')
  })

  it('locks the approval mode while a task is running or waiting for approval', () => {
    render(<WorkspaceComposer {...baseProps()} busy />)

    expect(screen.getByRole('button', { name: /open approval mode selector/i })).toBeDisabled()
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

describe('Run retry controls', () => {
  it('offers distinct resume and new-branch actions for a failed run', () => {
    const onRetry = vi.fn()
    render(
      <WorkspaceComposer
        {...baseProps()}
        run={{ ...completedRun, status: 'failed', error: 'Provider timed out' }}
        onRetry={onRetry}
      />,
    )

    expect(screen.getByText(/both choices reuse the saved project/i)).toBeInTheDocument()
    const resume = screen.getByRole('button', { name: /resume session/i })
    const branch = screen.getByRole('button', { name: /start new branch/i })
    expect(resume).not.toBeDisabled()
    expect(branch).not.toBeDisabled()

    fireEvent.click(resume)
    fireEvent.click(branch)
    expect(onRetry).toHaveBeenNthCalledWith(1, 'resume')
    expect(onRetry).toHaveBeenNthCalledWith(2, 'new_branch')
  })

  it('keeps new-branch recovery available when a cancelled run has no SDK session', () => {
    render(
      <WorkspaceComposer
        {...baseProps()}
        run={{
          ...completedRun,
          status: 'cancelled',
          sdk_session_id: null,
          error: null,
        }}
        retryError="Retry policy denied"
        onRetry={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: /no recorded sdk session/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /start new branch/i })).not.toBeDisabled()
    expect(screen.getByRole('alert')).toHaveTextContent('Retry policy denied')
  })

  it('disables both choices while either retry request is in flight', () => {
    render(
      <WorkspaceComposer
        {...baseProps()}
        run={{ ...completedRun, status: 'failed' }}
        retrying="new_branch"
        onRetry={vi.fn()}
      />,
    )

    expect(screen.getByRole('button', { name: /resume session/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /starting branch/i })).toBeDisabled()
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
    destructive: false,
    persistable: false,
    scope: '',
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

  it('never allows a malformed question payload through with empty answers', () => {
    const onDecision = vi.fn()
    render(
      <WorkspaceComposer
        {...baseProps()}
        run={{ ...completedRun, status: 'running', finished_at: null }}
        isActive
        permissions={[{
          ...questionPermission,
          id: 'perm_bad',
          toolInput: {
            questions: [
              ...(questionPermission.toolInput.questions),
              { question: 'Malformed follow-up', options: [] },
            ],
          },
        }]}
        onDecision={onDecision}
      />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent(/could not be decoded/i)
    expect(screen.getByRole('button', { name: /submit answers/i })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /skip/i }))
    expect(onDecision).toHaveBeenCalledWith('perm_bad', 'deny')
  })
})

describe('Permission safety and provider eligibility', () => {
  it('shows the server scope, validity and redacted input while hiding persistent choices for destructive work', () => {
    render(
      <WorkspaceComposer
        {...baseProps()}
        run={{ ...completedRun, status: 'running', finished_at: null }}
        isActive
        permissions={[{
          id: 'perm_delete',
          toolName: 'Bash',
          toolInput: { command: 'rm report.csv', api_token: 'super-secret' },
          actor: { type: 'agent', name: 'sdk-permission', agent_id: null },
          destructive: true,
          persistable: true,
          scope: 'path:/workspace/report.csv',
          timeoutSeconds: 180,
        }]}
      />,
    )

    expect(screen.getByText('path:/workspace/report.csv')).toBeInTheDocument()
    expect(screen.getByText('3 minutes')).toBeInTheDocument()
    expect(screen.getByText(/\[REDACTED\]/)).toBeInTheDocument()
    expect(screen.queryByText('super-secret')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /allow once/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /allow for run/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /allow persistent/i })).not.toBeInTheDocument()
  })

  it('only enables explicitly runnable provider profiles and discloses their reported destinations', () => {
    const baseProvider: ProviderProfile = {
      id: 'ready',
      display_name: 'Ready provider',
      base_url: null,
      auth_env_name: 'ANTHROPIC_API_KEY',
      model: 'ready-model',
      region: 'us',
      production_eligible: true,
      enabled: true,
      secret_configured: true,
      last_verified_at: '2026-09-02T00:00:00Z',
      last_error_code: null,
      last_error_message: null,
      capabilities: { tool_use: true, streaming: true, images: false, prompt_caching: false, extended_thinking: false, tool_search: false },
    }
    const providers = [
      baseProvider,
      { ...baseProvider, id: 'disabled', model: 'disabled-model', enabled: false },
      { ...baseProvider, id: 'no-secret', model: 'no-secret-model', secret_configured: false },
      { ...baseProvider, id: 'not-verified', model: 'not-verified-model', last_verified_at: null },
      { ...baseProvider, id: 'not-prod', model: 'not-prod-model', production_eligible: false },
      { ...baseProvider, id: 'environment', model: 'environment-model', region: 'environment', production_eligible: false },
      { ...baseProvider, id: 'preflight-failed', model: 'failed-model', last_error_code: 'AUTH' },
    ]
    render(<WorkspaceComposer {...baseProps()} value="Run this task" providers={providers} providerProfileId="environment" />)

    expect(screen.getByRole('button', { name: /^send$/i })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /open model selector/i }))
    const ready = screen.getByRole('button', { name: /ready-model/i })
    expect(ready).not.toBeDisabled()
    expect(within(ready).getByText('Reported region: us')).toBeVisible()
    expect(within(ready).getByText('Endpoint: Anthropic default endpoint')).toBeVisible()
    expect(screen.getByText('Provider regions are administrator-reported. Model requests may cross borders.')).toBeVisible()
    expect(screen.getByRole('button', { name: /disabled-model/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /no-secret-model/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /not-verified-model/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /not-prod-model/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /environment-model/i })).toBeDisabled()
    expect(screen.getByRole('button', { name: /failed-model/i })).toBeDisabled()
  })

  it('keeps the selected data destination visible after closing the model selector', () => {
    const provider: ProviderProfile = {
      id: 'reviewed', display_name: 'Configured gateway', model: 'example-model',
      base_url: 'https://models.example/v1', auth_env_name: 'ANTHROPIC_API_KEY',
      region: 'Region A', production_eligible: true, enabled: true, secret_configured: true,
      last_verified_at: '2026-09-22T00:00:00Z', last_error_code: null, last_error_message: null,
      capabilities: { tool_use: true, streaming: true, images: false, prompt_caching: false, extended_thinking: false, tool_search: false },
    }
    function Selection() {
      const [id, setId] = useState<string | null>(null)
      return <WorkspaceComposer {...baseProps()} providers={[provider]} providerProfileId={id} onProviderProfileChange={setId} />
    }
    render(<Selection />)
    expect(screen.getByText(/Auto uses server-side Provider selection/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: /open model selector/i }))
    fireEvent.click(screen.getByRole('button', { name: /example-model/ }))
    expect(screen.queryByRole('dialog', { name: /choose a model/i })).not.toBeInTheDocument()
    const notice = screen.getByRole('complementary', { name: 'Model data and region' })
    expect(within(notice).getByText('Endpoint: https://models.example')).toBeVisible()
    expect(within(notice).getByText('Reported region: Region A')).toBeVisible()
    expect(within(notice).getByText(/Model requests may cross borders/)).toBeVisible()
    expect(screen.queryByText(/Auto uses server-side Provider selection/)).not.toBeInTheDocument()
  })

  it('does not offer run-wide approval when the backend marks a request non-persistable', () => {
    render(
      <WorkspaceComposer
        {...baseProps()}
        run={{ ...completedRun, status: 'running', finished_at: null }}
        isActive
        permissions={[{
          id: 'perm_nonpersistent',
          toolName: 'Bash',
          toolInput: { command: 'pwd' },
          actor: { type: 'agent', name: 'sdk-permission', agent_id: null },
          destructive: false,
          persistable: false,
          scope: '',
        }]}
      />,
    )

    expect(screen.getByRole('button', { name: /allow once/i })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /allow for run/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /allow persistent/i })).not.toBeInTheDocument()
  })
})
