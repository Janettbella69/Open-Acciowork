import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { CollaborationCapabilities, CommentThread, ProjectAccess, ProjectComment } from '../api/collaboration'
import type { OpportunityProject } from '../api/types'
import { I18nContext } from '../i18n'
import ProjectCollaborationPanel from './ProjectCollaborationPanel'

vi.mock('../api/client', () => ({
  getCollaborationCapabilities: vi.fn(), getProjectAccess: vi.fn(), grantProjectAccess: vi.fn(), revokeProjectAccess: vi.fn(),
  listProjectComments: vi.fn(), getCommentThread: vi.fn(), createProjectComment: vi.fn(), changeProjectComment: vi.fn(), getCommentHistory: vi.fn(),
  artifactDownloadUrl: (project: string, file: string) => `/api/projects/${project}/artifacts/${file}/download`,
  errorMessage: (reason: unknown) => String(reason instanceof Error ? reason.message : reason),
}))
import { changeProjectComment, createProjectComment, getCollaborationCapabilities, getCommentHistory, getCommentThread, getProjectAccess, grantProjectAccess, listProjectComments, revokeProjectAccess } from '../api/client'

const project: OpportunityProject = {
  id: 'project_1', name: 'Shared research', objective: 'Review', product_brief: '', target_markets: [], constraints: [],
  stage: 'discovery', hypotheses: [], evidence: [], assessments: {}, stage_decisions: [], decisions: [], artifacts: [],
  revision: 1, created_at: '2026-09-10T00:00:00Z', updated_at: '2026-09-10T00:00:00Z',
}
const capabilities: CollaborationCapabilities = {
  project_id: project.id, identity_mode: 'trusted_gateway', actor_id: 'owner@example.com', role: 'owner',
  can_comment: true, can_manage_members: true, grantable_roles: ['admin', 'member', 'reviewer', 'viewer'],
}
const root: ProjectComment = {
  id: 'comment_1', project_id: project.id, sequence: 1, parent_id: null, author_id: capabilities.actor_id,
  text: 'Original <script>quote</script>', revision: 1, state: 'open', created_at: project.created_at, updated_at: project.updated_at, reply_count: 0,
  target: { label: 'Supplier quote', reference: { kind: 'artifact', id: 'artifact_1' }, artifact_version: 1, artifact_checksum: 'a'.repeat(64), trace_id: null },
  actions: { edit: true, remove: true, resolve: true, history: true },
}
const access: ProjectAccess = {
  project_id: project.id, tenant_id: 'tenant', owner_principal_id: capabilities.actor_id, revision: 3,
  created_at: project.created_at, updated_at: project.updated_at,
  memberships: {
    [capabilities.actor_id]: { principal_id: capabilities.actor_id, role: 'owner', granted_by: capabilities.actor_id, granted_at: project.created_at },
    'member@example.com': { principal_id: 'member@example.com', role: 'member', granted_by: capabilities.actor_id, granted_at: project.created_at },
  },
}
function view(item = root): CommentThread { return { project_id: project.id, root: item, replies: [], next_after: null } }
function props() { return { project, currentRun: null, events: [] } }
function open() { fireEvent.click(screen.getByRole('button', { name: 'Team and comments' })) }
async function discussion() {
  open()
  fireEvent.click(await screen.findByRole('button', { name: /Supplier quote Original/ }))
  return screen.findByRole('region', { name: 'Discussion thread' })
}
async function members() {
  open(); fireEvent.click(await screen.findByRole('button', { name: 'Project members' }))
  return screen.findByRole('textbox', { name: 'Organization account ID' })
}
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(getCollaborationCapabilities).mockResolvedValue(capabilities)
  vi.mocked(listProjectComments).mockResolvedValue({ project_id: project.id, items: [root], next_before: null })
  vi.mocked(getCommentThread).mockImplementation(async (_projectId, commentId) => view({ ...root, id: commentId }))
  vi.mocked(createProjectComment).mockResolvedValue({ ...root, id: 'comment_new', sequence: 2, text: 'Draft' })
  vi.mocked(changeProjectComment).mockResolvedValue({ ...root, revision: 2, text: 'My revision' })
  vi.mocked(getProjectAccess).mockResolvedValue(access)
  vi.mocked(grantProjectAccess).mockResolvedValue({ ...access, revision: 4 })
  vi.mocked(revokeProjectAccess).mockResolvedValue({ ...access, revision: 4, memberships: { [capabilities.actor_id]: access.memberships[capabilities.actor_id] } })
  vi.mocked(getCommentHistory).mockResolvedValue({ project_id: project.id, comment_id: root.id,
    revisions: [{ revision: 1, actor_id: root.author_id, action: 'create', text: root.text, state: root.state, at: root.created_at }] })
})
afterEach(() => vi.useRealTimers())

