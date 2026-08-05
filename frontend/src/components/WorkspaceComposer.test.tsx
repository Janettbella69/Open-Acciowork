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
        onChange={onChange}
        onSubmit={vi.fn()}
        onSelectProject={vi.fn()}
      />,
    )

    expect(screen.getByRole('heading', { level: 1, name: /what opportunity should we explore/i })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: /ask ophira/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /start research/i })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: /find a real unmet need/i }))
    expect(onChange).toHaveBeenCalledWith(expect.stringMatching(/unmet customer need/i))
  })
})
