import { useCallback, useEffect, useRef, useState } from 'react'
import { artifactDownloadUrl, changeProjectComment, createProjectComment, getCommentHistory, getCommentThread, listProjectComments } from '../api/client'
import { requireCollaborationScope, type CollaborationCapabilities, type CommentAnchor, type CommentHistory, type CommentThread, type ProjectComment } from '../api/collaboration'
import type { OpportunityProject, OphiraEvent, RunRecord } from '../api/types'
import { useT } from '../i18n'
import { useCollaborationRequest, useCommentRequestKey } from './useCollaborationRequest'

type Common = { projectId: string; onUnavailable: () => void }
type AnchorOption = { key: string; label: string; anchor: CommentAnchor }

function mergeComments(previous: ProjectComment[], incoming: ProjectComment[]) {
  const result = new Map(previous.map((item) => [item.id, item]))
  for (const item of incoming) {
    const old = result.get(item.id)
    if (!old || old.revision <= item.revision) result.set(item.id, item)
  }
  return [...result.values()].sort((a, b) => a.sequence - b.sequence)
}

function useDiscussionPolling(refresh: () => Promise<void>) {
  useEffect(() => {
    void refresh()
    const check = () => { if (!document.hidden) void refresh() }
    const timer = window.setInterval(check, 15_000)
    window.addEventListener('focus', check)
    return () => { window.clearInterval(timer); window.removeEventListener('focus', check) }
  }, [refresh])
}

function CommentEditor({ projectId, onUnavailable, parentId, original, latest, options, onSaved, onCancel }: Common & {
  parentId?: string; original?: ProjectComment; latest?: ProjectComment; options?: AnchorOption[]
  onSaved: (comment: ProjectComment) => void; onCancel?: () => void
}) {
  const t = useT()
  const { run, error, busy } = useCollaborationRequest(onUnavailable)
  const [text, setText] = useState(original?.text ?? '')
  const [anchorKey, setAnchorKey] = useState('project')
  const [base, setBase] = useState(original)
  const key = useCommentRequestKey()
  const label = original ? 'Edit comment' : parentId ? 'Write a reply' : 'Start a discussion'
  const save = () => {
    const anchor = options?.find((item) => item.key === anchorKey)?.anchor ?? { kind: 'project' as const }
    const payload = base ? { expected_revision: base.revision, action: 'edit' as const, text: text.trim() }
      : { text: text.trim(), anchor, ...(parentId ? { parent_id: parentId } : {}) }
    const requestId = key.forPayload(payload)
    void run(async (signal) => {
      const result = base
        ? await changeProjectComment(projectId, base.id, { ...payload, expected_revision: base.revision, action: 'edit', request_id: requestId }, signal)
        : await createProjectComment(projectId, { ...payload, request_id: requestId }, signal)
      requireCollaborationScope(projectId, result)
      if (base ? result.id !== base.id : result.parent_id !== (parentId ?? null)) throw new Error('Comment response did not match this discussion.')
      return result
    }, (result) => { key.clear(); setText(''); onSaved(result) })
  }
  return <form className="collaboration-editor" onSubmit={(event) => { event.preventDefault(); if (text.trim()) save() }}>
    <fieldset disabled={busy}><legend>{t(label)}</legend>
      {options && <label>{t('Discuss')}<select value={anchorKey} onChange={(event) => setAnchorKey(event.target.value)}>
        {options.map((option) => <option key={option.key} value={option.key}>{option.label}</option>)}
      </select></label>}
      <label>{t('Comment text')}<textarea required maxLength={4000} rows={4} value={text} onChange={(event) => setText(event.target.value)} /></label>
      <small>{text.length} / 4000</small>
      {error && <p role="alert">{t(error)} {t('Your comment draft is kept here.')}</p>}
      {base && latest && latest.revision !== base.revision && <div className="collaboration-review">
        <p>{t('This comment changed while you were editing. Review the latest text before applying your draft.')}</p>
        <p className="collaboration-text">{latest.text ?? t('Comment removed')}</p>
        {latest.actions.edit && <button type="button" onClick={() => setBase(latest)}>{t('Apply draft to this version')}</button>}
      </div>}
      <div className="collaboration-actions">
        <button type="submit" disabled={!text.trim() || (!!base && text.trim() === base.text)}>{t(busy ? 'Saving…' : original ? 'Save comment' : parentId ? 'Post reply' : 'Post comment')}</button>
        {onCancel && <button type="button" onClick={onCancel}>{t('Cancel editing')}</button>}
      </div>
    </fieldset>
  </form>
}

