import { useEffect, useRef, useState } from 'react'
import { correctMemory, decideMemory, deleteMemory, errorMessage, exportMemory, listMemory, updateMemoryExpiry } from '../api/client'
import type { MemoryItem, MemoryKind, MemorySensitivity } from '../api/types'
import type { GovernedMemory } from '../types/MemoryGovernance'
import { useLanguage, useT } from '../i18n'

const kindLabels: Record<MemoryItem['kind'], string> = {
  preference: 'Preference', fact: 'Fact', decision: 'Decision', lesson: 'Lesson',
}
const scopeLabels: Record<MemoryItem['scope'], string> = {
  user: 'User scope', project: 'Project scope', agent: 'Agent scope',
}
const statusLabels: Record<MemoryItem['status'], string> = {
  proposed: 'Proposed', approved: 'Approved', rejected: 'Rejected', disabled: 'Disabled',
}
type MemoryDraft = { text: string; kind: MemoryKind; sensitivity: MemorySensitivity }
type Panel = { id: string; kind: 'edit' | 'expiry' | 'delete' } | null
const isExpired = (item: GovernedMemory) => item.expired === true
  || Boolean(item.expires_at && Date.parse(item.expires_at) <= Date.now())
const inaccessible = (error: unknown) => typeof error === 'object' && error !== null
  && 'status' in error && (error.status === 403 || error.status === 404)

