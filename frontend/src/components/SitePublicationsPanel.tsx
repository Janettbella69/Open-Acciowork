import { useCallback, useEffect, useId, useState } from 'react'
import {
  errorMessage, getSitePublications, publishSitePublication,
  revokeSitePublication, sitePublicationPreviewUrl, stageSitePublication,
} from '../api/client'
import type { SitePublicationCatalog, SitePublicationView } from '../api/sitePublications'
import type { Artifact } from '../api/types'
import { useT } from '../i18n'
import './site-publications.css'

const MAX_HTML_BYTES = 512 * 1024

export default function SitePublicationsPanel({ projectId, artifacts }: {
  projectId: string
  artifacts: Artifact[]
}) {
  const t = useT()
  const bodyId = useId()
  const [open, setOpen] = useState(false)
  const [catalog, setCatalog] = useState<SitePublicationCatalog | null>(null)
  const [artifactId, setArtifactId] = useState('')
  const [allowScripts, setAllowScripts] = useState(false)
  const [selectedId, setSelectedId] = useState('')
  const [reviewed, setReviewed] = useState(false)
  const [revokeId, setRevokeId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const htmlArtifacts = artifacts.filter((item) => (
    item.project_id === projectId && item.content_storage === 'snapshot'
    && item.size_bytes > 0 && item.size_bytes <= MAX_HTML_BYTES
    && /\.html?$/i.test(item.name)
  )).sort((a, b) => b.created_at.localeCompare(a.created_at))
  const chosenArtifactId = htmlArtifacts.some((item) => item.id === artifactId)
    ? artifactId : htmlArtifacts[0]?.id ?? ''
  const selected = catalog?.publications.find((item) => item.publication.id === selectedId)

  const refresh = useCallback(async (signal?: AbortSignal) => {
    const next = await getSitePublications(projectId, signal)
    setCatalog(next)
    return next
  }, [projectId])

  useEffect(() => {
    const controller = new AbortController()
    void refresh(controller.signal).catch((cause: unknown) => {
      if (!controller.signal.aborted) setError(errorMessage(cause))
    })
    return () => controller.abort()
  }, [refresh])

  const replace = (view: SitePublicationView) => {
    setCatalog((current) => current ? {
      ...current,
      publications: [view, ...current.publications.filter(
        (item) => item.publication.id !== view.publication.id,
      )],
    } : current)
  }

  const stage = async () => {
    if (!chosenArtifactId || !catalog?.can_stage || busy) return
    setBusy(true); setError(''); setNotice('')
    try {
      const view = await stageSitePublication(projectId, chosenArtifactId, allowScripts)
      replace(view)
      setSelectedId(view.publication.id)
      setReviewed(false)
      setNotice(t('HTML version staged for review. It is not public yet.'))
    } catch (cause) {
      setError(errorMessage(cause))
      void refresh().catch(() => undefined)
    } finally {
      setBusy(false)
    }
  }

  const publish = async (view: SitePublicationView) => {
    if (!catalog?.can_publish || !reviewed || busy) return
    setBusy(true); setError(''); setNotice('')
    try {
      const updated = await publishSitePublication(projectId, view)
      replace(updated)
      setReviewed(false)
      setNotice(t('This exact HTML version is public.'))
    } catch (cause) {
      setError(errorMessage(cause))
      void refresh().catch(() => undefined)
    } finally {
      setBusy(false)
    }
  }

  const revoke = async (view: SitePublicationView) => {
    if (!catalog?.can_publish || revokeId !== view.publication.id || busy) return
    setBusy(true); setError(''); setNotice('')
    try {
      const updated = await revokeSitePublication(projectId, view)
      replace(updated)
      setRevokeId('')
      setNotice(t('The public page was withdrawn.'))
    } catch (cause) {
      setError(errorMessage(cause))
      void refresh().catch(() => undefined)
    } finally {
      setBusy(false)
    }
  }

  return <section className="site-publications">
    <button className="site-publications-toggle" type="button" aria-expanded={open}
      aria-controls={bodyId} onClick={() => setOpen(!open)}>
      <span>{t('Published pages')} · {catalog?.publications.filter(
        (item) => item.publication.state === 'published',
      ).length ?? 0}</span><span aria-hidden="true">{open ? '−' : '+'}</span>
    </button>
    {open && <div className="site-publications-body" id={bodyId}>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {!catalog && !error && <p role="status">{t('Loading page publications…')}</p>}
      {catalog && <>
        <p>{t('Publish a saved HTML version after reviewing it. Public pages can be withdrawn.')}</p>
        <p>{t('Public pages allow inline CSS and embedded data images. Forms and remote resources are blocked.')}</p>
        {!catalog.public_origin && <p>{t('Configure a separate public HTTPS site origin to publish pages.')}</p>}
        {catalog.public_origin && catalog.can_stage && <div className="site-publications-stage">
          <label>{t('Saved HTML version')}
            <select value={chosenArtifactId} onChange={(event) => setArtifactId(event.target.value)}>
              {htmlArtifacts.map((item) => <option key={item.id} value={item.id}>
                {item.name} · v{item.version} · {item.checksum.slice(0, 12)}
              </option>)}
            </select>
          </label>
          <label className="site-publications-confirm"><input type="checkbox" checked={allowScripts}
            disabled={busy} onChange={(event) => setAllowScripts(event.target.checked)} />
            {t('Run inline scripts in an isolated page (no storage or background requests).')}</label>
          <button type="button" disabled={busy || !chosenArtifactId} onClick={() => void stage()}>
            {t('Stage this HTML version')}
          </button>
        </div>}
        {catalog.public_origin && catalog.can_stage && !htmlArtifacts.length
          && <p>{t('Save an HTML artifact in this project before staging a page.')}</p>}
        {!catalog.publications.length && <p>{t('No page drafts or publications yet.')}</p>}
        {catalog.publications.map((view) => {
          const item = view.publication
          return <article key={item.id} className="site-publications-item">
            <button type="button" aria-expanded={selectedId === item.id}
              onClick={() => { setSelectedId(selectedId === item.id ? '' : item.id); setReviewed(false); setRevokeId('') }}>
              <strong>{item.artifact_name}</strong>
              <span>{t({ draft: 'Draft', published: 'Published', revoked: 'Withdrawn' }[item.state])} · {item.artifact_checksum.slice(0, 12)} · {item.allow_scripts ? t('Interactive scripts') : t('Static page')}</span>
            </button>
            {selected?.publication.id === item.id && <div className="site-publications-detail">
              <p>{t('Exact saved version')}: <code>{item.artifact_checksum}</code></p>
              <p>{t('Public address')}: <code>{view.public_url}</code></p>
              {item.state === 'draft' && <>
                <iframe title={t('Page draft preview')} sandbox={item.allow_scripts ? 'allow-scripts' : ''}
                  referrerPolicy="no-referrer"
                  src={sitePublicationPreviewUrl(projectId, item.id)} />
                <p>{t('Review the preview and exact SHA-256 before approving publication.')}</p>
                {catalog.can_publish ? <>
                  <label className="site-publications-confirm"><input type="checkbox" checked={reviewed}
                    disabled={busy} onChange={(event) => setReviewed(event.target.checked)} />
                    {item.allow_scripts
                      ? t('I reviewed this exact HTML version and approve its scripts running publicly.')
                      : t('I reviewed this exact HTML version and approve making it public.')}</label>
                  <button type="button" disabled={!reviewed || busy}
                    onClick={() => void publish(view)}>{t('Approve and publish page')}</button>
                </> : <p>{t('A project administrator must approve publication.')}</p>}
              </>}
              {item.state === 'published' && <>
                <a href={view.public_url} target="_blank" rel="noreferrer">{t('Open public page')}</a>
                {catalog.can_publish && (revokeId === item.id ? <div className="site-publications-revoke">
                  <p>{t('Withdraw this page from its public address?')}</p>
                  <button type="button" disabled={busy} onClick={() => void revoke(view)}>{t('Confirm withdrawal')}</button>
                  <button type="button" disabled={busy} onClick={() => setRevokeId('')}>{t('Cancel')}</button>
                </div> : <button type="button" disabled={busy} onClick={() => setRevokeId(item.id)}>{t('Withdraw page')}</button>)}
              </>}
              {item.state === 'revoked' && <p>{t('This page is no longer public.')}</p>}
            </div>}
          </article>
        })}
      </>}
    </div>}
  </section>
}