function CommentCard({ projectId, item, onUnavailable, onChanged }: Common & { item: ProjectComment; onChanged: (item: ProjectComment) => void }) {
  const t = useT()
  const { run, error, busy } = useCollaborationRequest(onUnavailable)
  const [editing, setEditing] = useState<ProjectComment | null>(null)
  const [removing, setRemoving] = useState<ProjectComment | null>(null)
  const [history, setHistory] = useState<CommentHistory | null>(null)
  const key = useCommentRequestKey()
  const mutate = (action: 'remove' | 'resolve' | 'reopen') => {
    const base = action === 'remove' ? removing : item
    if (!base) return
    const payload = { expected_revision: base.revision, action }
    void run(async (signal) => {
      const result = await changeProjectComment(projectId, item.id, { ...payload, request_id: key.forPayload(payload) }, signal)
      requireCollaborationScope(projectId, result)
      if (result.id !== item.id) throw new Error('Comment response did not match this discussion.')
      return result
    }, (result) => { key.clear(); setRemoving(null); setHistory(null); onChanged(result) })
  }
  const inspectHistory = () => {
    if (history) { setHistory(null); return }
    void run(async (signal) => {
      const result = await getCommentHistory(projectId, item.id, signal)
      requireCollaborationScope(projectId, result)
      if (result.comment_id !== item.id) throw new Error('Comment response did not match this discussion.')
      return result
    }, setHistory)
  }
  return <article className="collaboration-comment" aria-label={t('Comment by {name}', { name: item.author_id })}>
    <header><strong>{item.author_id}</strong><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString()}</time></header>
    {!item.parent_id && <span className="collaboration-state">{t(item.state === 'resolved' ? 'Resolved discussion' : 'Open discussion')}</span>}
    {item.revision > 1 && <small>{t('Comment revision {n}', { n: item.revision })} · {new Date(item.updated_at).toLocaleString()}</small>}
    <p className="collaboration-text">{item.text ?? t('Comment removed')}</p>
    <div className="collaboration-actions">
      {item.actions.edit && <button type="button" disabled={busy || !!editing} onClick={() => setEditing(item)}>{t('Edit comment')}</button>}
      {item.actions.remove && <button type="button" disabled={busy || !!editing} onClick={() => setRemoving(item)}>{t('Remove comment')}</button>}
      {item.actions.resolve && <button type="button" disabled={busy || !!editing} onClick={() => mutate(item.state === 'resolved' ? 'reopen' : 'resolve')}>{t(item.state === 'resolved' ? 'Reopen discussion' : 'Resolve discussion')}</button>}
      {item.actions.history && <button type="button" disabled={busy} onClick={inspectHistory} aria-expanded={history !== null}>{t('Comment history')}</button>}
    </div>
    {error && <p role="alert">{t(error)}</p>}
    {removing && <div className="collaboration-review">
      <p>{t('Remove this comment from the discussion? Its edit history remains available to its author and project administrators.')}</p>
      <button type="button" disabled={busy} onClick={() => mutate('remove')}>{t('Confirm comment removal')}</button>
      <button type="button" disabled={busy} onClick={() => setRemoving(null)}>{t('Cancel')}</button>
    </div>}
    {editing && <CommentEditor projectId={projectId} original={editing} latest={item} onUnavailable={onUnavailable}
      onCancel={() => setEditing(null)} onSaved={(result) => { setEditing(null); setHistory(null); onChanged(result) }} />}
    {history && <ol className="collaboration-history" aria-label={t('Comment history')}>
      {history.revisions.map((entry) => <li key={entry.revision}>
        <strong>{t('Comment revision {n}', { n: entry.revision })} · {t(entry.action)}</strong>
        <small>{entry.actor_id} · {new Date(entry.at).toLocaleString()}</small>
        <p className="collaboration-text">{entry.text ?? t('Comment removed')}</p>
      </li>)}
    </ol>}
  </article>
}