it('loads only when opened and renders a saved file version and plain comment text', async () => {
  const { container } = render(<ProjectCollaborationPanel {...props()} />)
  expect(getCollaborationCapabilities).not.toHaveBeenCalled()
  const thread = await discussion()
  expect(within(thread).getByRole('link', { name: 'Download discussed file version' })).toHaveAttribute('href', '/api/projects/project_1/artifacts/artifact_1/download')
  expect(within(thread).getByText('Original <script>quote</script>')).toBeInTheDocument()
  expect(container.querySelector('script')).toBeNull()
  fireEvent.click(within(thread).getByRole('button', { name: 'Comment history' }))
  expect(getCommentHistory).toHaveBeenCalledOnce()
  expect(await within(thread).findByRole('list', { name: 'Comment history' })).toHaveTextContent(root.text!)
})

it('keeps a failed draft and reuses its key after a lost response without double posting', async () => {
  vi.mocked(createProjectComment).mockRejectedValueOnce(new Error('Connection lost'))
  render(<ProjectCollaborationPanel {...props()} />)
  open()
  fireEvent.change(await screen.findByRole('textbox', { name: 'Comment text' }), { target: { value: 'Draft' } })
  fireEvent.click(screen.getByRole('button', { name: 'Post comment' }))
  await screen.findByRole('alert')
  expect(screen.getByRole('textbox', { name: 'Comment text' })).toHaveValue('Draft')
  const firstKey = vi.mocked(createProjectComment).mock.calls[0][1].request_id
  fireEvent.click(screen.getByRole('button', { name: 'Post comment' }))
  expect(screen.getByRole('button', { name: 'Saving…' })).toBeDisabled()
  await waitFor(() => expect(createProjectComment).toHaveBeenCalledTimes(2))
  expect(vi.mocked(createProjectComment).mock.calls[1][1].request_id).toBe(firstKey)
  expect(vi.mocked(createProjectComment).mock.calls[1][1]).not.toHaveProperty('author_id')
})

it('keeps an edited draft on conflict and requires explicit rebasing onto the newest revision', async () => {
  vi.mocked(changeProjectComment).mockRejectedValueOnce(Object.assign(new Error('Comment changed; reload before editing'), { status: 409 }))
  const updated = { ...root, revision: 2, text: 'Changed by another session' }
  render(<ProjectCollaborationPanel {...props()} />)
  const thread = await discussion()
  fireEvent.click(within(thread).getByRole('button', { name: 'Edit comment' }))
  const editor = within(thread).getByRole('group', { name: 'Edit comment' })
  fireEvent.change(within(editor).getByRole('textbox'), { target: { value: 'My revision' } })
  fireEvent.click(within(editor).getByRole('button', { name: 'Save comment' }))
  await within(editor).findByRole('alert')
  expect(within(editor).getByRole('textbox')).toHaveValue('My revision')
  vi.mocked(getCommentThread).mockResolvedValue(view(updated))
  fireEvent.click(within(thread).getByRole('button', { name: 'Refresh discussion' }))
  fireEvent.click(await within(editor).findByRole('button', { name: 'Apply draft to this version' }))
  vi.mocked(changeProjectComment).mockResolvedValue({ ...updated, revision: 3, text: 'My revision' })
  fireEvent.click(within(editor).getByRole('button', { name: 'Save comment' }))
  await waitFor(() => expect(changeProjectComment).toHaveBeenCalledTimes(2))
  expect(vi.mocked(changeProjectComment).mock.calls[0][2].expected_revision).toBe(1)
  expect(vi.mocked(changeProjectComment).mock.calls[1][2].expected_revision).toBe(2)
})

it('requires confirmation to remove a comment and keeps its history control', async () => {
  vi.mocked(changeProjectComment).mockResolvedValue({ ...root, revision: 2, text: null, actions: { ...root.actions, edit: false, remove: false } })
  render(<ProjectCollaborationPanel {...props()} />)
  const thread = await discussion()
  fireEvent.click(within(thread).getByRole('button', { name: 'Remove comment' }))
  expect(changeProjectComment).not.toHaveBeenCalled()
  fireEvent.click(within(thread).getByRole('button', { name: 'Confirm comment removal' }))
  await within(thread).findByText('Comment removed')
  expect(within(thread).getByRole('button', { name: 'Comment history' })).toBeInTheDocument()
  expect(vi.mocked(changeProjectComment).mock.calls[0][2]).toMatchObject({ action: 'remove', expected_revision: 1 })
})

