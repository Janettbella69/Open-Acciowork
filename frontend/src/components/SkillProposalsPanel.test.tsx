import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api/client'
import type { SkillProposalDetail, SkillProposalPreview, SkillProposalSummary } from '../api/skillProposals'
import { I18nContext } from '../i18n'
import SkillProposalsPanel from './SkillProposalsPanel'
import SkillsPage from './SkillsPage'

vi.mock('../api/client')

const timestamp = '2026-09-06T00:00:00Z'
const fileContents: Record<string, string> = {
  'SKILL.md': '---\nname: supplier-review\ndescription: Review repeated supplier checks\nversion: 1.0.0\n---\nRead references/evidence.md and templates/report.md.',
  'references/evidence.md': '<script>neverExecute()</script>\n<img src="https://example.invalid/track">\nUse sourced claims.',
  'templates/report.md': '# Editable supplier comparison\nEvidence | Next action',
  'scripts/check.py': 'print("local calculation")',
}

function candidate(id = 'a'): SkillProposalDetail {
  return {
    id: `skill_proposal_${id.repeat(64)}`, revision: 1, state: 'pending',
    project_id: `project_${id}`, project_name: `Source project ${id.toUpperCase()}`,
    source_run_id: `run_${id}`, source_artifact_id: `artifact_${id}`, source_sdk_session_id: `sdk_${id}`,
    manifest_checksum: 'c'.repeat(64),
    manifest: { schema_version: 1, kind: 'ophira.skill-proposal', name: `supplier-review-${id}`,
      version: '1.0.0', package_dir: 'package', reason: `Repeated workflow explanation ${id}`,
      related_run_ids: [`previous_${id}`] },
    skill_id: `ophira-tenant:supplier-review-${id}`, description: `Frozen supplier method ${id}`,
    content_hash: id.repeat(64),
    files: [...Object.entries(fileContents).map(([path, text], index) => ({
      path, size_bytes: text.length, content_hash: String(index + 1).repeat(64), preview_kind: 'text' as const,
    })), { path: 'assets/sample.bin', size_bytes: 3, content_hash: '5'.repeat(64), preview_kind: 'binary' }],
    scan: { skill_id: `ophira-tenant:supplier-review-${id}`, ok: true, checked_at: timestamp,
      checks: [{ name: 'secret_scan', status: 'passed', detail: 'clean' },
        { name: 'permission_frontmatter', status: 'warning', detail: 'Permissions remain controlled by runtime policy.' }] },
    scan_content_hash: id.repeat(64),
    source_runs: [{ id: `previous_${id}`, status: 'completed', prompt_excerpt: `Previous supplier task ${id}`,
      started_at: timestamp, finished_at: timestamp }],
    review: null, history: [], publication_receipt: null,
    created_at: timestamp, updated_at: timestamp,
    target: { current_hash: 'e'.repeat(64), enabled: true, generation: 7 },
    actions: { can_publish: true, can_reject: true, reason: null },
  }
}

function summary(detail: SkillProposalDetail): SkillProposalSummary {
  return { ...detail, scan_ok: detail.scan?.ok === true }
}

function preview(detail: SkillProposalDetail, path: string): SkillProposalPreview {
  const file = detail.files.find((item) => item.path === path)!
  return { proposal_id: detail.id, content_hash: detail.content_hash, path,
    file_content_hash: file.content_hash, size_bytes: file.size_bytes, preview_kind: file.preview_kind,
    text: file.preview_kind === 'text' ? fileContents[path] : null, complete: true }
}

function decided(detail: SkillProposalDetail, decision: 'approve' | 'reject' = 'approve'): SkillProposalDetail {
  const outcome = decision === 'approve' ? 'published' : 'rejected'
  const review = { review_revision: 1, decision, content_hash: detail.content_hash,
    expected_target: decision === 'approve' ? detail.target : null,
    reviewer: { kind: 'principal' as const, id: 'owner@example.invalid', role: 'owner' as const },
    note: 'Reviewed the frozen source.', reviewed_at: timestamp, reviewer_binding: '9'.repeat(64), outcome } as const
  return { ...detail, revision: 2, state: outcome, review, history: [review],
    actions: { can_publish: false, can_reject: false, reason: 'This proposal is no longer awaiting review.' },
    target: decision === 'approve' ? { current_hash: detail.content_hash, enabled: false, generation: 8 } : detail.target,
    publication_receipt: decision === 'approve' ? { proposal_id: detail.id, skill_id: detail.skill_id,
      content_hash: detail.content_hash, review_revision: 1, reviewer_binding: '9'.repeat(64),
      previous_selection: detail.target, published_at: timestamp } : null }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail })
  return { promise, resolve, reject }
}

