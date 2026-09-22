import { useEffect, useRef, useState, type FormEvent } from 'react'
import { deleteCredential, errorMessage, exportCredential, listCredentials, saveServiceCredential } from '../api/client'
import type { CredentialView } from '../api/credentials'
import { useT } from '../i18n'
import './credential-settings.css'

const labels: Record<CredentialView['source'], string> = {
  stored: 'Stored in the local vault', environment: 'Provided by the deployment environment',
  missing: 'No credential configured', deleted: 'Credential access deleted',
}
const kinds: Record<CredentialView['kind'], string> = { provider: 'Provider', connector: 'Connector', service: 'Application service' }
const titles = { export: 'Export encrypted credential for {name}', delete: 'Delete credential access for {name}', save: 'Save service credential for {name}' }
const actions = { export: 'Download encrypted credential', delete: 'Confirm credential deletion', save: 'Save service credential' }

function CredentialPanel({ onChanged }: { onChanged: () => void }) {
  const t = useT()
  const [items, setItems] = useState<CredentialView[]>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [selected, setSelected] = useState<{ item: CredentialView; action: keyof typeof actions } | null>(null)
  const [secret, setSecret] = useState('')
  const [password, setPassword] = useState('')
  const [repeat, setRepeat] = useState('')
  const [confirmed, setConfirmed] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const operation = useRef<AbortController | null>(null)
  const name = (item: CredentialView) => item.kind === 'service' ? t(item.display_name) : item.display_name

  useEffect(() => {
    const controller = new AbortController()
    void listCredentials(controller.signal).then((result) => {
      if (!controller.signal.aborted) { setItems(result); setLoading(false) }
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) { setError(errorMessage(failure)); setLoading(false) }
    })
    return () => { controller.abort() }
  }, [refresh])
  useEffect(() => () => { operation.current?.abort() }, [])

  const clearForm = () => {
    setSelected(null); setPassword(''); setRepeat(''); setConfirmed(false); setSecret('')
  }
  const reload = () => {
    clearForm(); setError(''); setItems([]); setLoading(true); setRefresh((value) => value + 1)
  }
  const choose = (item: CredentialView, action: keyof typeof actions) => {
    clearForm(); setError(''); setNotice(''); setSelected({ item, action })
  }
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!selected || operation.current) return
    if (selected.action === 'delete' && !confirmed) return
    if (selected.action === 'save' && (!secret.trim() || selected.item.kind !== 'service')) return
    if (selected.action === 'export' && (password.length < 12 || password !== repeat)) {
      setError('Use matching archive passwords of at least 12 characters.'); return
    }
    const controller = new AbortController()
    operation.current = controller
    setBusy(true); setError(''); setNotice('')
    const { item, action } = selected
    try {
      if (action === 'export') {
        const archive = await exportCredential(item, password, controller.signal)
        if (controller.signal.aborted) return
        const url = URL.createObjectURL(new Blob([JSON.stringify(archive, null, 2)], { type: 'application/json' }))
        const link = document.createElement('a')
        link.href = url; link.download = `ophira-${item.kind}-${item.id}.credential.json`
        document.body.append(link); link.click(); link.remove()
        // Give the browser time to consume the download URL.
        window.setTimeout(() => URL.revokeObjectURL(url), 1000)
        setNotice('Encrypted credential downloaded. Keep its password separately.')
      } else {
        const receipt = action === 'save'
          ? await saveServiceCredential(item, secret, controller.signal)
          : await deleteCredential(item, controller.signal)
        if (controller.signal.aborted) return
        if (receipt.kind !== item.kind || receipt.id !== item.id || receipt.source !== (action === 'save' ? 'stored' : 'deleted')) throw new Error('Credential response did not match the reviewed action. Refresh before retrying.')
        setItems((current) => current.map((entry) => entry.id === item.id && entry.kind === item.kind ? receipt : entry))
        setNotice(action === 'save' ? 'Service credential saved. Existing application and device settings still apply.' : 'Local credential access deleted. Existing external copies are unchanged.')
        onChanged()
      }
      clearForm()
    } catch (failure: unknown) {
      if (!controller.signal.aborted) {
        setError(errorMessage(failure)); clearForm()
        // A lost response can mean a mutation committed. Fetch current state before reviewing again.
        setItems([]); setLoading(true); setRefresh((value) => value + 1)
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false)
      operation.current = null
    }
  }

  return <div className="credential-panel">
    <p>{t('Export one Provider, connector or application service credential in a password-encrypted file. Saved keys are never displayed here.')}</p>
    {error && <p role="alert">{t(error)}</p>}
    {notice && <p role="status">{t(notice)}</p>}
    <button type="button" disabled={busy || loading} onClick={reload}>{t('Refresh credentials')}</button>
    {loading && <p role="status">{t('Loading credentials…')}</p>}
    {!loading && !items.length && !error && <p>{t('No credential profiles found.')}</p>}
    <ul>{items.map((item) => <li key={`${item.kind}:${item.id}`}>
      <div><strong>{name(item)}</strong><small>{t(kinds[item.kind])} · {t(labels[item.source])}</small>
        {item.deleted_at && <small>{t('Deleted {time}', { time: item.deleted_at })}</small>}</div>
      <div className="credential-actions">
        <button type="button" disabled={busy || !item.configured} onClick={() => choose(item, 'export')}>{t('Export {name}', { name: name(item) })}</button>
        <button type="button" disabled={busy || !item.configured} onClick={() => choose(item, 'delete')}>{t('Delete {name}', { name: name(item) })}</button>
        {item.kind === 'service' && <button type="button" disabled={busy} onClick={() => choose(item, 'save')}>{t('Configure {name}', { name: name(item) })}</button>}
      </div>
    </li>)}</ul>
    {selected && <form onSubmit={(event) => void submit(event)} aria-label={t('Credential review')}>
      <h4>{t(titles[selected.action], { name: name(selected.item) })}</h4>
      {selected.item.effect && <p>{t(selected.item.effect)}</p>}
      {selected.action === 'export' ? <>
        <p>{t('Use a new password, not the API key. You will need this password to decrypt the archive.')}</p>
        <label>{t('Archive password')}<input type="password" minLength={12} maxLength={1024} autoComplete="new-password" required disabled={busy} value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        <label>{t('Repeat archive password')}<input type="password" maxLength={1024} autoComplete="new-password" required disabled={busy} value={repeat} onChange={(event) => setRepeat(event.target.value)} /></label>
      </> : selected.action === 'save' ? <>
        <p>{t('Paste the replacement credential. Application IDs, endpoints and feature settings remain managed by your deployment operator.')}</p>
        <p>{t('Saving a valid key can resume configured channel replies or notifications. Review pending deliveries and service settings before saving.')}</p>
        <label>{t('New service credential')}<input type="password" autoComplete="new-password" required maxLength={2000} disabled={busy} value={secret} onChange={(event) => setSecret(event.target.value)} /></label>
      </> : <>
        <p>{t('This removes the saved key and disables environment fallback until you save a new credential. It does not revoke the key at the provider, erase backups or stop tasks that already loaded it.')}</p>
        <label className="credential-confirm"><input type="checkbox" checked={confirmed} disabled={busy} onChange={(event) => setConfirmed(event.target.checked)} />{t('I confirm deletion of this credential access.')}</label>
      </>}
      {selected.action !== 'export' && <p>{t('Closing this panel does not undo a submitted change. Refresh credentials to check its result after a disconnect.')}</p>}
      <div className="credential-actions">
        <button type="button" disabled={busy} onClick={clearForm}>{t('Cancel')}</button>
        <button type="submit" disabled={busy || (selected.action === 'delete' && !confirmed) || (selected.action === 'save' && !secret.trim())}>{busy ? t('Working…') : t(actions[selected.action])}</button>
      </div>
    </form>}
  </div>
}

export default function CredentialSettings({ onChanged }: { onChanged: () => void }) {
  const t = useT()
  const [open, setOpen] = useState(false)
  return <section className="settings-card credential-settings">
    <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>{t('Credential backups and deletion')}</button>
    {open && <CredentialPanel onChanged={onChanged} />}
  </section>
}