function Discussion({ projectId, commentId, canComment, onUnavailable }: Common & { commentId: string; canComment: boolean }) {
  const t = useT()
  const [thread, setThread] = useState<CommentThread | null>(null)
  const visibleReplies = useRef(50)
  const { run, error, busy } = useCollaborationRequest(onUnavailable)
  const accept = useCallback((result: CommentThread) => setThread((current) => ({
    ...result,
    root: current && current.root.revision > result.root.revision ? current.root : result.root,
    replies: mergeComments(current?.replies ?? [], result.replies),
  })), [])
  const refresh = useCallback(() => run(async (signal) => {
    let cursor: number | undefined
    const replies: ProjectComment[] = []
    // Re-read the pages the user opened, including edits to older replies.
    for (let pageNumber = 0; pageNumber < 100; pageNumber += 1) {
      const result = await getCommentThread(projectId, commentId, cursor, signal)
      requireCollaborationScope(projectId, result, result.root, ...result.replies)
      if (result.root.id !== commentId || result.root.parent_id !== null || result.replies.some((item) => item.parent_id !== commentId)) throw new Error('Comment response did not match this discussion.')
      replies.push(...result.replies)
      if (result.next_after === null || replies.length >= visibleReplies.current) return { ...result, replies }
      if (result.next_after <= (cursor ?? 0)) throw new Error('Comment response did not match this discussion.')
      cursor = result.next_after
    }
    throw new Error('Discussion exceeds the display limit.')
  }, accept), [projectId, commentId, run, accept])
  useDiscussionPolling(refresh)
  const more = () => {
    if (!thread?.next_after) return
    void run(async (signal) => {
      const page = await getCommentThread(projectId, commentId, thread.next_after!, signal)
      requireCollaborationScope(projectId, page, page.root, ...page.replies)
      if (page.root.id !== commentId || page.replies.some((item) => item.parent_id !== commentId)) throw new Error('Comment response did not match this discussion.')
      return page
    }, (page) => { visibleReplies.current += page.replies.length; accept(page) })
  }
  const changed = (item: ProjectComment) => {
    setThread((current) => current ? { ...current, root: item.parent_id === null ? item : current.root,
      replies: item.parent_id ? mergeComments(current.replies, [item]) : current.replies } : current)
    void refresh()
  }
  return <section className="collaboration-thread" aria-label={t('Discussion thread')}>
    <button type="button" disabled={busy} onClick={() => void refresh()}>{t('Refresh discussion')}</button>
    {error && <p role="alert">{t(error)}</p>}
    {!thread && busy && <p role="status">{t('Loading discussion…')}</p>}
    {thread && <>
      <div className="collaboration-target"><strong>{t('Discussing')}</strong><p>{thread.root.target.label}</p>
        <small>{t(thread.root.target.reference.kind)} {thread.root.target.artifact_version && t('Version {n}', { n: thread.root.target.artifact_version })}</small>
        {thread.root.target.reference.kind === 'artifact' && thread.root.target.reference.id && <a href={artifactDownloadUrl(projectId, thread.root.target.reference.id)}>{t('Download discussed file version')}</a>}
      </div>
      <CommentCard projectId={projectId} item={thread.root} onUnavailable={onUnavailable} onChanged={changed} />
      <div className="collaboration-replies">{thread.replies.map((item) => <CommentCard key={item.id} projectId={projectId} item={item} onUnavailable={onUnavailable} onChanged={changed} />)}</div>
      {thread.next_after !== null && <button type="button" disabled={busy} onClick={more}>{t('Load more replies')}</button>}
      {canComment && <CommentEditor projectId={projectId} parentId={commentId} onUnavailable={onUnavailable} onSaved={changed} />}
    </>}
  </section>
}