it('lets reviewers reply with inherited targets while viewers get no comment or member controls', async () => {
  vi.mocked(getCollaborationCapabilities).mockResolvedValue({ ...capabilities, role: 'reviewer', can_manage_members: false, grantable_roles: [] })
  vi.mocked(getCommentThread).mockResolvedValue(view({ ...root, actions: { edit: false, remove: false, resolve: false, history: false } }))
  vi.mocked(createProjectComment).mockResolvedValue({ ...root, id: 'reply_1', parent_id: root.id, sequence: 2, text: 'Another quote needed' })
  const { unmount } = render(<ProjectCollaborationPanel {...props()} />)
  const thread = await discussion()
  expect(screen.queryByRole('button', { name: 'Project members' })).not.toBeInTheDocument()
  const reply = within(thread).getByRole('group', { name: 'Write a reply' })
  fireEvent.change(within(reply).getByRole('textbox'), { target: { value: 'Another quote needed' } })
  fireEvent.click(within(reply).getByRole('button', { name: 'Post reply' }))
  await within(thread).findByText('Another quote needed')
  expect(vi.mocked(createProjectComment).mock.calls[0][1]).toMatchObject({ parent_id: root.id, anchor: { kind: 'project' } })
  unmount()
  vi.mocked(getCollaborationCapabilities).mockResolvedValue({ ...capabilities, role: 'viewer', can_comment: false, can_manage_members: false, grantable_roles: [] })
  render(<ProjectCollaborationPanel {...props()} />)
  await discussion()
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Project members' })).not.toBeInTheDocument()
})