export default function MemoryReview() {
  const t = useT()
  const language = useLanguage()
  const [items, setItems] = useState<GovernedMemory[]>([])
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [panel, setPanel] = useState<Panel>(null)
  const [draft, setDraft] = useState<MemoryDraft | null>(null)
  const [reason, setReason] = useState('')
  const [duration, setDuration] = useState('30')
  const [reload, setReload] = useState(0)
  const [loading, setLoading] = useState(true)
  const [, tick] = useState(0)
  const actionInFlightRef = useRef('')
  const loadingRef = useRef(true)
  const epochRef = useRef(0)
  const mountedRef = useRef(false)
  const exportAbortRef = useRef<AbortController | null>(null)
  const downloadRef = useRef<{ url: string; timer: ReturnType<typeof setTimeout> } | null>(null)

  useEffect(() => {
    mountedRef.current = true
    const epoch = ++epochRef.current
    const controller = new AbortController()
    const interval = setInterval(() => tick((value) => value + 1), 30_000)
    void listMemory(controller.signal)
      .then((memories) => {
        if (mountedRef.current && epoch === epochRef.current) {
          setItems(memories)
          loadingRef.current = false
          setLoading(false)
        }
      })
      .catch((loadError) => {
        if (mountedRef.current && epoch === epochRef.current) {
          if (inaccessible(loadError)) { setItems([]); setPanel(null); setDraft(null) }
          setError(errorMessage(loadError))
          loadingRef.current = false
          setLoading(false)
        }
      })
    return () => {
      mountedRef.current = false
      epochRef.current += 1
      controller.abort()
      exportAbortRef.current?.abort()
      clearInterval(interval)
      if (downloadRef.current) {
        clearTimeout(downloadRef.current.timer)
        URL.revokeObjectURL(downloadRef.current.url)
        downloadRef.current = null
      }
    }
  }, [reload])

  const closePanel = () => { setPanel(null); setDraft(null); setReason('') }
  const begin = (item: GovernedMemory, kind: NonNullable<Panel>['kind']) => {
    if (loadingRef.current || actionInFlightRef.current || (kind === 'edit' && isExpired(item))) return
    setPanel({ id: item.id, kind })
    setDraft(kind === 'edit' ? { text: item.text, kind: item.kind, sensitivity: item.sensitivity } : null)
    setReason('')
    setDuration('30')
    setError('')
    setNotice('')
  }
  const mutate = async (item: GovernedMemory, operation: () => Promise<GovernedMemory | null>) => {
    if (loadingRef.current || actionInFlightRef.current) return
    actionInFlightRef.current = item.id
    const epoch = epochRef.current
    setBusy(item.id)
    setError('')
    setNotice('')
    try {
      const updated = await operation()
      if (!mountedRef.current || epoch !== epochRef.current) return
      setItems((current) => updated
        ? current.map((entry) => entry.id === item.id ? updated : entry)
        : current.filter((entry) => entry.id !== item.id))
      closePanel()
    } catch (actionError) {
      if (!mountedRef.current || epoch !== epochRef.current) return
      if (inaccessible(actionError)) {
        setItems((current) => current.filter((entry) => entry.id !== item.id))
        closePanel()
        setError('This memory is no longer available. Its local content has been cleared.')
      } else setError(errorMessage(actionError))
    } finally {
      if (mountedRef.current && epoch === epochRef.current) {
        actionInFlightRef.current = ''
        setBusy('')
      }
    }
  }
  const decide = (item: GovernedMemory, decision: 'approve' | 'reject' | 'disable') => {
    if (decision === 'approve' && isExpired(item)) return
    void mutate(item, () => decideMemory(item.id, decision))
  }
  const saveEdit = (item: GovernedMemory) => {
    if (!draft?.text.trim() || isExpired(item)) return
    const input = { ...draft, text: draft.text.trim() }
    void mutate(item, () => correctMemory(item.id, input))
  }
  const download = async () => {
    if (loadingRef.current || actionInFlightRef.current) return
    actionInFlightRef.current = 'export'
    const controller = new AbortController()
    exportAbortRef.current = controller
    const epoch = epochRef.current
    setBusy('export')
    setError('')
    setNotice('')
    try {
      const result = await exportMemory(controller.signal)
      if (!mountedRef.current || controller.signal.aborted || epoch !== epochRef.current) return
      const url = URL.createObjectURL(new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' }))
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = 'ophira-memory-export.json'
      if (downloadRef.current) {
        clearTimeout(downloadRef.current.timer)
        URL.revokeObjectURL(downloadRef.current.url)
      }
      downloadRef.current = { url, timer: setTimeout(() => {
        URL.revokeObjectURL(url)
        if (downloadRef.current?.url === url) downloadRef.current = null
      }, 1000) }
      document.body.appendChild(anchor)
      try { anchor.click() } finally { anchor.remove() }
      setNotice('Memory export sent to your browser. Check your downloads to confirm it was saved.')
    } catch (downloadError) {
      if (!mountedRef.current || controller.signal.aborted || epoch !== epochRef.current) return
      if (inaccessible(downloadError)) {
        setItems([])
        closePanel()
        setError('Your memory access has changed. Local memory content has been cleared.')
      } else setError(errorMessage(downloadError))
    } finally {
      if (mountedRef.current && exportAbortRef.current === controller) {
        actionInFlightRef.current = ''
        exportAbortRef.current = null
        setBusy('')
      }
    }
  }
  const cancelDownload = () => {
    exportAbortRef.current?.abort()
    exportAbortRef.current = null
    actionInFlightRef.current = ''
    setBusy('')
    setNotice('Memory export cancelled.')
  }
  const date = (value: string) => new Date(value).toLocaleString(language === 'zh-CN' ? 'zh-CN' : 'en')
  const renderPanel = (item: GovernedMemory) => {
    if (panel?.kind === 'edit' && draft) return (
      <form className="memory-review-editor" aria-label={t('Edit memory')} onSubmit={(event) => { event.preventDefault(); saveEdit(item) }}>
        <label>{t('Memory text')}<textarea value={draft.text} maxLength={4000} disabled={Boolean(busy) || loading} onChange={(event) => setDraft((current) => current ? { ...current, text: event.target.value } : current)} rows={3} required /></label>
        <label>{t('Kind')}<select disabled={Boolean(busy) || loading} value={draft.kind} onChange={(event) => setDraft((current) => current ? { ...current, kind: event.target.value as MemoryKind } : current)}>{Object.entries(kindLabels).map(([kind, label]) => <option key={kind} value={kind}>{t(label)}</option>)}</select></label>
        <label>{t('Sensitivity')}<select disabled={Boolean(busy) || loading} value={draft.sensitivity} onChange={(event) => setDraft((current) => current ? { ...current, sensitivity: event.target.value as MemorySensitivity } : current)}><option value="normal">{t('Normal')}</option><option value="sensitive">{t('Sensitive')}</option></select></label>
        <small>{t('Saving a correction creates a new approved version.')}</small>
        {isExpired(item) && <small>{t('This memory has expired. Renew it before approving or editing.')}</small>}
        <div className="memory-review-actions"><button type="submit" disabled={Boolean(busy) || loading || !draft.text.trim() || isExpired(item)}>{busy === item.id ? t('Saving…') : t('Save')}</button><button type="button" disabled={Boolean(busy) || loading} onClick={closePanel}>{t('Cancel')}</button></div>
      </form>
    )
    if (panel?.kind === 'expiry') return (
      <form className="memory-governance-form" aria-label={t('Set memory expiry')} onSubmit={(event) => {
        event.preventDefault()
        void mutate(item, () => updateMemoryExpiry(item.id, duration === 'permanent' ? { clear_expiry: true } : { ttl_seconds: Number(duration) * 86400 }))
      }}>
        <p>{item.text}</p>
        <label>{t('Keep memory for')}<select value={duration} disabled={Boolean(busy) || loading} onChange={(event) => setDuration(event.target.value)}>{[1, 7, 30, 90].map((days) => <option key={days} value={days}>{t('{n} days from now', { n: days })}</option>)}<option value="permanent">{t('No expiry')}</option></select></label>
        <small>{t('Renewing an approved memory makes it available to the Agent again. Other review statuses stay unchanged.')}</small>
        <div className="memory-review-actions"><button type="submit" disabled={Boolean(busy) || loading}>{busy === item.id ? t('Saving…') : t('Save expiry')}</button><button type="button" disabled={Boolean(busy) || loading} onClick={closePanel}>{t('Cancel')}</button></div>
      </form>
    )
    return (
      <form className="memory-governance-form" aria-label={t('Delete memory')} onSubmit={(event) => {
        event.preventDefault()
        if (!reason.trim()) return
        void mutate(item, async () => { await deleteMemory(item.id, reason.trim()); return null })
      }}>
        <p>{item.text}</p>
        <p>{t('Permanently delete this memory and its text history from the application? Its Agent memory mirror is also removed. Backups and downloaded copies are managed separately.')}</p>
        <small>{t('A deletion record keeps your reason and a content hash, without the memory text. Do not include private content in the reason.')}</small>
        <label>{t('Deletion reason')}<textarea value={reason} disabled={Boolean(busy) || loading} maxLength={2000} required rows={2} onChange={(event) => setReason(event.target.value)} /></label>
        <div className="memory-review-actions"><button type="submit" data-tone="danger" disabled={Boolean(busy) || loading || !reason.trim()}>{busy === item.id ? t('Deleting…') : t('Permanently delete')}</button><button type="button" disabled={Boolean(busy) || loading} onClick={closePanel}>{t('Cancel')}</button></div>
      </form>
    )
  }
  const renderItem = (item: GovernedMemory) => (
    <article key={item.id} className="memory-review-item">
      {panel?.id === item.id ? renderPanel(item) : <>
        <div className="memory-review-content">
          <p>{item.status !== 'proposed' && <span data-status={item.status}>{t(statusLabels[item.status])} · </span>}{item.text}</p>
          <small>{t(kindLabels[item.kind])} · {t(scopeLabels[item.scope])} · {item.status === 'proposed' ? t('Proposed {date}', { date: new Date(item.created_at).toLocaleDateString(language === 'zh-CN' ? 'zh-CN' : 'en') }) : `v${item.version}`}</small>
          <small className="memory-expiry" data-expired={isExpired(item)}>{item.expires_at ? t(isExpired(item) ? 'Expired {date}' : 'Expires {date}', { date: date(item.expires_at) }) : t('No expiry')}</small>
          {isExpired(item) && <small>{t('This memory has expired. Renew it before approving or editing.')}</small>}
        </div>
        <div className="memory-review-actions">
          <button type="button" disabled={Boolean(busy) || loading || isExpired(item)} onClick={() => begin(item, 'edit')}>{t('Edit')}</button>
          {item.status === 'proposed' && <><button type="button" disabled={Boolean(busy) || loading || isExpired(item)} onClick={() => decide(item, 'approve')}>{t('Approve')}</button><button type="button" disabled={Boolean(busy) || loading} onClick={() => decide(item, 'reject')}>{t('Reject')}</button></>}
          {item.status === 'approved' && <button type="button" disabled={Boolean(busy) || loading} onClick={() => decide(item, 'disable')}>{t('Disable')}</button>}
          <button type="button" disabled={Boolean(busy) || loading} onClick={() => begin(item, 'expiry')}>{t(isExpired(item) ? 'Renew' : 'Set expiry')}</button>
          <button type="button" data-tone="danger" disabled={Boolean(busy) || loading} onClick={() => begin(item, 'delete')}>{t('Delete')}</button>
        </div>
      </>}
    </article>
  )
  const proposed = items.filter((item) => item.status === 'proposed')
  const decided = items.filter((item) => item.status !== 'proposed')
  return (
    <div className="settings-card memory-review">
      <div className="memory-review-toolbar"><p>{t('Export all memories you can access, including review statuses, expiry dates and deletion records.')}</p><div className="memory-review-actions"><button type="button" disabled={Boolean(busy) || loading} onClick={() => void download()}>{busy === 'export' ? t('Exporting…') : t('Export memories (JSON)')}</button>{busy === 'export' && <button type="button" onClick={cancelDownload}>{t('Cancel export')}</button>}</div></div>
      {error && <div className="conversation-error" role="alert"><p>{t(error)}</p><button type="button" disabled={Boolean(busy) || loading} onClick={() => { closePanel(); setError(''); loadingRef.current = true; setLoading(true); setReload((value) => value + 1) }}>{t('Refresh memories')}</button></div>}
      {loading && <p role="status">{t('Loading memories…')}</p>}
      {notice && <p role="status">{t(notice)}</p>}
      {proposed.length ? proposed.map(renderItem) : <p className="settings-integration-note">{t('No memory proposals are waiting for review. Agent proposals appear here and only take effect after you approve them.')}</p>}
      {decided.length > 0 && <details className="memory-review-history"><summary>{t('Reviewed memories ({n})', { n: decided.length })}</summary>{decided.map(renderItem)}</details>}
    </div>
  )
}
