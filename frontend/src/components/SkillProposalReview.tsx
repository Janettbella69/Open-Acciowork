import { useCallback, useEffect, useRef, useState } from 'react'
import { errorMessage, getSkillProposal, getSkillProposalFile, reviewSkillProposal, skillProposalDownloadUrl } from '../api/client'
import type { SkillProposalDetail, SkillProposalFile, SkillProposalPreview, SkillProposalReviewRequest } from '../api/skillProposals'
import { proposalStateLabels } from '../api/skillProposals'
import { useLanguage, useT } from '../i18n'

function responseStatus(error: unknown): number | undefined {
  return typeof error === 'object' && error !== null && 'status' in error && typeof error.status === 'number'
    ? error.status : undefined
}

type ReviewProps = {
  proposalId: string
  minimumRevision: number
  busy: boolean
  beginReview: (id: string) => boolean
  endReview: (id: string) => void
  onChanged: (proposal: SkillProposalDetail) => void
  onPublished: () => Promise<void>
}

function FrozenFile({ proposal, file, onReady, onUnavailable }: {
  proposal: SkillProposalDetail
  file: SkillProposalFile
  onReady: (ready: boolean, instructionComplete?: boolean) => void
  onUnavailable: (message: string) => void
}) {
  const t = useT()
  const [preview, setPreview] = useState<SkillProposalPreview | null>(null)
  const [error, setError] = useState('')
  useEffect(() => {
    const controller = new AbortController()
    let active = true
    setPreview(null)
    setError('')
    onReady(false)
    void getSkillProposalFile(proposal.id, file.path, proposal.content_hash, controller.signal)
      .then((result) => {
        if (!active) return
        if (result.proposal_id !== proposal.id || result.content_hash !== proposal.content_hash
          || result.path !== file.path || result.file_content_hash !== file.content_hash
          || result.size_bytes !== file.size_bytes || result.preview_kind !== file.preview_kind
          || typeof result.complete !== 'boolean'
          || (result.preview_kind === 'text' ? typeof result.text !== 'string' : result.text !== null)) {
          throw new Error('The file preview does not match the frozen package. Refresh the proposal.')
        }
        setPreview(result)
        onReady(true, file.path === 'SKILL.md'
          ? result.preview_kind === 'text' && result.complete : undefined)
      })
      .catch((cause) => {
        if (!active) return
        onReady(false, file.path === 'SKILL.md' ? false : undefined)
        if ([403, 404].includes(responseStatus(cause) ?? 0)) {
          onUnavailable('This proposal is no longer available to you. Refresh the proposal list.')
        } else setError(errorMessage(cause))
      })
    return () => { active = false; controller.abort() }
  }, [proposal.id, proposal.content_hash, file, onReady, onUnavailable])

  return <section className="skill-proposal-preview" aria-label={t('Frozen file preview')}>
    <header><strong>{file.path}</strong><span>{t('{n} bytes', { n: file.size_bytes })}</span></header>
    <code className="skill-proposal-hash">{file.content_hash}</code>
    {error ? <p role="alert">{t(error)}</p> : !preview ? <p role="status">{t('Loading frozen file…')}</p>
      : preview.preview_kind === 'binary' ? <p>{t('Binary file. Download the frozen original to inspect it; it will not run here.')}</p>
        : <>
          {!preview.complete && <p role="status">{t('Partial preview. Download the frozen original to review the complete file.')}</p>}
          <pre tabIndex={0}>{preview.text}</pre>
        </>}
    <a href={skillProposalDownloadUrl(proposal.id, file.path, proposal.content_hash)} download>
      {t('Download frozen file')}
    </a>
  </section>
}