export default function ProjectComments({ project, currentRun, events, capabilities, onUnavailable }: {
  project: OpportunityProject; currentRun: RunRecord | null; events: OphiraEvent[]; capabilities: CollaborationCapabilities; onUnavailable: () => void
}) {
  const t = useT()
  const { run, busy, error } = useCollaborationRequest(onUnavailable)
  const [items, setItems] = useState<ProjectComment[]>([])
  const [next, setNext] = useState<number | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [state, setState] = useState<'all' | 'open' | 'resolved'>('all')
  const projectId = project.id
  const refresh = useCallback(() => run(async (signal) => {
    const result = await listProjectComments(projectId, undefined, signal)
    requireCollaborationScope(projectId, result, ...result.items)
    if (result.items.some((item) => item.parent_id !== null)) throw new Error('Comment response did not match this discussion.')
    return result
  }, (result) => { setItems(result.items); setNext(result.next_before); setLoaded(true) }), [projectId, run])
  useDiscussionPolling(refresh)
  const more = () => {
    if (next === null) return
    void run(async (signal) => {
      const result = await listProjectComments(projectId, next, signal)
      requireCollaborationScope(projectId, result, ...result.items)
      if (result.items.some((item) => item.parent_id !== null)) throw new Error('Comment response did not match this discussion.')
      return result
    }, (result) => {
      setItems((current) => [...current.filter((item) => !result.items.some((row) => row.id === item.id)), ...result.items])
      setNext(result.next_before)
    })
  }
  const options: AnchorOption[] = [{ key: 'project', label: t('Whole project'), anchor: { kind: 'project' } }]
  if (currentRun?.project_id === projectId) options.push({ key: currentRun.id, label: `${t('Current run')}: ${currentRun.prompt.slice(0, 90)}`, anchor: { kind: 'run', id: currentRun.id } })
  for (const event of events) {
    if (currentRun?.project_id === projectId && event.project_id === projectId && event.run_id === currentRun.id
      && event.trace_id === currentRun.trace_id && !event.actor.agent_id && !event.parent_id
      && event.type === 'assistant.message.completed' && typeof event.data.text === 'string' && event.data.text.trim()) {
      options.push({ key: event.id, label: `${t('Assistant message')}: ${event.data.text.slice(0, 100)}`, anchor: { kind: 'message', id: event.id, run_id: event.run_id } })
    }
  }
  for (const item of project.evidence) options.push({ key: item.id, label: `${t('Evidence')}: ${item.claim.slice(0, 100)}`, anchor: { kind: 'evidence', id: item.id } })
  for (const item of project.decisions) options.push({ key: item.id, label: `${t('Decision')}: ${item.outcome.slice(0, 100)}`, anchor: { kind: 'decision', id: item.id } })
  for (const item of project.artifacts) options.push({ key: item.id, label: `${item.name} · ${t('Version {n}', { n: item.version })}`, anchor: { kind: 'artifact', id: item.id } })
  const visible = items.filter((item) => state === 'all' || item.state === state)
  return <div>
    <p>{t('Discuss findings with your team. Comments stay attached to their original evidence, decisions, or file versions.')}</p>
    {!capabilities.can_comment && <p>{t('You have read-only access to project discussions.')}</p>}
    <div className="collaboration-actions"><button type="button" disabled={busy} onClick={() => void refresh()}>{t('Refresh discussions')}</button>
      <label>{t('Discussion status')}<select value={state} onChange={(event) => setState(event.target.value as typeof state)}>
        <option value="all">{t('All discussions')}</option><option value="open">{t('Open discussions')}</option><option value="resolved">{t('Resolved discussions')}</option>
      </select></label>
    </div>
    {error && <p role="alert">{t(error)}</p>}
    {!loaded && busy && <p role="status">{t('Loading discussions…')}</p>}
    {loaded && !visible.length && <p>{t('No discussions in this view.')}</p>}
    <ul className="collaboration-discussions">{visible.map((item) => <li key={item.id}>
      <button type="button" aria-expanded={selected === item.id} onClick={() => setSelected(selected === item.id ? null : item.id)}>
        <strong>{item.target.label.slice(0, 150)}</strong><span>{(item.text ?? t('Comment removed')).slice(0, 220)}</span>
        <small>{item.author_id} · {t(item.state === 'resolved' ? 'Resolved discussion' : 'Open discussion')} · {t('{n} replies', { n: item.reply_count })}</small>
      </button>
    </li>)}</ul>
    {next !== null && <button type="button" disabled={busy} onClick={more}>{t('Load older discussions')}</button>}
    {selected && <Discussion key={selected} projectId={projectId} commentId={selected} canComment={capabilities.can_comment} onUnavailable={onUnavailable} />}
    {capabilities.can_comment && <CommentEditor projectId={projectId} options={options} onUnavailable={onUnavailable}
      onSaved={(item) => { setSelected(item.id); void refresh() }} />}
  </div>
}
