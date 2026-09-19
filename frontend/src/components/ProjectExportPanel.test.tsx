import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProjectExportPreview } from '../api/projectExport'
import { I18nContext } from '../i18n'
import ProjectExportPanel from './ProjectExportPanel'

vi.mock('../api/client', () => ({
  getProjectExport: vi.fn(), downloadProjectExport: vi.fn(),
  errorMessage: (reason: unknown) => reason instanceof Error ? reason.message : 'Unknown error',
}))
import { downloadProjectExport, getProjectExport } from '../api/client'

const createUrl = vi.fn()
const revokeUrl = vi.fn()
const privateNote = 'Workspace files, saved artifact versions and project Skill proposal packages retain their original bytes, including any private content you placed in them.'
function fixture(projectId = 'project_1'): ProjectExportPreview {
  return {
    format: 'ophira-project-export-v1', project_id: projectId, actor_id: 'owner_1', revision: 4,
    fingerprint: 'a'.repeat(64), file_count: 1, total_bytes: 200, directories: ['workspace/empty'],
    entries: [{ path: 'workspace/quote.txt', category: 'workspace', size_bytes: 200, sha256: 'b'.repeat(64) }],
    notes: [privateNote],
  }
}
function open() { fireEvent.click(screen.getByRole('button', { name: 'Project export' })) }
function download() { fireEvent.click(screen.getByRole('button', { name: 'Download reviewed ZIP' })) }

