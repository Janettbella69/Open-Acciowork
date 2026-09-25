import { useEffect, useState, type FormEvent } from 'react'
import {
  apiBaseUrl,
  createEventTrigger,
  errorMessage,
  getMailgunDelivery,
  getMarketplaceAccess,
  getShopifySubscription,
  listEventTriggers,
  registerShopifySubscription,
  removeShopifySubscription,
  rotateEventTriggerSecret,
  updateEventTrigger,
} from '../api/client'
import type { EventTrigger, EventTriggerKind, ShopifySubscriptionView } from '../api/eventTriggers'
import type { MarketplaceBinding } from '../api/marketplaces'
import type { OpportunityProject, SchedulePermissionRule } from '../api/types'
import { useT } from '../i18n'
import './event-triggers-panel.css'

type SecretView = { triggerId: string; value: string }

const workspaceFileRules: SchedulePermissionRule[] = [
  { tool_name: 'Write', scope: 'path:.' },
  { tool_name: 'Edit', scope: 'path:.' },
]
const commercialProposalRule: SchedulePermissionRule = {
  tool_name: 'mcp__ophira_commercial_actions__stage_commercial_action', scope: 'path:.',
}

const kindLabels: Record<EventTriggerKind, string> = {
  webhook: 'Webhook',
  inquiry_email: 'Inquiry email',
  store_order: 'Store order',
}

const payloadExamples: Record<EventTriggerKind, string> = {
  webhook: '{"signal":"new event"}',
  inquiry_email: '{"message_id":"mail-123","from":"buyer@example.com","subject":"Quote","body":"Please quote"}',
  store_order: '{"order_id":"order-123","shop_id":"shop-1","status":"paid"}',
}