function ReviewPackage({ proposal, busy, beginReview, endReview, onChanged, onPublished, onRefresh, onReviewed, onUnavailable }: Omit<ReviewProps, 'proposalId'> & {
  proposal: SkillProposalDetail
  onRefresh: (message?: string) => void
  onReviewed: (proposal: SkillProposalDetail, message: string, error: string) => void
  onUnavailable: (message: string) => void
}) {
  const t = useT()
  const language = useLanguage()
  const [path, setPath] = useState('SKILL.md')
  const [instructionReady, setInstructionReady] = useState(false)
  const [fileReady, setFileReady] = useState(false)
  const [confirmed, setConfirmed] = useState(false)
  const [note, setNote] = useState('')
  const active = useRef(false)
  useEffect(() => { active.current = true; return () => { active.current = false } }, [])
  const previewReady = useCallback((ready: boolean, instructionComplete?: boolean) => {
    setFileReady(ready)
    if (instructionComplete !== undefined) setInstructionReady(instructionComplete)
  }, [])
  const file = proposal.files.find((item) => item.path === path)
  const scanPassed = proposal.scan?.ok === true && proposal.scan_content_hash === proposal.content_hash
    && Array.isArray(proposal.scan.checks) && !proposal.scan.checks.some((check) => check.status === 'failed')
  const canInspectAndApprove = proposal.state === 'pending' && proposal.actions.can_publish
    && scanPassed && instructionReady && fileReady
  const canReject = proposal.state === 'pending' && proposal.actions.can_reject && Boolean(note.trim())
  const when = (value: string) => new Date(value).toLocaleString(language)

  const submit = async (decision: SkillProposalReviewRequest['decision']) => {
    if (busy || (decision === 'approve' ? !canInspectAndApprove || !confirmed : !canReject)
      || !beginReview(proposal.id)) return
    const payload: SkillProposalReviewRequest = {
      decision, revision: proposal.revision, content_hash: proposal.content_hash, note: note.trim(),
      ...(decision === 'approve' ? { expected_target: proposal.target } : {}),
    }
    try {
      const updated = await reviewSkillProposal(proposal.id, payload)
      if (updated.id !== proposal.id || updated.project_id !== proposal.project_id
        || updated.content_hash !== proposal.content_hash || updated.revision < proposal.revision) {
        throw new Error('The review response could not be verified. Refresh to check its recorded outcome.')
      }
      if (updated.state === 'published' && (!updated.publication_receipt
        || updated.publication_receipt.proposal_id !== proposal.id
        || updated.publication_receipt.skill_id !== proposal.skill_id
        || updated.publication_receipt.content_hash !== proposal.content_hash)) {
        throw new Error('The review response could not be verified. Refresh to check its recorded outcome.')
      }
      let refreshError = ''
      if (updated.state === 'published') {
        try { await onPublished() } catch {
          refreshError = 'The proposal was published, but Installed could not be refreshed. Refresh that view before enabling it.'
        }
      }
      if (!active.current) { onChanged(updated); return }
      setConfirmed(false)
      if (updated.state === 'published' || updated.state === 'rejected') {
        onReviewed(updated, updated.state === 'published'
          ? 'Published with enablement off. Manage this Skill from Installed.'
          : 'The proposal was rejected. Its frozen package and review remain in the record.', refreshError)
      } else {
        onReviewed(updated, 'Publication is being finalized. Refresh to check the recorded outcome.', '')
      }
      onChanged(updated)
    } catch (cause) {
      if (!active.current) return
      setConfirmed(false)
      const status = responseStatus(cause)
      if (status === 403 || status === 404) {
        onUnavailable('This proposal is no longer available to you. Refresh the proposal list.')
      } else {
        onRefresh(status === 409
          ? 'The proposal or installed target changed. Review the refreshed package and confirm again.'
          : 'The review outcome could not be confirmed. Check the refreshed record before trying again.')
      }
    } finally { endReview(proposal.id) }
  }

  const history = proposal.history.length ? proposal.history : proposal.review ? [proposal.review] : []
  return <>
    <header className="skill-proposal-detail-heading">
      <div><span>{t(proposalStateLabels[proposal.state])} · v{proposal.manifest.version}</span><h3>{proposal.manifest.name}</h3></div>
      <button type="button" disabled={busy} onClick={() => onRefresh()}>{t('Refresh proposal')}</button>
    </header>
    <p>{proposal.description}</p>
    <dl className="skill-proposal-facts">
      <div><dt>{t('Source project')}</dt><dd>{proposal.project_name} <code>{proposal.project_id}</code></dd></div>
      <div><dt>{t('Source task')}</dt><dd><code>{proposal.source_run_id}</code></dd></div>
      <div><dt>{t('Source artifact')}</dt><dd><code>{proposal.source_artifact_id}</code></dd></div>
      <div><dt>{t('Submitted')}</dt><dd>{when(proposal.created_at)}</dd></div>
      <div><dt>{t('Proposed Skill')}</dt><dd><code>{proposal.skill_id}</code></dd></div>
      <div><dt>{t('Frozen package hash')}</dt><dd><code>{proposal.content_hash}</code></dd></div>
    </dl>
    <section className="skill-proposal-source"><h4>{t('Agent proposal reason')}</h4><p>{proposal.manifest.reason}</p>
      {proposal.source_runs.length > 0 && <details><summary>{t('Related recorded tasks')} ({proposal.source_runs.length})</summary>
        {proposal.source_runs.map((source) => <article key={source.id}>
          <code>{source.id}</code><span>{t(source.status)} · {when(source.started_at)}</span><p>{source.prompt_excerpt}</p>
        </article>)}
      </details>}
    </section>
    <section className="skill-proposal-scan" aria-label={t('Candidate package scan')} data-ok={scanPassed}>
      <h4>{t(scanPassed ? 'Frozen package scan passed' : 'Publication blocked by package scan')}</h4>
      {proposal.scan && <><small>{t('Checked')} {when(proposal.scan.checked_at)}</small><ul>
        {proposal.scan.checks.map((check) => <li key={check.name} data-status={check.status}>
          <strong>{t(check.status)} · {check.name}</strong><span>{check.detail}</span>
        </li>)}
      </ul></>}
      {proposal.scan_content_hash !== proposal.content_hash && <p>{t('The scan does not match this frozen package. Refresh before reviewing.')}</p>}
    </section>
    <section className="skill-proposal-package" aria-label={t('Frozen package files')}>
      <h4>{t('Review the frozen package')} <span>({proposal.files.length})</span></h4>
      <div className="skill-proposal-files">
        <nav aria-label={t('Package files')}>{proposal.files.map((item) => <button type="button" key={item.path}
          aria-pressed={path === item.path} disabled={busy} onClick={() => {
            if (path === item.path) return
            setPath(item.path); setFileReady(false); setConfirmed(false)
          }}>
          <span>{item.path}</span><small>{t('{n} bytes', { n: item.size_bytes })} · {t(item.preview_kind === 'text' ? 'Text' : 'Binary')}</small>
        </button>)}</nav>
        {file ? <FrozenFile key={file.path} proposal={proposal} file={file} onReady={previewReady} onUnavailable={onUnavailable} />
          : <p role="alert">{t('SKILL.md is missing from the frozen package.')}</p>}
      </div>
      {!instructionReady && <p>{t('Approval requires a complete text preview of SKILL.md. If it is too large or binary, submit a shorter text instruction file in a new proposal.')}</p>}
    </section>
    <section className="skill-proposal-target">
      <h4>{t('Publication target')}</h4>
      {proposal.target.current_hash ? <>
        <p>{t('Publishing replaces the selected tenant version and turns its enablement off.')}</p>
        <code>{proposal.target.current_hash}</code><span>{t(proposal.target.enabled ? 'Enabled' : 'Disabled')}</span>
      </> : <p>{t('A new tenant Skill will be published with enablement off.')}</p>}
      <p>{t('Existing Run snapshots keep their pinned versions. Enablement is a separate action in Installed.')}</p>
    </section>
    {proposal.actions.reason && <p className="skill-proposal-access" role="status">{t(proposal.actions.reason)}</p>}
    {proposal.state === 'pending' && <section className="skill-proposal-actions" aria-label={t('Human review decision')}>
      <label>{t('Review note (required for rejection)')}<textarea value={note} maxLength={4000} disabled={busy}
        onChange={(event) => setNote(event.target.value)} /></label>
      <label className="skill-proposal-confirm"><input type="checkbox" checked={confirmed} disabled={busy || !canInspectAndApprove}
        onChange={(event) => setConfirmed(event.target.checked)} />
        {t('I reviewed this frozen package, its scan and the publication target. Publish it with enablement off.')}
      </label>
      <div><button type="button" disabled={busy || !canReject} onClick={() => void submit('reject')}>{t('Reject proposal')}</button>
        <button type="button" disabled={busy || !canInspectAndApprove || !confirmed} onClick={() => void submit('approve')}>
          {busy ? t('Saving review…') : t('Approve and publish disabled')}
        </button></div>
    </section>}
    {proposal.state === 'publishing' && <p role="status">{t('Publication is being finalized. Refresh to check the recorded outcome.')}</p>}
    {proposal.publication_receipt && <section className="skill-proposal-receipt"><h4>{t('Publication receipt')}</h4>
      <p>{when(proposal.publication_receipt.published_at)}</p><code>{proposal.publication_receipt.content_hash}</code>
    </section>}
    {history.length > 0 && <section className="skill-proposal-history"><h4>{t('Human review record')}</h4>
      {history.map((review, index) => <article key={`${review.review_revision}:${review.outcome}:${index}`}>
        <strong>{t(review.decision === 'approve' ? 'Approved' : 'Rejected')} · {t(review.outcome)}</strong>
        <span>{review.reviewer.id} · {t(review.reviewer.role)} · {when(review.reviewed_at)}</span>
        <p>{review.note || t('No review note.')}</p>
      </article>)}
    </section>}
  </>
}

