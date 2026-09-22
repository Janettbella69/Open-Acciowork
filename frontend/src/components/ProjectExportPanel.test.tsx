import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { downloadProjectExport, previewProjectExport } from '../api/client'
import type { ProjectExportPreview } from '../api/projectExports'
import { I18nContext } from '../i18n'
import ProjectExportPanel from './ProjectExportPanel'

vi.mock('../api/client', () => ({
  downloadProjectExport: vi.fn(), previewProjectExport: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))
const preview: ProjectExportPreview = {
  schema_version: 1, kind: 'project', project_id: 'project_1', selected_run_id: null,
  project_revision: 4, run_ids: ['run_1', 'run_2'], fingerprint: 'a'.repeat(64),
  file_count: 8, total_bytes: 4123, warnings: [], exclusions: ['provider_and_connector_credentials'],
}
const createURL = vi.fn<(blob: Blob) => string>(() => 'blob:project-export')

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(previewProjectExport).mockResolvedValue(preview)
  vi.mocked(downloadProjectExport).mockResolvedValue(new Blob(['ZIP fixture'], { type: 'application/zip' }))
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = createURL
    static revokeObjectURL = vi.fn()
  })
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

function open() {
  const view = render(<ProjectExportPanel projectId="project_1" runId="run_2" />)
  fireEvent.click(screen.getByRole('button', { name: 'Export project data' }))
  return view
}

it('does no background export and downloads only the reviewed inventory', async () => {
  open()
  expect(previewProjectExport).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Review project export' }))
  expect(await screen.findByText('Project archive')).toBeInTheDocument()
  expect(screen.getByText('8 files · 2 runs · 4123 bytes before compression')).toBeInTheDocument()
  expect(screen.getByText(/It is unencrypted/)).toBeInTheDocument()
  expect(downloadProjectExport).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Download reviewed ZIP' }))
  expect(await screen.findByText(/Data archive downloaded/)).toHaveAttribute('role', 'status')
  expect(downloadProjectExport).toHaveBeenCalledWith('project_1', preview, expect.any(AbortSignal))
  expect(createURL).toHaveBeenCalledTimes(1)
  expect(screen.queryByText('Project archive')).not.toBeInTheDocument()
})

it('reviews the current conversation and displays missing historical versions', async () => {
  const session = { ...preview, kind: 'session' as const, selected_run_id: 'run_2', warnings: [{ code: 'legacy_artifact_unavailable', id: 'old' }] }
  vi.mocked(previewProjectExport).mockResolvedValue(session)
  open()
  fireEvent.click(screen.getByRole('button', { name: 'Review conversation export' }))
  expect(await screen.findByText('Conversation archive')).toBeInTheDocument()
  expect(previewProjectExport).toHaveBeenCalledWith('project_1', 'run_2', expect.any(AbortSignal))
  expect(screen.getByRole('alert')).toHaveTextContent('1 older artifact versions are unavailable')
})

it('rejects a preview from another project and never enables its download', async () => {
  vi.mocked(previewProjectExport).mockResolvedValue({ ...preview, project_id: 'another-project' })
  open()
  fireEvent.click(screen.getByRole('button', { name: 'Review project export' }))
  expect(await screen.findByRole('alert')).toHaveTextContent('did not match this selection')
  expect(screen.queryByRole('button', { name: 'Download reviewed ZIP' })).not.toBeInTheDocument()
})

it('clears stale review after a failed download and prevents duplicate requests', async () => {
  let fail!: (reason: unknown) => void
  vi.mocked(downloadProjectExport).mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject }))
  open()
  fireEvent.click(screen.getByRole('button', { name: 'Review project export' }))
  const button = await screen.findByRole('button', { name: 'Download reviewed ZIP' })
  fireEvent.click(button)
  fireEvent.click(button)
  expect(downloadProjectExport).toHaveBeenCalledTimes(1)
  expect(button).toBeDisabled()
  await act(async () => { fail(new Error('Project data changed. Refresh the export preview.')) })
  expect(await screen.findByRole('alert')).toHaveTextContent('Project data changed')
  expect(screen.queryByRole('button', { name: 'Download reviewed ZIP' })).not.toBeInTheDocument()
  expect(createURL).not.toHaveBeenCalled()
})

it.each(['close', 'project', 'run'])('aborts a download on %s changes and discards its late response', async (change) => {
  let finish!: (value: Blob) => void
  vi.mocked(downloadProjectExport).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const view = open()
  fireEvent.click(screen.getByRole('button', { name: 'Review project export' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Download reviewed ZIP' }))
  const signal = vi.mocked(downloadProjectExport).mock.calls[0][2]!
  if (change === 'close') fireEvent.click(screen.getByRole('button', { name: 'Export project data' }))
  else view.rerender(<ProjectExportPanel projectId={change === 'project' ? 'project_2' : 'project_1'} runId="run_3" />)
  expect(signal.aborted).toBe(true)
  await act(async () => { finish(new Blob(['private archive'])) })
  expect(createURL).not.toHaveBeenCalled()
  expect(screen.queryByText('Project archive')).not.toBeInTheDocument()
})

it('aborts a pending preview on close and presents localized controls', async () => {
  let finish!: (value: ProjectExportPreview) => void
  vi.mocked(previewProjectExport).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  render(<I18nContext.Provider value="zh-CN"><ProjectExportPanel projectId="project_1" /></I18nContext.Provider>)
  fireEvent.click(screen.getByRole('button', { name: '导出项目数据' }))
  expect(screen.getByRole('button', { name: '查看会话导出范围' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '查看项目导出范围' }))
  await waitFor(() => expect(previewProjectExport).toHaveBeenCalledTimes(1))
  const signal = vi.mocked(previewProjectExport).mock.calls[0][2]!
  fireEvent.click(screen.getByRole('button', { name: '导出项目数据' }))
  expect(signal.aborted).toBe(true)
  await act(async () => { finish(preview) })
  fireEvent.click(screen.getByRole('button', { name: '导出项目数据' }))
  expect(screen.queryByText('项目归档')).not.toBeInTheDocument()
})
