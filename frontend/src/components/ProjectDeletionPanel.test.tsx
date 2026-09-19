import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getProjectDeletion, getProjectDeletionOperation, retryProjectDeletion, submitProjectDeletion } from '../api/client'
import type { ProjectDeletionPreview } from '../api/projectDeletion'
import { useProjectDeletion } from '../api/useProjectDeletion'
import { I18nContext } from '../i18n'
import { deferred, deletionOperation, deletionOperationId, deletionPreview } from '../test/projectDeletionFixtures'
import ProjectDeletionPanel from './ProjectDeletionPanel'
import ProjectDeletionProgress from './ProjectDeletionProgress'

vi.mock('../api/client', () => ({
  getProjectDeletion: vi.fn(), getProjectDeletionOperation: vi.fn(), submitProjectDeletion: vi.fn(), retryProjectDeletion: vi.fn(),
  errorMessage: (cause: unknown) => cause instanceof Error ? cause.message : 'Unknown error',
}))
const accepted = vi.fn()
const accessCheck = vi.fn()
function Harness({ projectId = 'project_1' }: { projectId?: string }) {
  const deletion = useProjectDeletion(accepted)
  return <>
    <ProjectDeletionPanel projectId={projectId} pending={!!deletion.attempt}
      canReviewAgain={!!deletion.attempt && !deletion.action && !deletion.attempt.accepted && ['missing', 'rejected', 'forbidden'].includes(deletion.attempt.phase)}
      onReviewAgain={deletion.dismiss} onSubmit={deletion.submit} onAccessCheck={accessCheck} />
    <ProjectDeletionProgress deletion={deletion} />
  </>
}
async function open() {
  fireEvent.click(screen.getByRole('button', { name: 'Delete project' }))
  await screen.findByRole('heading', { name: 'Supplier research' })
}
function confirm() {
  fireEvent.change(screen.getByLabelText('Type the project name to confirm'), { target: { value: 'Supplier research' } })
  fireEvent.change(screen.getByLabelText('Deletion reason'), { target: { value: 'No longer needed' } })
  fireEvent.click(screen.getByRole('checkbox', { name: /submitted deletion is irreversible/ }))
}
beforeEach(() => {
  vi.resetAllMocks()
  window.history.replaceState({}, '', '/app')
  vi.spyOn(crypto, 'randomUUID').mockReturnValue(deletionOperationId)
  vi.mocked(getProjectDeletion).mockResolvedValue(deletionPreview())
  vi.mocked(submitProjectDeletion).mockResolvedValue(deletionOperation())
  vi.mocked(getProjectDeletionOperation).mockResolvedValue(deletionOperation())
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); window.history.replaceState({}, '', '/app') })

