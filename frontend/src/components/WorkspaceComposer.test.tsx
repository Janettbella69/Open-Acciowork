import { fireEvent, render, screen } from '@testing-library/react'
import { vi } from 'vitest'
import WorkspaceComposer from './WorkspaceComposer'

describe('WorkspaceComposer', () => {
  it('starts the workspace from one primary conversation box', () => {
    const onChange = vi.fn()
    render(
      <WorkspaceComposer
        value=""
        busy={false}
        notice=""
        error=""
        projects={[]}
        project={null}
        run={null}
        events={[]}
        assistantText=""
        reasoningSummary=""
        permissions={[]}
        connection="idle"
        historyLoading={false}
        isActive={false}
        onChange={onChange}
        onSubmit={vi.fn()}
        onNewResearch={vi.fn()}
        onSelectProject={vi.fn()}
        onCancel={vi.fn()}
        onDecision={vi.fn()}
      />,
    )

    expect(screen.getByRole('heading', { level: 1, name: /what are you working on/i })).toBeInTheDocument()
    const primaryComposer = screen.getByRole('textbox', { name: /ask ophira/i })
    expect(primaryComposer).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /send/i })).toBeDisabled()
    expect(screen.getByRole('link', { name: /01 agent workspace/i })).toHaveAttribute('href', '/app')
    expect(screen.getByRole('link', { name: /03 skill studio/i })).toHaveAttribute('href', '/app/skills')
    expect(screen.queryByRole('link', { name: /04 settings/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /agent team/i })).not.toBeInTheDocument()

    expect(screen.getByRole('heading', { name: /start with a validation task/i })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /demand|supply|capital|sales/i })).toHaveLength(4)
    expect(onChange).not.toHaveBeenCalled()
  })

  it('keeps add files and skills as separate composer controls', () => {
    const onChange = vi.fn()
    render(
      <WorkspaceComposer
        value=""
        busy={false}
        notice=""
        error=""
        projects={[]}
        project={null}
        run={null}
        events={[]}
        assistantText=""
        reasoningSummary=""
        permissions={[]}
        connection="idle"
        historyLoading={false}
        isActive={false}
        onChange={onChange}
        onSubmit={vi.fn()}
        onNewResearch={vi.fn()}
        onSelectProject={vi.fn()}
        onCancel={vi.fn()}
        onDecision={vi.fn()}
      />,
    )

    const attachment = new File(['supplier notes'], 'supplier-notes.pdf', { type: 'application/pdf' })
    fireEvent.change(screen.getByLabelText(/choose files or images/i), { target: { files: [attachment] } })
    expect(screen.getByLabelText(/locally staged attachments/i)).toHaveTextContent('supplier-notes.pdf')

    fireEvent.click(screen.getByRole('button', { name: /skills/i }))
    expect(screen.getByRole('dialog', { name: /choose an ophira skill/i })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /buyer test designer/i }))
    expect(onChange).toHaveBeenCalledWith(expect.stringMatching(/smallest credible and falsifiable buyer test/i))
    expect(screen.queryByRole('dialog', { name: /choose an ophira skill/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /remove supplier-notes.pdf/i }))
    expect(screen.queryByText('supplier-notes.pdf')).not.toBeInTheDocument()
  })

  it('opens the workspace account menu and exposes the settings route', () => {
    render(
      <WorkspaceComposer
        value=""
        busy={false}
        notice=""
        error=""
        projects={[]}
        project={null}
        run={null}
        events={[]}
        assistantText=""
        reasoningSummary=""
        permissions={[]}
        connection="idle"
        historyLoading={false}
        isActive={false}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        onNewResearch={vi.fn()}
        onSelectProject={vi.fn()}
        onCancel={vi.fn()}
        onDecision={vi.fn()}
      />,
    )

    const accountButton = screen.getByRole('button', { name: /open account menu for ophira workspace/i })
    fireEvent.click(accountButton)

    expect(screen.getByRole('dialog', { name: /workspace account menu/i })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /settings permissions, runtime, and language/i })).toHaveAttribute('href', '/app/settings')

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(screen.queryByRole('dialog', { name: /workspace account menu/i })).not.toBeInTheDocument()
  })

  it('renders emitted reasoning and complete tool event details in the execution trace', () => {
    render(
      <WorkspaceComposer
        value="Review this market"
        busy={false}
        notice=""
        error=""
        projects={[]}
        project={null}
        run={{
          id: 'run_1', trace_id: 'trace_1', project_id: 'project_1', prompt: 'Review this market', status: 'running',
          sdk_session_id: 'session_1', error: null, created_at: '2026-08-05T10:00:00Z', started_at: '2026-08-05T10:00:00Z', finished_at: null,
        }}
        events={[
          {
            id: 'event_1', sequence: 7, type: 'tool.completed', run_id: 'run_1', project_id: 'project_1', trace_id: 'trace_1',
            timestamp: '2026-08-05T10:00:05Z', actor: { type: 'subagent', name: 'demand-agent', agent_id: 'agent_1' },
            parent_id: 'parent_1', tool_use_id: 'tool_1',
            data: { tool_name: 'WebSearch', tool_input: { query: 'buyer signals' }, tool_response: { result: 'three verified sources' } },
          },
        ]}
        assistantText={'## Result\n\nResearch completed with three verified sources.'}
        reasoningSummary="I compared the available buyer evidence before selecting the next validation step."
        permissions={[]}
        connection="live"
        historyLoading={false}
        isActive={true}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        onNewResearch={vi.fn()}
        onSelectProject={vi.fn()}
        onCancel={vi.fn()}
        onDecision={vi.fn()}
      />,
    )

    const activitySummary = screen.getByText(/working for/i).closest('summary')
    expect(activitySummary?.parentElement).not.toHaveAttribute('open')
    fireEvent.click(activitySummary!)

    expect(screen.getByRole('heading', { name: 'Result' })).toBeInTheDocument()
    expect(screen.getByText(/websearch completed/i)).toBeVisible()
    const reasoningSummary = screen.getByText('Thinking').closest('summary')
    fireEvent.click(reasoningSummary!)
    expect(screen.getByText(/compared the available buyer evidence/i)).toBeVisible()
    expect(screen.getAllByText(/three verified sources/i)).toHaveLength(2)
    expect(screen.getByText(/trace trace_1/i)).toBeVisible()
  })

  it('shows a real history restore state without fabricating project results', () => {
    render(
      <WorkspaceComposer
        value=""
        busy
        notice=""
        error=""
        projects={[]}
        project={null}
        run={{
          id: 'run_1', trace_id: 'trace_1', project_id: 'project_1', prompt: 'Review this market', status: 'completed',
          sdk_session_id: 'session_1', error: null, created_at: '2026-08-05T10:00:00Z', started_at: '2026-08-05T10:00:00Z', finished_at: '2026-08-05T10:01:00Z',
        }}
        events={[]}
        assistantText=""
        reasoningSummary=""
        permissions={[]}
        connection="connecting"
        historyLoading
        isActive={false}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        onNewResearch={vi.fn()}
        onSelectProject={vi.fn()}
        onCancel={vi.fn()}
        onDecision={vi.fn()}
      />,
    )

    expect(screen.getByRole('status')).toHaveTextContent(/restoring the recorded response/i)
    expect(screen.queryByRole('heading', { name: /recorded assessment/i })).not.toBeInTheDocument()
  })
})
