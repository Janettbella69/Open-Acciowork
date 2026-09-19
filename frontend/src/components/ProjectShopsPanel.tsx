import { useEffect, useRef, useState } from 'react'
import { apiBaseUrl, controlProjectShop, errorMessage, getProjectShopData, grantProjectShop, listConnectors, listProjectShops, saveProjectShop } from '../api/client'
import type { ProjectShop, ShopData, ShopIdentityInput } from '../api/shops'
import type { Connector } from '../api/types'
import { useT } from '../i18n'
import './project-shops.css'

const platforms = { shein: 'SHEIN', temu: 'Temu', tiktok_shop: 'TikTok Shop', aliexpress: 'AliExpress', shopee: 'Shopee', shopify: 'Shopify', other: 'Other platform' } as const satisfies Record<ShopIdentityInput['platform'], string>
const empty: ShopIdentityInput = { name: '', platform: 'tiktok_shop', seller_shop_id: '', connector_id: null, entry_url: '', additional_domains: [] }

export default function ProjectShopsPanel({ projectId, onTask }: { projectId: string; onTask?: (text: string) => void }) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const [shops, setShops] = useState<ProjectShop[]>([])
  const [connectors, setConnectors] = useState<Connector[]>([])
  const [form, setForm] = useState<ShopIdentityInput>(empty)
  const [domains, setDomains] = useState('')
  const [editing, setEditing] = useState<ProjectShop | undefined>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [confirmed, setConfirmed] = useState<Record<string, boolean>>({})
  const [days, setDays] = useState(7)
  const [data, setData] = useState<{ shopId: string; records: ShopData[] } | null>(null)
  const pending = useRef(false)
  const mounted = useRef(true)
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])

  const act = async (action: () => Promise<void>) => {
    if (pending.current) return
    pending.current = true; setBusy(true); setError(''); setNotice('')
    try { await action() } catch (reason) {
      if (mounted.current) {
        setError(errorMessage(reason)); setData(null)
        if (reason && typeof reason === 'object' && 'status' in reason && [403, 404].includes(Number(reason.status))) {
          setShops([]); setEditing(undefined); setForm(empty); setDomains(''); setConfirmed({})
        }
      }
    } finally { pending.current = false; if (mounted.current) setBusy(false) }
  }
  const replace = (shop: ProjectShop) => {
    if (!mounted.current || shop.project_id !== projectId) return
    setShops((current) => current.some((item) => item.id === shop.id) ? current.map((item) => item.id === shop.id ? shop : item) : [...current, shop])
    setConfirmed((current) => ({ ...current, [shop.id]: false }))
  }
  const refresh = () => act(async () => {
    const [items, catalog] = await Promise.all([listProjectShops(projectId), listConnectors()])
    if (mounted.current) { setShops(items.filter((item) => item.project_id === projectId)); setConnectors(catalog); setConfirmed({}) }
  })
  const save = () => act(async () => {
    const saved = await saveProjectShop(projectId, { ...form, additional_domains: domains.split(/[\s,]+/).filter(Boolean) }, editing)
    replace(saved)
    if (mounted.current) { setForm(empty); setDomains(''); setEditing(undefined); setNotice(t('Shop identity saved. Open its browser and sign in.')) }
  })
  const control = (shop: ProjectShop, operation: 'open' | 'close' | 'revoke') => act(async () => {
    replace(await controlProjectShop(projectId, shop, operation))
    if (mounted.current && operation === 'open') setNotice(t('The shop window opened on the computer running TradeMind. Complete sign-in there, then confirm the shop identity here.'))
  })
  const grant = (shop: ProjectShop) => act(async () => {
    if (!confirmed[shop.id]) return
    replace(await grantProjectShop(projectId, shop, days))
    if (mounted.current) setNotice(t('This browser session is available to project Agents until expiry.'))
  })
  const inspectData = (shop: ProjectShop) => act(async () => {
    setData(null)
    const records = await getProjectShopData(projectId, shop.id)
    if (mounted.current) setData({ shopId: shop.id, records })
  })

  return <section className="project-shops">
    <button type="button" className="project-shops-toggle" aria-expanded={open} onClick={() => {
      setOpen(!open); if (!open) void refresh()
    }}>{t('Project shops')} <span>{shops.length || ''}</span></button>
    {open && <div className="project-shops-content">
      <p>{t('Configure a shop identity, connect its platform, and sign in through its independent browser. Agent captures and downloaded reports stay with this project and their sources.')}</p>
      {error && <p role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
      <button type="button" disabled={busy} onClick={() => void refresh()}>{t('Refresh shops')}</button>
      <form onSubmit={(event) => { event.preventDefault(); void save() }}>
        <fieldset disabled={busy}><legend>{t(editing ? 'Edit shop identity' : 'Add shop identity')}</legend>
          <div className="project-shops-fields">
            <label>{t('Shop display name')}<input required maxLength={160} value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label>
            <label>{t('Shop platform')}<select value={form.platform} onChange={(event) => setForm({ ...form, platform: event.target.value as ShopIdentityInput['platform'], connector_id: null })}>{Object.entries(platforms).map(([value, label]) => <option key={value} value={value}>{value === 'other' ? t('Other platform') : label}</option>)}</select></label>
            <label>{t('Seller shop identity')}<input required maxLength={160} value={form.seller_shop_id} onChange={(event) => setForm({ ...form, seller_shop_id: event.target.value })} /></label>
            <label>{t('Platform Connector')}<select value={form.connector_id ?? ''} onChange={(event) => setForm({ ...form, connector_id: event.target.value || null })}>
              <option value="">{t('No platform connector linked')}</option>{connectors.filter((item) => item.type === form.platform).map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}
            </select></label>
            <label>{t('Seller portal URL')}<input type="url" required placeholder="https://seller.example.com/" value={form.entry_url} onChange={(event) => setForm({ ...form, entry_url: event.target.value })} /></label>
            <label>{t('Additional login and resource domains')}<input value={domains} placeholder="example.com cdn.example.com" onChange={(event) => setDomains(event.target.value)} /></label>
          </div>
          <p>{t('Use an HTTPS portal address without query parameters. Add any required sign-in and asset domains. Close the browser before editing; changing the shop identity starts a separate login profile.')}</p>
          <button type="submit">{t('Save shop identity')}</button>
          {editing && <button type="button" onClick={() => { setEditing(undefined); setForm(empty); setDomains('') }}>{t('Cancel editing')}</button>}
        </fieldset>
      </form>
      <label>{t('Shop access duration')}<select disabled={busy} value={days} onChange={(event) => setDays(Number(event.target.value))}><option value={1}>{t('1 day')}</option><option value={7}>{t('7 days')}</option><option value={30}>{t('30 days')}</option></select></label>
      <div className="project-shops-list">{shops.map((shop) => <article key={shop.id}>
        <header><strong>{shop.name}</strong><span>{platforms[shop.platform]} · {shop.seller_shop_id}</span></header>
        <p>{t(shop.browser_state === 'open' ? 'Shop browser open' : shop.browser_state === 'starting' ? 'Shop browser opening' : 'Shop browser closed')} · {t(shop.agent_access ? 'Agent session authorized' : 'Agent session requires confirmation')}</p>
        {shop.grant_expires_at && <small>{t('Expires')}: {new Date(shop.grant_expires_at).toLocaleString()}</small>}
        <div className="project-shops-actions">
          <button type="button" disabled={busy || shop.browser_state !== 'closed'} onClick={() => void control(shop, 'open')}>{t('Open shop login browser')}</button>
          <button type="button" disabled={busy || shop.browser_state === 'closed'} onClick={() => void control(shop, 'close')}>{t('Close shop browser')}</button>
          <button type="button" disabled={busy || shop.browser_state !== 'closed'} onClick={() => { setEditing(shop); setForm({ name: shop.name, platform: shop.platform, seller_shop_id: shop.seller_shop_id, connector_id: shop.connector_id, entry_url: shop.entry_url, additional_domains: shop.additional_domains }); setDomains(shop.additional_domains.join(' ')) }}>{t('Edit shop identity')}</button>
          <button type="button" disabled={busy} onClick={() => void control(shop, 'revoke')}>{t('Revoke browser access')}</button>
        </div>
        {shop.browser_state === 'open' && <div className="project-shops-confirm">
          <label><input type="checkbox" disabled={busy} checked={!!confirmed[shop.id]} onChange={(event) => setConfirmed({ ...confirmed, [shop.id]: event.target.checked })} />{t('I checked that this browser is signed in to {shop}.', { shop: `${shop.name} (${shop.seller_shop_id})` })}</label>
          <button type="button" disabled={busy || !confirmed[shop.id]} onClick={() => void grant(shop)}>{t('Confirm identity and allow Agent reads')}</button>
        </div>}
        <div className="project-shops-actions">
          {onTask && <button type="button" disabled={busy || !shop.agent_access} onClick={() => onTask(t('Read the current page for shop {name} (shop_id: {id}), summarize its visible data, and retain the source. Ask before any page action or report download.', { name: shop.name, id: shop.id }))}>{t('Prepare shop task')}</button>}
          <button type="button" disabled={busy} onClick={() => void inspectData(shop)}>{t('View collected shop data')} ({shop.capture_count})</button>
        </div>
      </article>)}</div>
      {shops.length === 0 && <p>{t('No shop identities in this project yet.')}</p>}
      {data && <section aria-label={t('Collected shop data')}><h3>{t('Collected shop data')}</h3><p>{t('Latest 50 captures. Original reports, page text and screenshots retain source links; shop identity is confirmed by the user.')}</p>
        {data.records.map((record) => <article key={record.evidence.id}>
          <strong>{record.evidence.source_title}</strong><p>{record.evidence.retrieved_at}</p><p>{record.evidence.source_url}</p>
          <div className="project-shops-actions">{record.artifacts.map((artifact) => <a key={artifact.id} href={`${apiBaseUrl}${artifact.download_url}`}>{artifact.name}</a>)}</div>
        </article>)}
        {!data.records.length && <p>{t('Run a shop task to collect the first page or report.')}</p>}
      </section>}
    </div>}
  </section>
}