describe('project deletion review and progress', () => {
  it('lazily reviews once in StrictMode and requires name, reason and explicit irreversible confirmation', async () => {
    render(<StrictMode><Harness /></StrictMode>)
    expect(getProjectDeletion).not.toHaveBeenCalled()
    await open()
    expect(getProjectDeletion).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Already downloaded copies remain outside the application.')).toBeInTheDocument()
    expect(screen.getByText('12 files · 2,400 bytes')).toBeInTheDocument()
    const submit = screen.getByRole('button', { name: 'Permanently delete project' })
    expect(submit).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Type the project name to confirm'), { target: { value: 'Wrong project' } })
    fireEvent.change(screen.getByLabelText('Deletion reason'), { target: { value: 'No longer needed' } })
    fireEvent.click(screen.getByRole('checkbox', { name: /submitted deletion is irreversible/ }))
    expect(submit).toBeDisabled()
    fireEvent.change(screen.getByLabelText('Type the project name to confirm'), { target: { value: 'Supplier research' } })
    expect(submit).toBeEnabled()
    fireEvent.click(submit); fireEvent.click(submit)
    await screen.findByText('Deletion is in progress. Erasure is not yet complete.')
    expect(submitProjectDeletion).toHaveBeenCalledExactlyOnceWith('project_1', {
      operation_id: deletionOperationId, fingerprint: 'a'.repeat(64), confirmation_name: 'Supplier research', reason: 'No longer needed',
    }, expect.any(AbortSignal))
    expect(screen.queryByText('Application-controlled project content has been deleted.')).not.toBeInTheDocument()
  })

  it.each([false, true])('blocks confirmation for insufficient capability or real blockers (blocker: %s)', async (blocked) => {
    vi.mocked(getProjectDeletion).mockResolvedValueOnce({ ...deletionPreview(), can_delete: blocked,
      blockers: blocked ? [{ code: 'SDK_BUSY', message: 'Native worker is still active.' }] : [] })
    render(<Harness />)
    await open()
    expect(screen.getByRole('button', { name: 'Permanently delete project' })).toBeDisabled()
    if (blocked) expect(screen.getByText('Native worker is still active.')).toBeInTheDocument()
    fireEvent.submit(screen.getByLabelText('Deletion reason').closest('form')!)
    expect(submitProjectDeletion).not.toHaveBeenCalled()
  })

  it('closes a review without submitting and ignores a preview after switching projects', async () => {
    const old = deferred<ProjectDeletionPreview>()
    vi.mocked(getProjectDeletion).mockReturnValueOnce(old.promise)
    const { rerender } = render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Delete project' }))
    await waitFor(() => expect(getProjectDeletion).toHaveBeenCalledTimes(1))
    const signal = vi.mocked(getProjectDeletion).mock.calls[0][1]!
    rerender(<Harness projectId="project_2" />)
    expect(signal.aborted).toBe(true)
    await act(async () => { old.resolve(deletionPreview()) })
    expect(screen.queryByText('Supplier research')).not.toBeInTheDocument()
    expect(submitProjectDeletion).not.toHaveBeenCalled()
  })

  it('rejects a preview for the wrong project and reports access failures without assuming read access is lost', async () => {
    vi.mocked(getProjectDeletion).mockResolvedValueOnce(deletionPreview('other_project'))
    render(<Harness />)
    fireEvent.click(screen.getByRole('button', { name: 'Delete project' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The deletion review did not match this project.')
    expect(screen.queryByRole('button', { name: 'Permanently delete project' })).not.toBeInTheDocument()
    vi.mocked(getProjectDeletion).mockRejectedValueOnce(Object.assign(new Error('Only project owners and administrators may delete'), { status: 403 }))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh deletion review' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Only an authorized project owner or administrator can delete this project.')
    expect(accessCheck).toHaveBeenCalledOnce()
    expect(accepted).not.toHaveBeenCalled()
  })

  it('requires a new explicit review and confirmation after 409 without automatically resubmitting', async () => {
    vi.mocked(submitProjectDeletion).mockRejectedValueOnce(Object.assign(new Error('Project changed; review again'), { status: 409 }))
    render(<Harness />)
    await open(); confirm()
    fireEvent.click(screen.getByRole('button', { name: 'Permanently delete project' }))
    await screen.findByText('Deletion did not start. Review the project again before confirming.')
    expect(screen.queryByLabelText('Deletion reason')).not.toBeInTheDocument()
    expect(accepted).not.toHaveBeenCalled()
    vi.mocked(getProjectDeletion).mockResolvedValueOnce({ ...deletionPreview(), fingerprint: 'b'.repeat(64) })
    fireEvent.click(screen.getByRole('button', { name: 'Review deletion again' }))
    await screen.findByLabelText('Deletion reason')
    expect(screen.getByLabelText('Deletion reason')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Permanently delete project' })).toBeDisabled()
    expect(submitProjectDeletion).toHaveBeenCalledTimes(1)
    confirm(); fireEvent.click(screen.getByRole('button', { name: 'Permanently delete project' }))
    await waitFor(() => expect(submitProjectDeletion).toHaveBeenCalledTimes(2))
    expect(vi.mocked(submitProjectDeletion).mock.calls[1][1].fingerprint).toBe('b'.repeat(64))
  })

  it('shows blocked progress and actual retained copies without claiming deletion has finished', async () => {
    vi.mocked(submitProjectDeletion).mockResolvedValueOnce(deletionOperation('blocked'))
    render(<Harness />)
    await open(); confirm()
    fireEvent.click(screen.getByRole('button', { name: 'Permanently delete project' }))
    await screen.findByText('Deletion is blocked. Erasure is not complete.')
    expect(screen.getByText('The SDK process has not stopped.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry this deletion' })).toBeEnabled()
    expect(screen.getByText('Already downloaded copies remain outside the application.')).toBeInTheDocument()
    expect(screen.queryByText('Application-controlled project content has been deleted.')).not.toBeInTheDocument()
  })

  it('allows a persisted deleting operation to be resumed using its original ID', async () => {
    window.history.replaceState({}, '', `/app?deletion_project=project_1&deletion_operation=${deletionOperationId}`)
    vi.mocked(getProjectDeletionOperation).mockResolvedValueOnce({ ...deletionOperation('deleting'), can_retry: true })
    vi.mocked(retryProjectDeletion).mockResolvedValueOnce(deletionOperation('blocked'))
    render(<Harness />)
    await screen.findByText('Deletion is in progress. Erasure is not yet complete.')
    fireEvent.click(screen.getByRole('button', { name: 'Retry this deletion' }))
    await screen.findByText('Deletion is blocked. Erasure is not complete.')
    expect(retryProjectDeletion).toHaveBeenCalledExactlyOnceWith('project_1', deletionOperationId, expect.any(AbortSignal))
    expect(submitProjectDeletion).not.toHaveBeenCalled()
    expect(screen.queryByText('Application-controlled project content has been deleted.')).not.toBeInTheDocument()
  })

  it('localizes confirmation and a restored final result in Chinese', async () => {
    render(<I18nContext.Provider value="zh-CN"><Harness /></I18nContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: '删除项目' }))
    await screen.findByRole('heading', { name: 'Supplier research' })
    expect(screen.getByText('12 个文件 · 2,400 字节')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('输入项目名称以确认'), { target: { value: 'Supplier research' } })
    fireEvent.change(screen.getByLabelText('删除原因'), { target: { value: '已不再需要' } })
    fireEvent.click(screen.getByRole('checkbox', { name: '我理解提交删除后不可撤销，保留副本需要另行管理。' }))
    vi.mocked(submitProjectDeletion).mockResolvedValueOnce(deletionOperation('deleted'))
    fireEvent.click(screen.getByRole('button', { name: '永久删除项目' }))
    expect(await screen.findByText('应用控制的项目内容已删除。')).toBeInTheDocument()
    expect(screen.getByText('保留的副本与记录')).toBeInTheDocument()
  })

  it('explains forbidden deletion in Chinese without discarding readable project access', async () => {
    vi.mocked(getProjectDeletion).mockRejectedValueOnce(Object.assign(new Error('Deletion operation access is unavailable.'), { status: 403 }))
    render(<I18nContext.Provider value="zh-CN"><Harness /></I18nContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: '删除项目' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('只有获得授权的项目所有者或管理员可以删除此项目。')
    expect(screen.queryByRole('button', { name: '永久删除项目' })).not.toBeInTheDocument()
    expect(submitProjectDeletion).not.toHaveBeenCalled()
    expect(accessCheck).toHaveBeenCalledOnce()
    expect(accepted).not.toHaveBeenCalled()
  })

  it('translates real application and native blockers and retained scope without implying zero retained copies', async () => {
    vi.mocked(getProjectDeletion).mockResolvedValueOnce({ ...deletionPreview(), can_delete: false,
      domains: [
        { id: 'project', label: 'Project, workspace, uploads and checkpoints', files: 3, bytes: 800 },
        { id: 'native-sessions', label: 'Native SDK sessions and file checkpoints', files: 2, bytes: 1000 },
      ],
      retained: [
        { code: 'retained_0', message: 'Shared Provider profiles, credentials, connectors and organization policy are retained.', count: 0 },
        { code: 'retained_1', message: "Other projects' native sessions and shared Provider configuration are retained.", count: 0 },
      ],
      blockers: [
        { code: 'project_active', message: 'Project work is still active. Stop runs and close shop sessions, then refresh this review.' },
        { code: 'native_session_scope_unproven', message: 'Native session ownership could not be fully verified. Resolve the stored session boundary before deleting this project.' },
      ],
    })
    render(<I18nContext.Provider value="zh-CN"><Harness /></I18nContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: '删除项目' }))
    await screen.findByText('项目、工作区、上传文件和检查点')
    expect(screen.getByText('原生 SDK 会话与文件检查点')).toBeInTheDocument()
    expect(screen.getByText('共享的模型服务配置、凭据、连接器和组织策略会保留。')).toBeInTheDocument()
    expect(screen.getByText('其他项目的原生会话和共享的模型服务配置会保留。')).toBeInTheDocument()
    expect(screen.queryByText('数量：0')).not.toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('项目仍有活动任务。请停止运行任务并关闭店铺会话，然后刷新审核。')
    expect(screen.getByRole('alert')).toHaveTextContent('无法完整验证原生会话的归属。请先确认已存储会话的所属范围，再删除此项目。')
    expect(screen.getByRole('button', { name: '永久删除项目' })).toBeDisabled()
  })

  it('translates the actual conflict response and blocked operation while keeping erasure unconfirmed', async () => {
    vi.mocked(getProjectDeletion).mockRejectedValueOnce(Object.assign(new Error('Deletion could not be verified. Refresh the review or check this operation.'), { status: 409 }))
    const { unmount } = render(<I18nContext.Provider value="zh-CN"><Harness /></I18nContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: '删除项目' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('无法验证删除操作。请刷新审核或查询此操作的状态。')
    unmount()
    window.history.replaceState({}, '', `/app?deletion_project=project_1&deletion_operation=${deletionOperationId}`)
    vi.mocked(getProjectDeletionOperation).mockResolvedValueOnce({ ...deletionOperation('blocked'),
      error_code: 'deletion_requires_attention',
      error_message: 'Deletion is incomplete. The project remains unavailable. Resolve active work or changed files, then retry this operation.',
    })
    render(<I18nContext.Provider value="zh-CN"><Harness /></I18nContext.Provider>)
    expect(await screen.findByRole('alert')).toHaveTextContent('删除尚未完成，项目仍不可用。请处理活动任务或已变化的文件，然后重试同一操作。')
    expect(screen.getByRole('button', { name: '重试此删除操作' })).toBeEnabled()
    expect(screen.queryByText('应用控制的项目内容已删除。')).not.toBeInTheDocument()
  })
})
