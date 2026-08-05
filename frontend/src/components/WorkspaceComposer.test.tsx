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
        isActive={false}
        onChange={onChange}
        onSubmit={vi.fn()}
        onNewResearch={vi.fn()}
        onSelectProject={vi.fn()}
        onCancel={vi.fn()}
        onDecision={vi.fn()}
      />,
    )

    expect(screen.getByRole('heading', { level: 1, name: /what opportunity should we explore/i })).toBeInTheDocument()
    const primaryComposer = screen.getByRole('textbox', { name: /ask ophira/i })
    expect(primaryComposer).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start research/i })).toBeDisabled()
    expect(screen.getByRole('link', { name: /01 opportunity studio/i })).toHaveAttribute('href', '/app')
    expect(screen.getByRole('link', { name: /03 skill studio/i })).toHaveAttribute('href', '/app/skills')
    expect(screen.queryByRole('link', { name: /04 settings/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /agent team/i })).not.toBeInTheDocument()

    const demandShortcut = screen.getByRole('button', { name: /find a real unmet need/i })
    expect(primaryComposer.compareDocumentPosition(demandShortcut) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    fireEvent.click(demandShortcut)
    expect(onChange).toHaveBeenCalledWith(expect.stringMatching(/unmet customer need/i))
  })

  it('stages local attachments and inserts built-in skill instructions', () => {
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
    expect(screen.getByText(/attachment upload is not connected yet/i)).toBeInTheDocument()

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
        assistantText="Research completed with three verified sources."
        reasoningSummary="I compared the available buyer evidence before selecting the next validation step."
        permissions={[]}
        connection="live"
        isActive={true}
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        onNewResearch={vi.fn()}
        onSelectProject={vi.fn()}
        onCancel={vi.fn()}
        onDecision={vi.fn()}
      />,
    )

    expect(screen.getByRole('heading', { name: /running for/i })).toBeInTheDocument()
    expect(screen.getByText(/compared the available buyer evidence/i)).toBeVisible()
    expect(screen.getByText(/websearch completed/i)).toBeInTheDocument()
    expect(screen.getAllByText(/three verified sources/i)).toHaveLength(2)
    expect(screen.getByText(/reasoning is shown only when the sdk emits a summary/i)).toBeInTheDocument()
  })
})