const a = candidate()
const b = candidate('b')
const onPublished = vi.fn<() => Promise<void>>()

beforeEach(() => {
  vi.resetAllMocks()
  onPublished.mockResolvedValue(undefined)
  vi.mocked(api.errorMessage).mockImplementation((cause) => cause instanceof Error ? cause.message : 'Unknown error')
  vi.mocked(api.listSkillProposals).mockResolvedValue({ items: [summary(a), summary(b)], next_cursor: null })
  vi.mocked(api.getSkillProposal).mockImplementation(async (id) => id === a.id ? a : b)
  vi.mocked(api.getSkillProposalFile).mockImplementation(async (id, path) => preview(id === a.id ? a : b, path))
  vi.mocked(api.skillProposalDownloadUrl).mockImplementation((id, path, hash) => `/api/skill-proposals/${id}/download?${new URLSearchParams({ path, content_hash: hash })}`)
  vi.mocked(api.reviewSkillProposal).mockImplementation(async (id, review) => decided(id === a.id ? a : b, review.decision))
  vi.mocked(api.listSkills).mockResolvedValue([])
})

async function openCandidate(detail = a) {
  fireEvent.click(await screen.findByRole('button', { name: new RegExp(detail.manifest.name) }))
  await screen.findByRole('heading', { name: detail.manifest.name })
  await waitFor(() => expect(screen.getByRole('checkbox')).toBeEnabled())
}

function approve() {
  fireEvent.click(screen.getByRole('checkbox'))
  fireEvent.click(screen.getByRole('button', { name: 'Approve and publish disabled' }))
}