beforeEach(() => {
  vi.clearAllMocks()
  const OriginalURL = URL
  vi.stubGlobal('URL', class extends OriginalURL {
    static createObjectURL = createUrl
    static revokeObjectURL = revokeUrl
  })
  createUrl.mockReturnValue('blob:reviewed-project-export')
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
  vi.mocked(getProjectExport).mockResolvedValue(fixture())
  vi.mocked(downloadProjectExport).mockResolvedValue(new Blob(['archive'], { type: 'application/zip' }))
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('ProjectExportPanel', () => {
  it('starts only one preview request when StrictMode replays the mount effect', async () => {
    render(<StrictMode><ProjectExportPanel projectId="project_1" /></StrictMode>)
    open()
    await screen.findByText('workspace/quote.txt')
    expect(getProjectExport).toHaveBeenCalledTimes(1)
    expect(vi.mocked(getProjectExport).mock.calls[0][1]?.aborted).toBe(false)
  })

  it('shows a real busy conflict in Chinese without starting another preview request', async () => {
    vi.mocked(getProjectExport).mockRejectedValueOnce(Object.assign(new Error('Project files are busy. Try exporting again shortly.'), { status: 409 }))
    render(<StrictMode><I18nContext.Provider value="zh-CN"><ProjectExportPanel projectId="project_1" /></I18nContext.Provider></StrictMode>)
    fireEvent.click(screen.getByRole('button', { name: '导出项目' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('项目文件正在使用中，请稍后重试导出。')
    expect(getProjectExport).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: '下载已审阅的 ZIP' })).toBeDisabled()
  })

  it('loads only when opened, reviews returned coverage and bounds the file list with pagination', async () => {
    const preview = fixture()
    preview.entries = Array.from({ length: 52 }, (_, index) => ({ ...preview.entries[0], path: `workspace/file-${index}.txt` }))
    preview.file_count = 52; preview.total_bytes = 10400
    vi.mocked(getProjectExport).mockResolvedValue(preview)
    render(<ProjectExportPanel projectId="project_1" />)
    expect(getProjectExport).not.toHaveBeenCalled()
    open()
    expect(screen.getByRole('button', { name: 'Download reviewed ZIP' })).toBeDisabled()
    await screen.findByText(privateNote)
    expect(getProjectExport).toHaveBeenCalledWith('project_1', expect.any(AbortSignal))
    expect(screen.getByText('10,400 bytes')).toBeInTheDocument()
    expect(screen.getByText('File counts and sizes describe project data files. The ZIP also includes an integrity manifest.')).toBeInTheDocument()
    const files = screen.getByRole('list', { name: 'Reviewed export files' })
    expect(within(files).getAllByRole('listitem')).toHaveLength(50)
    expect(screen.queryByText('workspace/file-50.txt')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Next page' }))
    expect(within(files).getAllByRole('listitem')).toHaveLength(3)
    expect(screen.getByText('workspace/empty')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'Previous page' }))
    expect(screen.getByText('workspace/file-0.txt')).toBeInTheDocument()
  })

  it('downloads only the reviewed fingerprint once and reports browser handoff rather than a completed save', async () => {
    let finish!: (archive: Blob) => void
    vi.mocked(downloadProjectExport).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const { unmount } = render(<ProjectExportPanel projectId="project_1" />)
    open(); await screen.findByText('workspace/quote.txt')
    const button = screen.getByRole('button', { name: 'Download reviewed ZIP' })
    fireEvent.click(button); fireEvent.click(button)
    expect(downloadProjectExport).toHaveBeenCalledTimes(1)
    expect(downloadProjectExport).toHaveBeenCalledWith('project_1', 'a'.repeat(64), expect.any(AbortSignal))
    expect(screen.getByRole('button', { name: 'Preparing ZIP download…' })).toBeDisabled()
    expect(createUrl).not.toHaveBeenCalled()
    const archive = new Blob(['zip bytes'], { type: 'application/zip' })
    await act(async () => { finish(archive) })
    expect(createUrl).toHaveBeenCalledWith(archive)
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalledTimes(1)
    const link = vi.mocked(HTMLAnchorElement.prototype.click).mock.instances[0]
    expect(link).toHaveAttribute('download', 'project_1-export.zip')
    expect(await screen.findByRole('status')).toHaveTextContent('Check your browser downloads to confirm it was saved.')
    unmount()
    expect(revokeUrl).toHaveBeenCalledWith('blob:reviewed-project-export')
  })

  it('requires a fresh review after a changed archive and sends its new fingerprint', async () => {
    vi.mocked(downloadProjectExport).mockRejectedValueOnce(Object.assign(new Error('Project data changed. Review a fresh export.'), { status: 409 }))
    render(<ProjectExportPanel projectId="project_1" />)
    open(); await screen.findByText('workspace/quote.txt'); download()
    expect(await screen.findByRole('alert')).toHaveTextContent('Project data changed. Review a fresh export.')
    expect(screen.getByRole('status')).toHaveTextContent('Review a fresh export before downloading.')
    expect(screen.queryByText('workspace/quote.txt')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download reviewed ZIP' })).toBeDisabled()
    expect(createUrl).not.toHaveBeenCalled()
    vi.mocked(getProjectExport).mockResolvedValue({ ...fixture(), fingerprint: 'c'.repeat(64) })
    fireEvent.click(screen.getByRole('button', { name: 'Refresh export preview' }))
    await screen.findByText('workspace/quote.txt'); download()
    await waitFor(() => expect(downloadProjectExport).toHaveBeenLastCalledWith('project_1', 'c'.repeat(64), expect.any(AbortSignal)))
  })

  it.each([403, 404])('drops all private preview data after download returns %s', async (status) => {
    vi.mocked(downloadProjectExport).mockRejectedValueOnce(Object.assign(new Error('Project export access is unavailable'), { status }))
    render(<ProjectExportPanel projectId="project_1" />)
    open(); await screen.findByText('workspace/quote.txt'); download()
    await screen.findByRole('alert')
    expect(screen.queryByText('workspace/quote.txt')).not.toBeInTheDocument()
    expect(screen.queryByText('owner_1')).not.toBeInTheDocument()
    expect(screen.queryByText('a'.repeat(64))).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download reviewed ZIP' })).toBeDisabled()
    expect(createUrl).not.toHaveBeenCalled()
  })

  it('keeps the review retryable after a download error without announcing a saved archive', async () => {
    vi.mocked(downloadProjectExport).mockRejectedValueOnce(new Error('Connection lost'))
    render(<ProjectExportPanel projectId="project_1" />)
    open(); await screen.findByText('workspace/quote.txt'); download()
    expect(await screen.findByRole('alert')).toHaveTextContent('Connection lost')
    expect(screen.getByText('workspace/quote.txt')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Download reviewed ZIP' })).toBeEnabled()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(createUrl).not.toHaveBeenCalled()
  })

  it('rejects a preview from another project and recovers from preview errors', async () => {
    vi.mocked(getProjectExport).mockResolvedValueOnce(fixture('other_project'))
    render(<ProjectExportPanel projectId="project_1" />)
    open()
    expect(await screen.findByRole('alert')).toHaveTextContent('The export preview did not match this project.')
    expect(screen.getByRole('button', { name: 'Download reviewed ZIP' })).toBeDisabled()
    expect(screen.queryByText('workspace/quote.txt')).not.toBeInTheDocument()
    vi.mocked(getProjectExport).mockRejectedValueOnce(new Error('Preview unavailable'))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh export preview' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Preview unavailable')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh export preview' }))
    await screen.findByText('workspace/quote.txt')
  })

  it('aborts an obsolete preview on refresh and ignores its late result', async () => {
    let finish!: (preview: ProjectExportPreview) => void
    vi.mocked(getProjectExport).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    render(<ProjectExportPanel projectId="project_1" />)
    open()
    await waitFor(() => expect(getProjectExport).toHaveBeenCalledTimes(1))
    const signal = vi.mocked(getProjectExport).mock.calls[0][1]!
    fireEvent.click(screen.getByRole('button', { name: 'Refresh export preview' }))
    await screen.findByText('workspace/quote.txt')
    expect(signal.aborted).toBe(true)
    await act(async () => { finish({ ...fixture(), entries: [{ ...fixture().entries[0], path: 'obsolete.txt' }] }) })
    expect(screen.queryByText('obsolete.txt')).not.toBeInTheDocument()
  })

  it('clears the previous project immediately and drops its pending archive without creating a URL', async () => {
    let finish!: (archive: Blob) => void
    vi.mocked(downloadProjectExport).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const { rerender } = render(<ProjectExportPanel projectId="project_1" />)
    open(); await screen.findByText('workspace/quote.txt'); download()
    const signal = vi.mocked(downloadProjectExport).mock.calls[0][2]!
    rerender(<ProjectExportPanel projectId="project_2" />)
    expect(signal.aborted).toBe(true)
    expect(screen.queryByText('workspace/quote.txt')).not.toBeInTheDocument()
    await act(async () => { finish(new Blob(['old archive'])) })
    expect(createUrl).not.toHaveBeenCalled()
    expect(HTMLAnchorElement.prototype.click).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Project export' })).toHaveAttribute('aria-expanded', 'false')
  })

  it('aborts a pending preview when closed and ignores the response after unmount', async () => {
    let finish!: (preview: ProjectExportPreview) => void
    vi.mocked(getProjectExport).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const { unmount } = render(<ProjectExportPanel projectId="project_1" />)
    open()
    await waitFor(() => expect(getProjectExport).toHaveBeenCalledTimes(1))
    const signal = vi.mocked(getProjectExport).mock.calls[0][1]!
    fireEvent.click(screen.getByRole('button', { name: 'Project export' }))
    expect(signal.aborted).toBe(true)
    unmount()
    await act(async () => { finish(fixture()) })
    expect(createUrl).not.toHaveBeenCalled()
  })

  it('localizes the review and privacy notes while displaying filenames as plain text', async () => {
    const preview = fixture()
    preview.entries[0].path = '<img src=x onerror=alert(1)>.txt'
    vi.mocked(getProjectExport).mockResolvedValue(preview)
    render(<I18nContext.Provider value="zh-CN"><ProjectExportPanel projectId="project_1" /></I18nContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: '导出项目' }))
    expect(await screen.findByText('工作区文件、已保存的产物版本及项目 Skill 提案包保留原始文件内容，包括你放入其中的私人信息。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '下载已审阅的 ZIP' })).toBeEnabled()
    expect(screen.getByText('200 字节', { selector: 'dd' })).toBeInTheDocument()
    expect(screen.getByText('<img src=x onerror=alert(1)>.txt')).toBeInTheDocument()
    expect(document.querySelector('.project-export img')).toBeNull()
  })
})