export default function SkillProposalReview(props: ReviewProps) {
  const { proposalId, onChanged } = props
  const t = useT()
  const [detail, setDetail] = useState<SkillProposalDetail | null>(null)
  const [reload, setReload] = useState(0)
  const [message, setMessage] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const minimumRevision = useRef(props.minimumRevision)
  minimumRevision.current = props.minimumRevision
  const refresh = useCallback((notice = '') => { setMessage(notice); setDetail(null); setReload((value) => value + 1) }, [])
  const reviewed = useCallback((proposal: SkillProposalDetail, notice: string, failure: string) => {
    setDetail(proposal); setMessage(notice); setError(failure)
  }, [])
  const unavailable = useCallback((notice: string) => { setDetail(null); setMessage(''); setError(notice); setLoading(false) }, [])
  useEffect(() => {
    if (detail && detail.revision < props.minimumRevision) {
      refresh('The proposal or installed target changed. Review the refreshed package and confirm again.')
    }
  }, [detail, props.minimumRevision, refresh])
  useEffect(() => {
    let active = true
    const controller = new AbortController()
    setLoading(true)
    setError('')
    void getSkillProposal(proposalId, controller.signal)
      .then((proposal) => {
        if (!active) return
        if (proposal.id !== proposalId || proposal.revision < minimumRevision.current) {
          throw new Error('The proposal response could not be verified. Refresh the list.')
        }
        setDetail(proposal)
        onChanged(proposal)
      })
      .catch((cause) => {
        if (!active) return
        setDetail(null)
        setError([403, 404].includes(responseStatus(cause) ?? 0)
          ? 'This proposal is no longer available to you. Refresh the proposal list.' : errorMessage(cause))
      })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false; controller.abort() }
  }, [proposalId, onChanged, reload])

  return <section className="skill-proposal-detail" aria-label={t('Skill proposal review')} aria-busy={loading}>
    {message && <p className="skill-proposal-notice" role="status">{t(message)}</p>}
    {error && <p role="alert">{t(error)}</p>}
    {loading ? <p role="status">{t('Loading frozen proposal…')}</p> : detail
      ? <ReviewPackage key={`${detail.id}:${detail.revision}:${detail.target.generation}`} {...props} proposal={detail}
        onRefresh={refresh} onReviewed={reviewed} onUnavailable={unavailable} />
      : <button type="button" onClick={() => refresh()}>{t('Refresh proposal')}</button>}
  </section>
}