describe('SkillProposalsPanel', () => {
  it('shows real sources, scans and frozen files, rendering text without executing HTML', async () => {
    const { container } = render(<SkillProposalsPanel onPublished={onPublished} />)
    await openCandidate()
    expect(screen.getByText(a.source_run_id)).toBeInTheDocument()
    expect(screen.getByText(a.source_artifact_id)).toBeInTheDocument()
    expect(screen.getByText(a.manifest.reason)).toBeInTheDocument()
    expect(screen.getByText('Permissions remain controlled by runtime policy.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Approve and publish disabled' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /references\/evidence.md/ }))
    await waitFor(() => expect(container.querySelector('pre')?.textContent).toBe(fileContents['references/evidence.md']))
    expect(container.querySelector('script')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
    const link = screen.getByRole('link', { name: 'Download frozen file' })
    expect(link).toHaveAttribute('download')
    expect(link.getAttribute('href')).toContain(`content_hash=${a.content_hash}`)
    expect(api.getSkillProposalFile).toHaveBeenCalledWith(a.id, 'references/evidence.md', a.content_hash, expect.any(AbortSignal))
    fireEvent.click(screen.getByRole('button', { name: /scripts\/check.py/ }))
    expect(await screen.findByText(fileContents['scripts/check.py'])).toBeInTheDocument()
    expect(api.reviewSkillProposal).not.toHaveBeenCalled()
  })

  it('publishes once with exact review and target revisions, then refreshes Installed without enabling', async () => {
    const pending = deferred<SkillProposalDetail>()
    vi.mocked(api.reviewSkillProposal).mockReturnValueOnce(pending.promise)
    render(<SkillProposalsPanel onPublished={onPublished} />)
    await openCandidate()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: ' Inspected every frozen file. ' } })
    fireEvent.click(screen.getByRole('checkbox'))
    const button = screen.getByRole('button', { name: 'Approve and publish disabled' })
    fireEvent.click(button)
    fireEvent.click(button)
    expect(api.reviewSkillProposal).toHaveBeenCalledTimes(1)
    expect(api.reviewSkillProposal).toHaveBeenCalledWith(a.id, {
      decision: 'approve', revision: 1, content_hash: a.content_hash, expected_target: a.target,
      note: 'Inspected every frozen file.',
    })
    await act(async () => { pending.resolve(decided(a)) })
    expect(await screen.findByText('Published with enablement off. Manage this Skill from Installed.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Publication receipt' })).toBeInTheDocument()
    expect(screen.getByText(/owner@example.invalid/)).toBeInTheDocument()
    expect(onPublished).toHaveBeenCalledTimes(1)
    expect(api.setSkillEnabled).not.toHaveBeenCalled()
    expect(api.installSkill).not.toHaveBeenCalled()
    expect(api.startRun).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Approve and publish disabled' })).not.toBeInTheDocument()
  })

  it('requires a rejection reason but permits rejecting an unsafe candidate', async () => {
    const blocked = { ...a, scan: { ...a.scan, ok: false, checks: [{ name: 'dangerous_commands', status: 'failed' as const, detail: 'Remote shell command.' }] },
      actions: { can_publish: false, can_reject: true, reason: 'The frozen package must pass its safety scan before publication.' } }
    vi.mocked(api.getSkillProposal).mockResolvedValue(blocked)
    render(<SkillProposalsPanel onPublished={onPublished} />)
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(a.manifest.name) }))
    expect(await screen.findByText('Remote shell command.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Approve and publish disabled' })).toBeDisabled()
    const reject = screen.getByRole('button', { name: 'Reject proposal' })
    expect(reject).toBeDisabled()
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Remove the remote command.' } })
    fireEvent.click(reject)
    expect(await screen.findByText('The proposal was rejected. Its frozen package and review remain in the record.')).toBeInTheDocument()
    expect(api.reviewSkillProposal).toHaveBeenCalledWith(a.id, {
      decision: 'reject', revision: 1, content_hash: a.content_hash, note: 'Remove the remote command.',
    })
    expect(onPublished).not.toHaveBeenCalled()
  })

  it.each(['missing_scan', 'wrong_scan_hash', 'truncated_instruction', 'binary_instruction', 'preview_identity'])(
    'blocks approval for %s instead of treating a partial review as complete', async (kind) => {
      const detail = candidate()
      if (kind === 'missing_scan') detail.scan = null as unknown as SkillProposalDetail['scan']
      if (kind === 'wrong_scan_hash') detail.scan_content_hash = 'f'.repeat(64)
      if (kind === 'binary_instruction') detail.files[0].preview_kind = 'binary'
      vi.mocked(api.getSkillProposal).mockResolvedValue(detail)
      vi.mocked(api.getSkillProposalFile).mockImplementation(async (_id, path) => ({ ...preview(detail, path),
        ...(kind === 'truncated_instruction' ? { complete: false } : {}),
        ...(kind === 'preview_identity' ? { file_content_hash: 'f'.repeat(64) } : {}),
      }))
      render(<SkillProposalsPanel onPublished={onPublished} />)
      fireEvent.click(await screen.findByRole('button', { name: new RegExp(a.manifest.name) }))
      await screen.findByRole('heading', { name: a.manifest.name })
      await waitFor(() => expect(screen.queryByText('Loading frozen file…')).not.toBeInTheDocument())
      expect(screen.getByRole('checkbox')).toBeDisabled()
      expect(screen.getByRole('button', { name: 'Approve and publish disabled' })).toBeDisabled()
      expect(api.reviewSkillProposal).not.toHaveBeenCalled()
    },
  )

  it('allows inspection by frozen download for binary resources after a complete instruction preview', async () => {
    render(<SkillProposalsPanel onPublished={onPublished} />)
    await openCandidate()
    fireEvent.click(screen.getByRole('button', { name: /assets\/sample.bin/ }))
    expect(await screen.findByText('Binary file. Download the frozen original to inspect it; it will not run here.')).toBeInTheDocument()
    expect(screen.getByRole('checkbox')).toBeEnabled()
    expect(screen.getByRole('link', { name: 'Download frozen file' }).getAttribute('href')).toContain('path=assets%2Fsample.bin')
  })

  it('does not lose preview readiness when the current file is selected again', async () => {
    render(<SkillProposalsPanel onPublished={onPublished} />)
    await openCandidate()
    fireEvent.click(screen.getByRole('button', { name: /^SKILL.md/ }))
    expect(screen.getByRole('checkbox')).toBeEnabled()
    expect(api.getSkillProposalFile).toHaveBeenCalledTimes(1)
  })

  it('shows read-only review capability without offering a usable approval or rejection', async () => {
    vi.mocked(api.getSkillProposal).mockResolvedValue({ ...a, actions: { can_publish: false, can_reject: false,
      reason: 'Only a current project owner or administrator can review this proposal.' } })
    render(<SkillProposalsPanel onPublished={onPublished} />)
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(a.manifest.name) }))
    await screen.findByText('Only a current project owner or administrator can review this proposal.')
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'An unauthorized opinion.' } })
    expect(screen.getByRole('button', { name: 'Reject proposal' })).toBeDisabled()
    expect(screen.getByRole('checkbox')).toBeDisabled()
  })

  it('reloads a 409 conflict, clears confirmation, and requires another explicit decision with the new target', async () => {
    const refreshed = { ...a, revision: 2, target: { current_hash: 'f'.repeat(64), enabled: false, generation: 9 } }
    vi.mocked(api.getSkillProposal).mockResolvedValueOnce(a).mockResolvedValue(refreshed)
    vi.mocked(api.reviewSkillProposal).mockRejectedValueOnce(Object.assign(new Error('Changed'), { status: 409 }))
    render(<SkillProposalsPanel onPublished={onPublished} />)
    await openCandidate()
    approve()
    await screen.findByText('The proposal or installed target changed. Review the refreshed package and confirm again.')
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeEnabled())
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    expect(api.reviewSkillProposal).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Published with enablement off. Manage this Skill from Installed.')).not.toBeInTheDocument()
    approve()
    expect(api.reviewSkillProposal).toHaveBeenLastCalledWith(a.id, {
      decision: 'approve', revision: 2, content_hash: a.content_hash, expected_target: refreshed.target, note: '',
    })
    await screen.findByRole('heading', { name: 'Publication receipt' })
  })

  it.each([403, 404])('clears frozen details after review access fails with %s', async (status) => {
    vi.mocked(api.reviewSkillProposal).mockRejectedValueOnce(Object.assign(new Error('Unavailable'), { status }))
    render(<SkillProposalsPanel onPublished={onPublished} />)
    await openCandidate()
    approve()
    expect(await screen.findByRole('alert')).toHaveTextContent('This proposal is no longer available to you.')
    expect(screen.queryByText(a.manifest.reason)).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Frozen file preview' })).not.toBeInTheDocument()
    expect(onPublished).not.toHaveBeenCalled()
  })

  it('discards late detail and file responses when another candidate is selected', async () => {
    const oldDetail = deferred<SkillProposalDetail>()
    vi.mocked(api.getSkillProposal).mockReturnValueOnce(oldDetail.promise)
    render(<SkillProposalsPanel onPublished={onPublished} />)
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(a.manifest.name) }))
    await openCandidate(b)
    await act(async () => { oldDetail.resolve(a) })
    expect(screen.getByRole('heading', { name: b.manifest.name })).toBeInTheDocument()
    expect(screen.queryByText(a.manifest.reason)).not.toBeInTheDocument()
    const oldFile = deferred<SkillProposalPreview>()
    vi.mocked(api.getSkillProposalFile).mockReturnValueOnce(oldFile.promise)
    fireEvent.click(screen.getByRole('button', { name: /references\/evidence.md/ }))
    await openCandidate(a)
    await act(async () => { oldFile.resolve(preview(b, 'references/evidence.md')) })
    expect(screen.getByRole('heading', { name: a.manifest.name })).toBeInTheDocument()
    expect(screen.queryByText(fileContents['references/evidence.md'])).not.toBeInTheDocument()
  })

  it('keeps an in-flight review bound to its candidate when switching away and back', async () => {
    const oldReview = deferred<SkillProposalDetail>()
    vi.mocked(api.reviewSkillProposal).mockReturnValueOnce(oldReview.promise)
    render(<SkillProposalsPanel onPublished={onPublished} />)
    await openCandidate()
    approve()
    await openCandidate(b)
    fireEvent.click(screen.getByRole('button', { name: new RegExp(a.manifest.name) }))
    await screen.findByRole('heading', { name: a.manifest.name })
    expect(screen.getByRole('checkbox')).toBeDisabled()
    expect(api.reviewSkillProposal).toHaveBeenCalledTimes(1)
    await openCandidate(b)
    await act(async () => { oldReview.resolve(decided(a)) })
    expect(screen.getByRole('heading', { name: b.manifest.name })).toBeInTheDocument()
    expect(screen.queryByText('Published with enablement off. Manage this Skill from Installed.')).not.toBeInTheDocument()
    expect(onPublished).toHaveBeenCalledTimes(1)
  })

  it('consumes opaque cursors and retains newer revisions when pages overlap', async () => {
    vi.mocked(api.listSkillProposals).mockResolvedValueOnce({ items: [summary({ ...a, revision: 3 })], next_cursor: 'opaque:/+?' })
      .mockResolvedValueOnce({ items: [summary(a), summary(b)], next_cursor: null })
    render(<SkillProposalsPanel onPublished={onPublished} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Load more proposals' }))
    await screen.findByRole('button', { name: new RegExp(b.manifest.name) })
    expect(within(screen.getByRole('region', { name: 'Proposal list' })).getAllByRole('button')).toHaveLength(2)
    expect(api.listSkillProposals).toHaveBeenLastCalledWith('pending', 'opaque:/+?', expect.any(AbortSignal))
    expect(screen.queryByRole('button', { name: 'Load more proposals' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: new RegExp(a.manifest.name) }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The proposal response could not be verified.')
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('reconciles an older mounted review after returning to the same candidate', async () => {
    const oldReview = deferred<SkillProposalDetail>()
    vi.mocked(api.reviewSkillProposal).mockReturnValueOnce(oldReview.promise)
    render(<SkillProposalsPanel onPublished={onPublished} />)
    await openCandidate()
    approve()
    await openCandidate(b)
    fireEvent.click(screen.getByRole('button', { name: new RegExp(a.manifest.name) }))
    await screen.findByRole('heading', { name: a.manifest.name })
    expect(screen.getByRole('checkbox')).toBeDisabled()
    vi.mocked(api.getSkillProposal).mockResolvedValue(decided(a))
    await act(async () => { oldReview.resolve(decided(a)) })
    expect(await screen.findByRole('heading', { name: 'Publication receipt' })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(api.reviewSkillProposal).toHaveBeenCalledTimes(1)
    expect(onPublished).toHaveBeenCalledTimes(1)
  })

  it('does not announce a published result without a matching receipt', async () => {
    vi.mocked(api.reviewSkillProposal).mockResolvedValueOnce({ ...decided(a), publication_receipt: null })
    render(<SkillProposalsPanel onPublished={onPublished} />)
    await openCandidate()
    approve()
    await screen.findByText('The review outcome could not be confirmed. Check the refreshed record before trying again.')
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeEnabled())
    expect(screen.getByRole('checkbox')).not.toBeChecked()
    expect(onPublished).not.toHaveBeenCalled()
    expect(screen.queryByRole('heading', { name: 'Publication receipt' })).not.toBeInTheDocument()
  })

  it('ignores an old page after switching proposal states', async () => {
    const old = deferred<Awaited<ReturnType<typeof api.listSkillProposals>>>()
    vi.mocked(api.listSkillProposals).mockReturnValueOnce(old.promise)
      .mockResolvedValueOnce({ items: [summary(decided(b, 'reject'))], next_cursor: null })
    render(<SkillProposalsPanel onPublished={onPublished} />)
    fireEvent.change(screen.getByRole('combobox', { name: 'Proposal state' }), { target: { value: 'rejected' } })
    await screen.findByRole('button', { name: new RegExp(b.manifest.name) })
    await act(async () => { old.resolve({ items: [summary(a)], next_cursor: 'old' }) })
    expect(screen.queryByRole('button', { name: new RegExp(a.manifest.name) })).not.toBeInTheDocument()
    expect(screen.getByRole('combobox')).toHaveValue('rejected')
  })

  it('reports publishing as unfinished and never repeats the publication POST', async () => {
    vi.mocked(api.reviewSkillProposal).mockResolvedValueOnce({ ...a, revision: 2, state: 'publishing',
      actions: { can_publish: false, can_reject: false, reason: 'This proposal is no longer awaiting review.' } })
    render(<SkillProposalsPanel onPublished={onPublished} />)
    await openCandidate()
    approve()
    await screen.findAllByText('Publication is being finalized. Refresh to check the recorded outcome.')
    expect(api.reviewSkillProposal).toHaveBeenCalledTimes(1)
    expect(onPublished).not.toHaveBeenCalled()
    expect(screen.queryByRole('button', { name: 'Approve and publish disabled' })).not.toBeInTheDocument()
  })

  it('preserves the publication receipt if refreshing Installed fails', async () => {
    onPublished.mockRejectedValueOnce(new Error('Offline'))
    render(<SkillProposalsPanel onPublished={onPublished} />)
    await openCandidate()
    approve()
    expect(await screen.findByRole('alert')).toHaveTextContent('The proposal was published, but Installed could not be refreshed.')
    expect(screen.getByRole('heading', { name: 'Publication receipt' })).toBeInTheDocument()
    expect(api.reviewSkillProposal).toHaveBeenCalledTimes(1)
  })

  it('keeps disabled installed Skills separate and refreshes the registry after a human publication', async () => {
    const installed = { id: a.skill_id, namespace: 'ophira-tenant', name: a.manifest.name,
      version: '0.9.0', description: 'Previously published method', content_hash: 'e'.repeat(64), source: 'tenant' as const, enabled: false }
    const initial = deferred<Awaited<ReturnType<typeof api.listSkills>>>()
    vi.mocked(api.listSkills).mockReturnValueOnce(initial.promise).mockResolvedValue([{ ...installed, version: '1.0.0', content_hash: a.content_hash }])
    render(<SkillsPage />)
    expect(api.listSkillProposals).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: 'Proposals' }))
    await openCandidate()
    approve()
    await screen.findByText('Published with enablement off. Manage this Skill from Installed.')
    await act(async () => { initial.resolve([installed]) })
    fireEvent.click(screen.getByRole('button', { name: /Installed/ }))
    const card = (await screen.findByRole('heading', { name: /supplier review a/ })).closest('article')!
    expect(within(card).getByText(/v1.0.0/)).toBeInTheDocument()
    expect(within(card).getByRole('button', { name: 'Enable' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Proposal list' })).not.toBeInTheDocument()
    expect(api.setSkillEnabled).not.toHaveBeenCalled()
  })

  it('renders the review controls and confirmation in Chinese', async () => {
    render(<I18nContext.Provider value="zh-CN"><SkillProposalsPanel onPublished={onPublished} /></I18nContext.Provider>)
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(a.manifest.name) }))
    expect(await screen.findByRole('heading', { name: /^审阅冻结包/ })).toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('checkbox')).toBeEnabled())
    expect(screen.getByRole('button', { name: '批准发布并保持禁用' })).toBeDisabled()
    expect(screen.getByText('我已审阅此冻结包、扫描结果和发布目标。发布后保持禁用。')).toBeInTheDocument()
  })

  it.each(['toggle', 'upload', 'rollback'])('ignores a late Installed %s response after publishing a new disabled version', async (operation) => {
    const old = { id: a.skill_id, namespace: 'ophira-tenant', name: a.manifest.name,
      version: '0.9.0', description: 'Old installed method', content_hash: 'e'.repeat(64), source: 'tenant' as const, enabled: false }
    const older = { ...old, version: '0.8.0', content_hash: 'd'.repeat(64) }
    const installed = { ...old, version: '1.0.0', content_hash: a.content_hash }
    const response = deferred<typeof old>()
    vi.mocked(api.listSkills).mockResolvedValueOnce([old]).mockResolvedValue([installed])
    vi.mocked(api.setSkillEnabled).mockReturnValueOnce(response.promise)
    vi.mocked(api.installSkill).mockReturnValueOnce(response.promise)
    vi.mocked(api.rollbackSkill).mockReturnValueOnce(response.promise)
    vi.mocked(api.listSkillVersions).mockResolvedValue([older, old])
    render(<SkillsPage />)
    const card = (await screen.findByRole('heading', { name: /supplier review a/ })).closest('article')!
    if (operation === 'toggle') fireEvent.click(within(card).getByRole('button', { name: 'Enable' }))
    if (operation === 'upload') fireEvent.change(screen.getByLabelText('Upload skill zip'), {
      target: { files: [new File(['frozen archive'], 'method.zip', { type: 'application/zip' })] },
    })
    if (operation === 'rollback') {
      fireEvent.click(within(card).getByRole('button', { name: 'Versions' }))
      fireEvent.click(await screen.findByRole('button', { name: 'Restore' }))
    }
    fireEvent.click(screen.getByRole('button', { name: 'Proposals' }))
    await openCandidate()
    approve()
    await screen.findByText('Published with enablement off. Manage this Skill from Installed.')
    fireEvent.click(screen.getByRole('button', { name: /Installed/ }))
    await within(screen.getByRole('region', { name: 'Installed TradeMind skills' })).findByText(/v1.0.0/)
    await act(async () => { response.resolve({ ...older, enabled: true }) })
    const current = screen.getByRole('heading', { name: /supplier review a/ }).closest('article')!
    expect(within(current).getByText(/v1.0.0/)).toBeInTheDocument()
    expect(within(current).getByRole('button', { name: 'Enable' })).toBeEnabled()
    expect(within(current).queryByRole('button', { name: 'Disable' })).not.toBeInTheDocument()
    expect(api.listSkillVersions).toHaveBeenCalledTimes(operation === 'rollback' ? 1 : 0)
  })

  it('keeps refreshed history and scan results when old Installed reads arrive after publication', async () => {
    const old = { id: a.skill_id, namespace: 'ophira-tenant', name: a.manifest.name,
      version: '0.9.0', description: 'Old installed method', content_hash: 'e'.repeat(64), source: 'tenant' as const, enabled: false }
    const installed = { ...old, version: '1.0.0', content_hash: a.content_hash }
    const history = deferred<typeof old[]>()
    const scan = deferred<SkillProposalDetail['scan']>()
    vi.mocked(api.listSkills).mockResolvedValueOnce([old]).mockResolvedValue([installed])
    vi.mocked(api.listSkillVersions).mockReturnValueOnce(history.promise).mockResolvedValue([installed])
    vi.mocked(api.testSkill).mockReturnValueOnce(scan.promise)
    render(<SkillsPage />)
    const card = (await screen.findByRole('heading', { name: /supplier review a/ })).closest('article')!
    fireEvent.click(within(card).getByRole('button', { name: 'Versions' }))
    fireEvent.click(within(card).getByRole('button', { name: 'Test' }))
    fireEvent.click(screen.getByRole('button', { name: 'Proposals' }))
    await openCandidate()
    approve()
    await screen.findByText('Published with enablement off. Manage this Skill from Installed.')
    fireEvent.click(screen.getByRole('button', { name: /Installed/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Versions' }))
    await screen.findByText('Current version')
    await act(async () => {
      history.resolve([old])
      scan.resolve({ ...a.scan, checks: [{ name: 'old_snapshot', status: 'passed', detail: 'Old scan evidence' }] })
    })
    expect(screen.getByText('Current version')).toBeInTheDocument()
    expect(screen.queryByText('v0.9.0')).not.toBeInTheDocument()
    expect(screen.queryByText('Old scan evidence')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Test' })).toBeEnabled()
  })

  it('retains a pending human review across Installed tab switches without submitting again', async () => {
    const response = deferred<SkillProposalDetail>()
    vi.mocked(api.reviewSkillProposal).mockReturnValueOnce(response.promise)
    render(<SkillsPage />)
    fireEvent.click(screen.getByRole('button', { name: 'Proposals' }))
    await openCandidate()
    approve()
    fireEvent.click(screen.getByRole('button', { name: /Installed/ }))
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Proposals' }))
    expect(screen.getByRole('checkbox')).toBeDisabled()
    expect(api.reviewSkillProposal).toHaveBeenCalledTimes(1)
    await act(async () => { response.resolve(decided(a)) })
    expect(await screen.findByRole('heading', { name: 'Publication receipt' })).toBeInTheDocument()
    expect(api.reviewSkillProposal).toHaveBeenCalledTimes(1)
  })
})
