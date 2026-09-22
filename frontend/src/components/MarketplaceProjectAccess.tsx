import { useEffect, useRef, useState } from 'react'
import { errorMessage, getMarketplaceAccess, grantMarketplaceAccess, listProjects, revokeMarketplaceAccess } from '../api/client'
import type { Connector, OpportunityProject } from '../api/types'
import { PRODUCT_READ_SCOPE, type MarketplaceAccess } from '../api/marketplaces'
import { useT } from '../i18n'

type Props = {
  connector: Connector
  selectedShop: { id: string; name: string } | null
  busy: boolean
  onBusyChange: (busy: boolean) => void
}

export default function MarketplaceProjectAccess({ connector, selectedShop, busy, onBusyChange }: Props) {
  const t = useT()
  const [projects, setProjects] = useState<OpportunityProject[] | null>(null)
  const [projectId, setProjectId] = useState('')
  const [access, setAccess] = useState<MarketplaceAccess | null>(null)
  const [days, setDays] = useState(7)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const pending = useRef(false)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  const act = async (action: () => Promise<void>) => {
    if (busy || pending.current) return
    pending.current = true
    onBusyChange(true)
    setError(''); setNotice('')
    try {
      await action()
    } catch (reason) {
      if (mounted.current) setError(errorMessage(reason))
    } finally {
      pending.current = false
      if (mounted.current) onBusyChange(false)
    }
  }
  const loadProjects = () => act(async () => {
    const result = await listProjects()
    if (mounted.current) setProjects(result)
  })
  const loadAccess = (id: string) => act(async () => {
    setProjectId(id); setAccess(null)
    if (!id) return
    const result = await getMarketplaceAccess(id)
    if (mounted.current) setAccess(result)
  })
  const grant = () => act(async () => {
    if (!selectedShop || !access || access.project_id !== projectId) return
    const shopName = selectedShop.name
    const result = await grantMarketplaceAccess(projectId, {
      expected_revision: access.revision, connector_id: connector.id, shop_id: selectedShop.id,
      expires_in_seconds: days * 86400,
    })
    if (!mounted.current) return
    setAccess(result)
    setNotice(t('Agent product reads allowed for {shop} in this project.', { shop: shopName }))
  })
  const revoke = (bindingId: string) => act(async () => {
    if (!access || access.project_id !== projectId) return
    const result = await revokeMarketplaceAccess(projectId, bindingId, access.revision)
    if (!mounted.current) return
    setAccess(result)
    setNotice(t('Project shop access revoked. Existing source history is retained.'))
  })
  const bindings = access?.project_id === projectId ? access.bindings.filter((item) => item.connector_id === connector.id) : []
  const productScope = PRODUCT_READ_SCOPE[connector.type]
  const canGrant = connector.enabled && Boolean(productScope) && connector.scope.split(/\s+/).includes(productScope)

  return <details className="marketplace-project-access" onToggle={(event) => {
    if (event.currentTarget.open && projects === null) void loadProjects()
  }}>
    <summary>{t('Project access for Agents')}</summary>
    <p>{t('Allow project Agents to read one selected shop until expiry, including scheduled runs. Product pages retain source receipts; evidence is recorded separately. Only project administrators can change grants.')}</p>
    {error && <p role="alert" className="connectors-error">{error}</p>}
    {notice && <p role="status">{notice}</p>}
    <fieldset disabled={busy}>
      <label>{t('Project for shop access')}<select value={projectId} onChange={(event) => void loadAccess(event.target.value)}>
        <option value="">{t('Select a project')}</option>
        {projects?.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
      </select></label>
      <button type="button" onClick={() => void (projectId ? loadAccess(projectId) : loadProjects())}>{t('Refresh shop grants')}</button>
      {projects?.length === 0 && <p>{t('Create a saved project before granting shop access.')}</p>}
      <label>{t('Shop access duration')}<select value={days} onChange={(event) => setDays(Number(event.target.value))}>
        <option value={1}>{t('1 day')}</option><option value={7}>{t('7 days')}</option><option value={30}>{t('30 days')}</option>
      </select></label>
      <button type="button" disabled={!selectedShop || !access || !canGrant} onClick={() => void grant()}>{t('Allow Agent reads for selected shop')}</button>
    </fieldset>
    <p>{selectedShop ? t('Selected shop: {shop}', { shop: selectedShop.name }) : t('Read authorized shops and select one above to grant access. You can revoke existing grants without reading the shop again.')}</p>
    {access && <div className="marketplace-table"><table>
      <caption>{t('Project shop grants')}</caption>
      <thead><tr><th>{t('Shop')}</th><th>{t('Expires')}</th><th>{t('Status')}</th><th>{t('Action')}</th></tr></thead>
      <tbody>{bindings.map((binding) => <tr key={binding.id}>
        <td>{binding.shop_name}<br /><small>{binding.shop_id}</small></td>
        <td>{new Date(binding.expires_at).toLocaleString()}</td>
        <td>{t(binding.revoked_at ? 'Revoked' : binding.active && new Date(binding.expires_at).getTime() > Date.now() ? 'Active' : 'Expired')}</td>
        <td>{!binding.revoked_at && <button type="button" disabled={busy} onClick={() => void revoke(binding.id)}>{t('Revoke project access')}</button>}</td>
      </tr>)}</tbody>
    </table>{bindings.length === 0 && <p>{t('No shop grants for this connector in the selected project.')}</p>}</div>}
  </details>
}