export default function EventTriggersPanel({ projects }: { projects: OpportunityProject[] }) {
  const t = useT()
  const [selectedProjectId, setSelectedProjectId] = useState('')
  const projectId = selectedProjectId || projects[0]?.id || ''
  const [triggers, setTriggers] = useState<EventTrigger[]>([])
  const [shopifySubscriptions, setShopifySubscriptions] = useState<Record<string, ShopifySubscriptionView>>({})
  const [subscriptionErrors, setSubscriptionErrors] = useState<Record<string, string>>({})
  const [name, setName] = useState('')
  const [kind, setKind] = useState<EventTriggerKind>('webhook')
  const [orderSource, setOrderSource] = useState<'signed' | 'shopify'>('signed')
  const [emailSource, setEmailSource] = useState<'signed' | 'mailgun'>('signed')
  const [mailgunRecipient, setMailgunRecipient] = useState('')
  const [mailgunSigningKey, setMailgunSigningKey] = useState('')
  const [replacementKeys, setReplacementKeys] = useState<Record<string, string>>({})
  const [mailgunDeliveryUrls, setMailgunDeliveryUrls] = useState<Record<string, string | null>>({})
  const [shopifyBindingId, setShopifyBindingId] = useState('')
  const [shopifyBindings, setShopifyBindings] = useState<MarketplaceBinding[]>([])
  const [shopifyAccessError, setShopifyAccessError] = useState('')
  const [prompt, setPrompt] = useState('')
  const [budget, setBudget] = useState('1')
  const [fileAccess, setFileAccess] = useState(false)
  const [commercialProposals, setCommercialProposals] = useState(false)
  const [secret, setSecret] = useState<SecretView | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  useEffect(() => {
    if (!projectId) return
    let active = true
    void listEventTriggers(projectId).then((items) => {
      if (active) {
        setTriggers(items)
        for (const trigger of items.filter((item) => item.shopify_binding_id)) {
          void getShopifySubscription(projectId, trigger.id).then((view) => {
            if (active) setShopifySubscriptions((current) => ({ ...current, [trigger.id]: view }))
          }).catch((cause: unknown) => {
            if (active) setSubscriptionErrors((current) => ({
              ...current, [trigger.id]: errorMessage(cause),
            }))
          })
        }
        for (const trigger of items.filter((item) => item.mailgun_recipient)) {
          void getMailgunDelivery(projectId, trigger.id).then((view) => {
            if (active) setMailgunDeliveryUrls((current) => ({
              ...current, [trigger.id]: view.delivery_url,
            }))
          }).catch((cause: unknown) => {
            if (active) setSubscriptionErrors((current) => ({
              ...current, [trigger.id]: errorMessage(cause),
            }))
          })
        }
      }
    }).catch((cause: unknown) => {
      if (active) setError(errorMessage(cause))
    })
    void getMarketplaceAccess(projectId).then((access) => {
      if (active) {
        setShopifyAccessError('')
        setShopifyBindings(access.bindings.filter((binding) => (
          binding.active && binding.connector_id === 'shopify'
        )))
      }
    }).catch((cause: unknown) => {
      if (active) {
        setShopifyBindings([])
        setShopifyAccessError(errorMessage(cause))
      }
    })
    return () => { active = false }
  }, [projectId])

  async function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!projectId || busy) return
    setBusy(true)
    setError('')
    setNotice('')
    setSecret(null)
    try {
      const created = await createEventTrigger(projectId, {
        name: name.trim(), kind, prompt: prompt.trim(),
        max_budget_usd: Number(budget),
        ...(kind === 'store_order' && orderSource === 'shopify'
          ? { shopify_binding_id: shopifyBindingId } : {}),
        ...(kind === 'inquiry_email' && emailSource === 'mailgun'
          ? {
            mailgun_recipient: mailgunRecipient.trim(),
            mailgun_signing_key: mailgunSigningKey.trim(),
          } : {}),
        permission_ceiling: [
          ...(fileAccess ? workspaceFileRules : []),
          ...(commercialProposals ? [commercialProposalRule] : []),
        ],
      })
      setTriggers((current) => [created.trigger, ...current])
      if (created.trigger.shopify_binding_id) {
        void getShopifySubscription(projectId, created.trigger.id).then((view) => {
          setShopifySubscriptions((current) => ({ ...current, [created.trigger.id]: view }))
        }).catch((cause: unknown) => {
          setSubscriptionErrors((current) => ({
            ...current, [created.trigger.id]: errorMessage(cause),
          }))
        })
      }
      if (created.trigger.mailgun_recipient) {
        void getMailgunDelivery(projectId, created.trigger.id).then((view) => {
          setMailgunDeliveryUrls((current) => ({
            ...current, [created.trigger.id]: view.delivery_url,
          }))
        }).catch((cause: unknown) => {
          setSubscriptionErrors((current) => ({
            ...current, [created.trigger.id]: errorMessage(cause),
          }))
        })
      }
      if (created.signing_secret) {
        setSecret({ triggerId: created.trigger.id, value: created.signing_secret })
      }
      setName('')
      setPrompt('')
      setMailgunSigningKey('')
      setNotice(t(created.trigger.mailgun_recipient
        ? 'Mailgun trigger created. Add its HTTPS Delivery URL to an inbound Mailgun route.'
        : created.signing_secret
          ? 'Event trigger created. Save the signing key now.'
          : 'Shopify trigger created. Register the orders/create webhook using its Delivery URL.'))
    } catch (cause) {
      setError(errorMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  async function toggle(trigger: EventTrigger) {
    if (busy) return
    setBusy(true)
    setError('')
    try {
      const updated = await updateEventTrigger(projectId, trigger.id, {
        expected_revision: trigger.revision, enabled: !trigger.enabled,
      })
      setTriggers((current) => current.map((item) => item.id === trigger.id ? updated : item))
      if (!updated.enabled) setSecret(null)
    } catch (cause) {
      setError(errorMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  async function rotate(trigger: EventTrigger) {
    if (busy) return
    setBusy(true)
    setError('')
    setNotice('')
    setSecret(null)
    try {
      const rotated = await rotateEventTriggerSecret(projectId, trigger.id)
      if (!rotated.signing_secret) throw new Error('Signing key was not returned')
      setTriggers((current) => current.map((item) => item.id === trigger.id ? rotated.trigger : item))
      setSecret({ triggerId: trigger.id, value: rotated.signing_secret })
      setNotice(t('Signing key rotated. Update the sender before its next event.'))
    } catch (cause) {
      setError(errorMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  async function replaceMailgunKey(trigger: EventTrigger) {
    const key = replacementKeys[trigger.id]?.trim()
    if (!key || busy) return
    setBusy(true)
    setError('')
    try {
      const updated = await updateEventTrigger(projectId, trigger.id, {
        expected_revision: trigger.revision, mailgun_signing_key: key,
      })
      setTriggers((current) => current.map((item) => item.id === trigger.id ? updated : item))
      setReplacementKeys((current) => ({ ...current, [trigger.id]: '' }))
      setNotice(t('Mailgun signing key replaced.'))
    } catch (cause) {
      setError(errorMessage(cause))
    } finally {
      setBusy(false)
    }
  }

  async function registerShopify(trigger: EventTrigger, retryUncertain = false) {
    if (busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const view = await registerShopifySubscription(
        projectId, trigger.id, trigger.revision, retryUncertain,
      )
      setShopifySubscriptions((current) => ({ ...current, [trigger.id]: view }))
      setSubscriptionErrors((current) => ({ ...current, [trigger.id]: '' }))
      setNotice(t(view.status === 'confirmed'
        ? 'Shopify order subscription confirmed.'
        : 'Shopify registration outcome is uncertain. Check Shopify before retrying.'))
    } catch (cause) {
      setError(errorMessage(cause))
      void getShopifySubscription(projectId, trigger.id).then((view) => {
        setShopifySubscriptions((current) => ({ ...current, [trigger.id]: view }))
      }).catch(() => undefined)
    } finally {
      setBusy(false)
    }
  }

  async function removeShopify(trigger: EventTrigger) {
    if (busy) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const view = await removeShopifySubscription(projectId, trigger.id, trigger.revision)
      setShopifySubscriptions((current) => ({ ...current, [trigger.id]: view }))
      setNotice(t('Shopify order subscription removed.'))
    } catch (cause) {
      setError(errorMessage(cause))
      void getShopifySubscription(projectId, trigger.id).then((view) => {
        setShopifySubscriptions((current) => ({ ...current, [trigger.id]: view }))
      }).catch(() => undefined)
    } finally {
      setBusy(false)
    }
  }

  function endpoint(trigger: EventTrigger): string {
    const publicUrl = shopifySubscriptions[trigger.id]?.delivery_url
    if (trigger.shopify_binding_id && publicUrl) return publicUrl
    const mailgunUrl = mailgunDeliveryUrls[trigger.id]
    if (trigger.mailgun_recipient && mailgunUrl) return mailgunUrl
    const suffix = trigger.shopify_binding_id
      ? 'shopify' : trigger.mailgun_recipient ? 'mailgun' : 'deliver'
    const path = `/api/projects/${encodeURIComponent(projectId)}/event-triggers/${encodeURIComponent(trigger.id)}/${suffix}`
    return `${apiBaseUrl || window.location.origin}${path}`
  }

  return (
    <section className="event-triggers" aria-labelledby="event-triggers-heading">
      <header>
        <div>
          <span>{t('External events')}</span>
          <h2 id="event-triggers-heading">{t('Event triggers')}</h2>
          <p>{t('A signed webhook can start a project Run when an external system sends a signal, inquiry email, or store order.')}</p>
        </div>
        <label>{t('Project')}
          <select value={projectId} onChange={(event) => {
            setSelectedProjectId(event.target.value)
            setSecret(null)
            setError('')
            setShopifyBindingId('')
            setShopifyBindings([])
            setShopifyAccessError('')
            setShopifySubscriptions({})
            setSubscriptionErrors({})
            setMailgunDeliveryUrls({})
            setMailgunSigningKey('')
            setReplacementKeys({})
            setTriggers([])
          }} disabled={!projects.length || busy}>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.name}</option>)}
          </select>
        </label>
      </header>
      {error && <p className="event-triggers-error" role="alert">{error}</p>}
      {notice && <p className="event-triggers-notice" role="status">{notice}</p>}

      {projectId && <>
        <form className="event-triggers-create" onSubmit={(event) => void create(event)}>
          <h3>{t('Create a trigger')}</h3>
          <div className="event-triggers-fields">
            <label>{t('Name')}<input value={name} onChange={(event) => setName(event.target.value)} maxLength={160} required /></label>
            <label>{t('Event source')}
              <select value={kind} onChange={(event) => setKind(event.target.value as EventTriggerKind)}>
                {(Object.keys(kindLabels) as EventTriggerKind[]).map((value) => (
                  <option key={value} value={value}>{t(kindLabels[value])}</option>
                ))}
              </select>
            </label>
            <label>{t('Budget per Run (USD)')}<input type="number" min="0.01" max="1000" step="0.01" value={budget} onChange={(event) => setBudget(event.target.value)} required /></label>
          </div>
          {kind === 'store_order' && <label>{t('Order delivery method')}
            <select value={orderSource} onChange={(event) => setOrderSource(event.target.value as 'signed' | 'shopify')}>
              <option value="signed">{t('Signed JSON from an integration')}</option>
              <option value="shopify">{t('Shopify orders/create webhook')}</option>
            </select>
          </label>}
          {kind === 'store_order' && orderSource === 'shopify' && <>
            <label>{t('Shopify shop grant')}
              <select value={shopifyBindingId} onChange={(event) => setShopifyBindingId(event.target.value)} required>
                <option value="">{t('Choose a shop')}</option>
                {shopifyBindings.map((binding) => <option key={binding.id} value={binding.id}>{binding.shop_name} · {binding.shop_id}</option>)}
              </select>
            </label>
            {shopifyAccessError && <p role="alert">{shopifyAccessError}</p>}
            {!shopifyAccessError && !shopifyBindings.length && <p>{t('Configure Shopify and grant this project access to the shop before creating a trigger.')}</p>}
          </>}
          {kind === 'inquiry_email' && <label>{t('Email delivery method')}
            <select value={emailSource} onChange={(event) => setEmailSource(event.target.value as 'signed' | 'mailgun')}>
              <option value="signed">{t('Signed JSON from an integration')}</option>
              <option value="mailgun">{t('Mailgun inbound route')}</option>
            </select>
          </label>}
          {kind === 'inquiry_email' && emailSource === 'mailgun' && <>
            <label>{t('Exact Mailgun recipient')}
              <input type="email" value={mailgunRecipient} onChange={(event) => setMailgunRecipient(event.target.value)} maxLength={255} required placeholder="quotes@example.com" />
            </label>
            <label>{t('Mailgun webhook signing key')}
              <input type="password" value={mailgunSigningKey} onChange={(event) => setMailgunSigningKey(event.target.value)} minLength={32} maxLength={256} autoComplete="off" required />
            </label>
            <p>{t('Create a Mailgun inbound route for this exact recipient and forward it to the HTTPS Delivery URL. Use the account webhook signing key, not an API key.')}</p>
          </>}
          <label>{t('Instructions for the Agent')}
            <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={3} maxLength={20000} required placeholder={t('For each event, summarize the facts and prepare the next action for review.')} />
          </label>
          {((kind !== 'store_order' || orderSource === 'signed')
            && (kind !== 'inquiry_email' || emailSource === 'signed')) && <div className="event-triggers-payload">
            <span>{t('Expected signed JSON body')}</span>
            <code>{payloadExamples[kind]}</code>
          </div>}
          <label className="event-triggers-check">
            <input type="checkbox" checked={fileAccess} onChange={(event) => setFileAccess(event.target.checked)} />
            <span>{t('Allow project workspace Write/Edit after human approval')}</span>
          </label>
          <label className="event-triggers-check">
            <input type="checkbox" checked={commercialProposals} onChange={(event) => setCommercialProposals(event.target.checked)} />
            <span>{t('Allow commercial proposals after human approval; applying them requires a separate review')}</span>
          </label>
          <p>{t('Shell commands and actions outside the permission ceiling are blocked. Outbound business actions still require their separate review.')}</p>
          <button type="submit" disabled={busy}>{busy ? t('Creating…') : t('Create trigger')}</button>
        </form>

        <div className="event-triggers-list">
          <h3>{t('Configured triggers')} <small>{triggers.length}</small></h3>
          {!triggers.length && <p>{t('No event triggers in this project yet.')}</p>}
          {triggers.map((trigger) => (
            <article key={trigger.id}>
              <div className="event-triggers-title">
                <strong>{trigger.name}</strong>
                <span>{trigger.shopify_binding_id ? t('Shopify orders/create webhook') : trigger.mailgun_recipient ? t('Mailgun inbound route') : t(kindLabels[trigger.kind])} · {trigger.enabled ? t('Enabled') : t('Paused')} · ${trigger.max_budget_usd.toFixed(2)} / {t('Run')}</span>
              </div>
              <p>{trigger.prompt}</p>
              <label>{t('Delivery URL')}<input readOnly value={endpoint(trigger)} onFocus={(event) => event.target.select()} /></label>
              {trigger.shopify_binding_id && <p>{t('Use Register or verify with Shopify to subscribe this HTTPS orders/create URL. The app client secret verifies deliveries; keep the shop grant active.')}</p>}
              {trigger.shopify_binding_id && !endpoint(trigger).startsWith('https://') && <p>{t('This local HTTP URL cannot receive Shopify deliveries; publish the endpoint over HTTPS first.')}</p>}
              {trigger.mailgun_recipient && <p>{t('Mailgun recipient')}: {trigger.mailgun_recipient}</p>}
              {trigger.mailgun_recipient && !endpoint(trigger).startsWith('https://') && <p>{t('This local HTTP URL cannot receive Mailgun deliveries; configure a public HTTPS event origin first.')}</p>}
              {trigger.mailgun_recipient && subscriptionErrors[trigger.id] && <p role="alert">{subscriptionErrors[trigger.id]}</p>}
              {trigger.shopify_binding_id && subscriptionErrors[trigger.id] && <p role="alert">{subscriptionErrors[trigger.id]}</p>}
              {trigger.shopify_binding_id && shopifySubscriptions[trigger.id] && <p>{t({
                unconfigured: 'Public HTTPS event origin is not configured.',
                not_registered: 'Not registered with Shopify.',
                attempting: 'Shopify registration outcome is uncertain. Check Shopify before retrying.',
                uncertain: 'Shopify registration outcome is uncertain. Check Shopify before retrying.',
                confirmed: 'Last confirmed with Shopify.',
                rejected: 'Shopify rejected the last registration attempt.',
                origin_changed: 'Public event origin changed; remove the old Shopify subscription first.',
              }[shopifySubscriptions[trigger.id].status])}</p>}
              <small>{trigger.permission_ceiling.length
                ? [
                  trigger.permission_ceiling.some((rule) => ['Write', 'Edit'].includes(rule.tool_name))
                    ? t('Workspace Write/Edit requires approval') : null,
                  trigger.permission_ceiling.some((rule) => rule.tool_name === commercialProposalRule.tool_name)
                    ? t('Commercial proposals require approval') : null,
                ].filter(Boolean).join(' · ')
                : t('Read-only permission ceiling')}</small>
              <div className="event-triggers-actions">
                <button type="button" disabled={busy} onClick={() => void toggle(trigger)}>{trigger.enabled ? t('Pause') : t('Resume')}</button>
                {!trigger.shopify_binding_id && !trigger.mailgun_recipient && <button type="button" disabled={busy} onClick={() => void rotate(trigger)}>{t('Rotate signing key')}</button>}
                {trigger.shopify_binding_id && shopifySubscriptions[trigger.id]
                  && !['unconfigured', 'origin_changed'].includes(shopifySubscriptions[trigger.id].status)
                  && <button type="button" disabled={busy || !trigger.enabled} onClick={() => void registerShopify(
                    trigger,
                    ['attempting', 'uncertain'].includes(shopifySubscriptions[trigger.id].status),
                  )}>{t(['attempting', 'uncertain'].includes(shopifySubscriptions[trigger.id].status)
                      ? 'Retry registration after checking Shopify'
                      : 'Register or verify with Shopify')}</button>}
                {trigger.shopify_binding_id && shopifySubscriptions[trigger.id]
                  && shopifySubscriptions[trigger.id].status !== 'not_registered'
                  && (shopifySubscriptions[trigger.id].status !== 'unconfigured'
                    || Boolean(shopifySubscriptions[trigger.id].subscription_id))
                  && <button type="button" disabled={busy} onClick={() => void removeShopify(trigger)}>{t('Remove Shopify subscription')}</button>}
              </div>
              {trigger.mailgun_recipient && <div className="event-triggers-actions">
                <label>{t('Replace Mailgun signing key')}
                  <input type="password" value={replacementKeys[trigger.id] || ''} onChange={(event) => setReplacementKeys((current) => ({ ...current, [trigger.id]: event.target.value }))} minLength={32} maxLength={256} autoComplete="off" />
                </label>
                <button type="button" disabled={busy || (replacementKeys[trigger.id]?.length || 0) < 32} onClick={() => void replaceMailgunKey(trigger)}>{t('Save new key')}</button>
              </div>}
              {secret?.triggerId === trigger.id && <div className="event-triggers-secret">
                <strong>{t('Signing key — shown once')}</strong>
                <code>{secret.value}</code>
                <button type="button" onClick={() => void navigator.clipboard.writeText(secret.value).then(() => setNotice(t('Signing key copied.'))).catch(() => setError(t('Clipboard unavailable. Copy the key manually.')))}>{t('Copy key')}</button>
                <p>{t('Send JSON with X-Ophira-Timestamp (Unix seconds), X-Ophira-Event-Id, and X-Ophira-Signature. The signature is sha256=HMAC-SHA256(key, timestamp + newline + event ID + newline + raw body). Timestamps expire after five minutes; event IDs are deduplicated.')}</p>
                <button type="button" onClick={() => setSecret(null)}>{t('I saved the key')}</button>
              </div>}
            </article>
          ))}
        </div>
      </>}
    </section>
  )
}
