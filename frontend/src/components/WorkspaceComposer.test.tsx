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
    expect(screen.getByRole('textbox', { name: /ask ophira/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start research/i })).toBeDisabled()
    expect(screen.queryByRole('link', { name: /04 settings/i })).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: /find a real unmet need/i }))
    expect(onChange).toHaveBeenCalledWith(expect.stringMatching(/unmet customer need/i))
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
})