it('reviews membership changes against the loaded revision and never edits the owner role', async () => {
  render(<ProjectCollaborationPanel {...props()} />)
  const input = await members()
  expect(screen.getAllByRole('button', { name: 'Change role' })).toHaveLength(1)
  fireEvent.change(input, { target: { value: 'new@example.com' } })
  fireEvent.change(screen.getByRole('combobox', { name: 'Project role' }), { target: { value: 'reviewer' } })
  fireEvent.click(screen.getByRole('button', { name: 'Review access change' }))
  expect(grantProjectAccess).not.toHaveBeenCalled()
  expect(screen.getByText('Set project access to reviewer.')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm access change' }))
  await screen.findByText('Project membership saved.')
  expect(grantProjectAccess).toHaveBeenCalledWith(project.id, { principal_id: 'new@example.com', role: 'reviewer', expected_revision: 3 }, expect.any(AbortSignal))
})

it('preserves a conflicting membership change for review and revokes only on confirmation', async () => {
  vi.mocked(grantProjectAccess).mockRejectedValueOnce(Object.assign(new Error('Project membership changed; reload before saving'), { status: 409 }))
  render(<ProjectCollaborationPanel {...props()} />)
  const input = await members()
  fireEvent.change(input, { target: { value: 'new@example.com' } })
  fireEvent.click(screen.getByRole('button', { name: 'Review access change' }))
  fireEvent.click(screen.getByRole('button', { name: 'Confirm access change' }))
  await screen.findByRole('alert')
  expect(input).toHaveValue('new@example.com')
  let finishRefresh!: (value: ProjectAccess) => void
  vi.mocked(getProjectAccess).mockImplementationOnce(() => new Promise((resolve) => { finishRefresh = resolve }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Refresh members' })).toBeEnabled())
  fireEvent.click(screen.getByRole('button', { name: 'Refresh members' }))
  expect(screen.getByRole('button', { name: 'Remove access' })).toBeDisabled()
  expect(revokeProjectAccess).not.toHaveBeenCalled()
  await act(async () => finishRefresh({ ...access, revision: 5 }))
  await waitFor(() => expect(screen.getByRole('button', { name: 'Remove access' })).toBeEnabled())
  expect(screen.queryByRole('button', { name: 'Confirm access change' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Remove access' }))
  expect(revokeProjectAccess).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm access change' }))
  await screen.findByText('Project membership saved.')
  expect(revokeProjectAccess).toHaveBeenCalledWith(project.id, 'member@example.com', 5, expect.any(AbortSignal))
})

it('clears discussions and private drafts when a later permission check is denied', async () => {
  vi.useFakeTimers()
  render(<ProjectCollaborationPanel {...props()} />)
  open(); await act(async () => {})
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Private draft' } })
  vi.mocked(getCollaborationCapabilities).mockRejectedValue(Object.assign(new Error('Denied'), { status: 403 }))
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
  expect(screen.getByRole('alert')).toHaveTextContent('Project collaboration access changed')
  expect(screen.queryByDisplayValue('Private draft')).not.toBeInTheDocument()
  expect(screen.queryByText('Supplier quote')).not.toBeInTheDocument()
  const calls = vi.mocked(getCollaborationCapabilities).mock.calls.length
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
  expect(getCollaborationCapabilities).toHaveBeenCalledTimes(calls)
})

it('does not reopen a revoked project when an earlier capability read finishes late', async () => {
  vi.useFakeTimers()
  render(<ProjectCollaborationPanel {...props()} />)
  open(); await act(async () => {})
  let finish!: (value: CollaborationCapabilities) => void
  vi.mocked(getCollaborationCapabilities).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  vi.mocked(listProjectComments).mockRejectedValueOnce(Object.assign(new Error('Denied'), { status: 403 }))
  await act(async () => { await vi.advanceTimersByTimeAsync(15_000) })
  expect(screen.getByRole('alert')).toHaveTextContent('Project collaboration access changed')
  await act(async () => finish(capabilities))
  expect(screen.queryByText('Supplier quote')).not.toBeInTheDocument()
  expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
})

it('keeps older reply drafts mounted while polling every page the user opened', async () => {
  const replies = Array.from({ length: 51 }, (_, index) => ({ ...root, id: `reply_${index}`, parent_id: root.id,
    sequence: index + 2, text: `Reply ${index}`, actions: { ...root.actions, resolve: false } }))
  vi.mocked(getCommentThread).mockImplementation(async (_project, _id, after) => ({ ...view(),
    replies: after ? replies.slice(50) : replies.slice(0, 50), next_after: after ? null : 51 }))
  render(<ProjectCollaborationPanel {...props()} />)
  const thread = await discussion()
  fireEvent.click(within(thread).getByRole('button', { name: 'Load more replies' }))
  const last = (await within(thread).findByText('Reply 50')).closest('article')!
  fireEvent.click(within(last).getByRole('button', { name: 'Edit comment' }))
  fireEvent.change(within(last).getByRole('textbox'), { target: { value: 'Older reply draft' } })
  fireEvent.click(within(thread).getByRole('button', { name: 'Refresh discussion' }))
  await waitFor(() => expect(getCommentThread).toHaveBeenCalledTimes(4))
  expect(screen.getByDisplayValue('Older reply draft')).toBeInTheDocument()
})

it('ignores a late read or write after switching projects and aborts its requests', async () => {
  let finish!: (comment: ProjectComment) => void
  vi.mocked(createProjectComment).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
  const { rerender } = render(<ProjectCollaborationPanel {...props()} />)
  open()
  fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'Private draft' } })
  fireEvent.click(screen.getByRole('button', { name: 'Post comment' }))
  const signal = vi.mocked(createProjectComment).mock.calls[0][2]
  rerender(<ProjectCollaborationPanel {...props()} project={{ ...project, id: 'project_2' }} />)
  await act(async () => finish(root))
  expect(signal?.aborted).toBe(true)
  expect(screen.queryByText(root.text!)).not.toBeInTheDocument()
  expect(getCommentThread).not.toHaveBeenCalled()
})

it('rejects a cross-project response before displaying its contents', async () => {
  vi.mocked(listProjectComments).mockResolvedValue({ project_id: project.id, items: [{ ...root, project_id: 'other' }], next_before: null })
  render(<ProjectCollaborationPanel {...props()} />)
  open()
  expect(await screen.findByRole('alert')).toHaveTextContent('did not match this project')
  expect(screen.queryByText(root.text!)).not.toBeInTheDocument()
})

it('supports Strict Mode effect restarts and explains local-only collaboration in Chinese', async () => {
  vi.mocked(getCollaborationCapabilities).mockResolvedValue({ ...capabilities, identity_mode: 'local_only', role: null, can_manage_members: false, grantable_roles: [] })
  render(<StrictMode><I18nContext.Provider value="zh-CN"><ProjectCollaborationPanel {...props()} /></I18nContext.Provider></StrictMode>)
  fireEvent.click(screen.getByRole('button', { name: '团队与评论' }))
  await screen.findByRole('button', { name: '发布评论' })
  expect(screen.getByText(/此本地项目的评论保存在这台电脑上/)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: '项目成员' })).not.toBeInTheDocument()
})
