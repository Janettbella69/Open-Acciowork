import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { OpportunityProject } from '../api/types'
import EventTriggersPanel from './EventTriggersPanel'

vi.mock('../api/client', () => ({
  apiBaseUrl: '',
  listEventTriggers: vi.fn(),
  createEventTrigger: vi.fn(),
  getMarketplaceAccess: vi.fn(),
  getMailgunDelivery: vi.fn(),
  getGmailWatch: vi.fn(),
  getOutlookSubscription: vi.fn(),
  reconcileOutlookSubscription: vi.fn(),
  getShopifySubscription: vi.fn(),
  updateEventTrigger: vi.fn(),
  rotateEventTriggerSecret: vi.fn(),
  registerShopifySubscription: vi.fn(),
  registerGmailWatch: vi.fn(),
  registerOutlookSubscription: vi.fn(),
  stopGmailWatch: vi.fn(),
  stopOutlookSubscription: vi.fn(),
  removeShopifySubscription: vi.fn(),
  errorMessage: (cause: unknown) => cause instanceof Error ? cause.message : String(cause),
}))

import {
  createEventTrigger,
  getMarketplaceAccess,
  getMailgunDelivery,
  getGmailWatch,
  getOutlookSubscription,
  reconcileOutlookSubscription,
  getShopifySubscription,
  listEventTriggers,
  rotateEventTriggerSecret,
  registerShopifySubscription,
  registerGmailWatch,
  registerOutlookSubscription,
  removeShopifySubscription,
  stopGmailWatch,
  stopOutlookSubscription,
  updateEventTrigger,
} from '../api/client'

const project = { id: 'project_1', name: 'Inquiry project' } as OpportunityProject
const trigger = {
  id: 'event_trigger_1', project_id: project.id, name: 'New inquiry',
  kind: 'inquiry_email' as const, prompt: 'Draft a response',
  provider_profile_id: null, max_budget_usd: 1,
  permission_ceiling: [], enabled: true, revision: 1,
  created_at: '2026-09-24T00:00:00Z', updated_at: '2026-09-24T00:00:00Z',
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listEventTriggers).mockResolvedValue([])
  vi.mocked(getMarketplaceAccess).mockResolvedValue({
    project_id: project.id, revision: 0, bindings: [],
  })
  vi.mocked(getShopifySubscription).mockResolvedValue({
    delivery_url: 'https://events.example.com/api/projects/project_1/event-triggers/event_trigger_1/shopify',
    status: 'not_registered', subscription_id: null, updated_at: null,
  })
  vi.mocked(getMailgunDelivery).mockResolvedValue({
    delivery_url: 'https://events.example.com/api/projects/project_1/event-triggers/event_trigger_1/mailgun',
  })
  vi.mocked(getGmailWatch).mockResolvedValue({
    email: 'inbox@example.com',
    push_url: 'https://events.example.com/api/connectors/gmail/gmail/push',
    status: 'not_registered', expiration_ms: null, history_id: null,
  })
  vi.mocked(getOutlookSubscription).mockResolvedValue({
    email: 'inbox@example.com',
    notification_url: 'https://events.example.com/api/connectors/outlook/outlook/notifications',
    status: 'not_registered', gap: false, subscription_id: null, expiration_at: null,
  })
})

it('creates an Outlook trigger and manages its Graph subscription', async () => {
  const outlookTrigger = { ...trigger, outlook_connector_id: 'outlook' }
  vi.mocked(createEventTrigger).mockResolvedValue({
    trigger: outlookTrigger, signing_secret: null,
  })
  vi.mocked(registerOutlookSubscription).mockResolvedValue({
    email: 'inbox@example.com',
    notification_url: 'https://events.example.com/api/connectors/outlook/outlook/notifications',
    status: 'active', gap: false, subscription_id: 'subscription-1',
    expiration_at: '2026-09-30T00:00:00Z',
  })
  vi.mocked(stopOutlookSubscription).mockResolvedValue({
    email: 'inbox@example.com',
    notification_url: 'https://events.example.com/api/connectors/outlook/outlook/notifications',
    status: 'not_registered', gap: false, subscription_id: null, expiration_at: null,
  })
  render(<EventTriggersPanel projects={[project]} />)
  await screen.findByText('No event triggers in this project yet.')
  fireEvent.change(screen.getByLabelText('Event source'), { target: { value: 'inquiry_email' } })
  fireEvent.change(screen.getByLabelText('Email delivery method'), { target: { value: 'outlook' } })
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Outlook inbox' } })
  fireEvent.change(screen.getByLabelText('Instructions for the Agent'), {
    target: { value: 'Review inquiry' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Create trigger' }))
  await waitFor(() => expect(createEventTrigger).toHaveBeenCalledWith(project.id, {
    name: 'Outlook inbox', kind: 'inquiry_email', prompt: 'Review inquiry',
    max_budget_usd: 1, permission_ceiling: [], outlook_connector_id: 'outlook',
  }))
  await waitFor(() => expect(screen.getByLabelText('Graph notification URL')).toHaveValue(
    'https://events.example.com/api/connectors/outlook/outlook/notifications',
  ))
  expect(screen.queryByRole('button', { name: 'Rotate signing key' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Register or renew Outlook subscription' }))
  await waitFor(() => expect(registerOutlookSubscription).toHaveBeenCalledWith(
    project.id, trigger.id,
  ))
  fireEvent.click(screen.getByRole('button', { name: 'Stop Outlook subscription' }))
  await waitFor(() => expect(stopOutlookSubscription).toHaveBeenCalledWith(
    project.id, trigger.id,
  ))
})

it('reconciles an uncertain Outlook registration before allowing another attempt', async () => {
  const outlookTrigger = { ...trigger, outlook_connector_id: 'outlook' }
  vi.mocked(listEventTriggers).mockResolvedValue([outlookTrigger])
  vi.mocked(getOutlookSubscription).mockResolvedValue({
    email: 'inbox@example.com',
    notification_url: 'https://events.example.com/api/connectors/outlook/outlook/notifications',
    status: 'uncertain', gap: false, subscription_id: null, expiration_at: null,
  })
  vi.mocked(reconcileOutlookSubscription).mockResolvedValue({
    email: 'inbox@example.com',
    notification_url: 'https://events.example.com/api/connectors/outlook/outlook/notifications',
    status: 'active', gap: false, subscription_id: 'subscription-1',
    expiration_at: '2026-09-30T00:00:00Z',
  })
  render(<EventTriggersPanel projects={[project]} />)
  await screen.findByRole('button', { name: 'Reconcile Outlook subscription with Graph' })
  expect(screen.queryByRole('button', {
    name: 'Register or renew Outlook subscription',
  })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Reconcile Outlook subscription with Graph' }))
  await waitFor(() => expect(reconcileOutlookSubscription).toHaveBeenCalledWith(
    project.id, trigger.id,
  ))
  await screen.findByRole('button', { name: 'Register or renew Outlook subscription' })
})

it('creates a Gmail trigger and manages the shared mailbox watch', async () => {
  const gmailTrigger = { ...trigger, gmail_connector_id: 'gmail' }
  vi.mocked(createEventTrigger).mockResolvedValue({
    trigger: gmailTrigger, signing_secret: null,
  })
  vi.mocked(registerGmailWatch).mockResolvedValue({
    email: 'inbox@example.com',
    push_url: 'https://events.example.com/api/connectors/gmail/gmail/push',
    status: 'active', expiration_ms: 1790000000000, history_id: '100',
  })
  vi.mocked(stopGmailWatch).mockResolvedValue({
    email: 'inbox@example.com',
    push_url: 'https://events.example.com/api/connectors/gmail/gmail/push',
    status: 'not_registered', expiration_ms: null, history_id: null,
  })
  render(<EventTriggersPanel projects={[project]} />)
  await screen.findByText('No event triggers in this project yet.')
  fireEvent.change(screen.getByLabelText('Event source'), { target: { value: 'inquiry_email' } })
  fireEvent.change(screen.getByLabelText('Email delivery method'), { target: { value: 'gmail' } })
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Gmail inbox' } })
  fireEvent.change(screen.getByLabelText('Instructions for the Agent'), {
    target: { value: 'Review inquiry' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Create trigger' }))
  await waitFor(() => expect(createEventTrigger).toHaveBeenCalledWith(project.id, {
    name: 'Gmail inbox', kind: 'inquiry_email', prompt: 'Review inquiry',
    max_budget_usd: 1, permission_ceiling: [], gmail_connector_id: 'gmail',
  }))
  await waitFor(() => expect(screen.getByLabelText('Pub/Sub push URL')).toHaveValue(
    'https://events.example.com/api/connectors/gmail/gmail/push',
  ))
  expect(screen.queryByRole('button', { name: 'Rotate signing key' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Register or renew Gmail watch' }))
  await waitFor(() => expect(registerGmailWatch).toHaveBeenCalledWith(
    project.id, trigger.id, false,
  ))
  fireEvent.click(screen.getByRole('button', { name: 'Stop Gmail watch' }))
  await waitFor(() => expect(stopGmailWatch).toHaveBeenCalledWith(project.id, trigger.id))
})

it('creates a Mailgun inquiry trigger with an exact recipient and replaces its private key', async () => {
  const mailgunTrigger = { ...trigger, mailgun_recipient: 'quotes@example.com' }
  vi.mocked(createEventTrigger).mockResolvedValue({
    trigger: mailgunTrigger, signing_secret: null,
  })
  vi.mocked(updateEventTrigger).mockResolvedValue({ ...mailgunTrigger, revision: 2 })
  render(<EventTriggersPanel projects={[project]} />)
  await screen.findByText('No event triggers in this project yet.')
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'Mailgun inquiries' } })
  fireEvent.change(screen.getByLabelText('Event source'), { target: { value: 'inquiry_email' } })
  fireEvent.change(screen.getByLabelText('Email delivery method'), { target: { value: 'mailgun' } })
  fireEvent.change(screen.getByLabelText('Exact Mailgun recipient'), {
    target: { value: 'quotes@example.com' },
  })
  fireEvent.change(screen.getByLabelText('Mailgun webhook signing key'), {
    target: { value: 'k'.repeat(40) },
  })
  fireEvent.change(screen.getByLabelText('Instructions for the Agent'), {
    target: { value: 'Review inquiry' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Create trigger' }))
  await waitFor(() => expect(createEventTrigger).toHaveBeenCalledWith(project.id, {
    name: 'Mailgun inquiries', kind: 'inquiry_email', prompt: 'Review inquiry',
    max_budget_usd: 1, mailgun_recipient: 'quotes@example.com',
    mailgun_signing_key: 'k'.repeat(40), permission_ceiling: [],
  }))
  await waitFor(() => expect(screen.getByLabelText('Delivery URL')).toHaveValue(
    'https://events.example.com/api/projects/project_1/event-triggers/event_trigger_1/mailgun',
  ))
  expect(screen.queryByText('k'.repeat(40))).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Rotate signing key' })).not.toBeInTheDocument()
  fireEvent.change(screen.getByLabelText('Replace Mailgun signing key'), {
    target: { value: 'z'.repeat(40) },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Save new key' }))
  await waitFor(() => expect(updateEventTrigger).toHaveBeenCalledWith(
    project.id, trigger.id, { expected_revision: 1, mailgun_signing_key: 'z'.repeat(40) },
  ))
  expect(screen.getByLabelText('Replace Mailgun signing key')).toHaveValue('')
})

it('creates a signed trigger and shows its secret only until dismissed', async () => {
  vi.mocked(createEventTrigger).mockResolvedValue({ trigger, signing_secret: 'secret-once' })
  render(<EventTriggersPanel projects={[project]} />)
  await screen.findByText('No event triggers in this project yet.')

  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'New inquiry' } })
  fireEvent.change(screen.getByLabelText('Event source'), { target: { value: 'inquiry_email' } })
  fireEvent.change(screen.getByLabelText('Instructions for the Agent'), { target: { value: 'Draft a response' } })
  fireEvent.click(screen.getByLabelText('Allow project workspace Write/Edit after human approval'))
  fireEvent.click(screen.getByLabelText('Allow commercial proposals after human approval; applying them requires a separate review'))
  fireEvent.click(screen.getByRole('button', { name: 'Create trigger' }))

  await waitFor(() => expect(createEventTrigger).toHaveBeenCalledWith(project.id, {
    name: 'New inquiry', kind: 'inquiry_email', prompt: 'Draft a response',
    max_budget_usd: 1,
    permission_ceiling: [
      { tool_name: 'Write', scope: 'path:.' },
      { tool_name: 'Edit', scope: 'path:.' },
      { tool_name: 'mcp__ophira_commercial_actions__stage_commercial_action', scope: 'path:.' },
    ],
  }))
  expect(screen.getByText('secret-once')).toBeInTheDocument()
  expect(screen.getByLabelText('Delivery URL')).toHaveValue(
    'http://localhost:3000/api/projects/project_1/event-triggers/event_trigger_1/deliver',
  )
  fireEvent.click(screen.getByRole('button', { name: 'I saved the key' }))
  expect(screen.queryByText('secret-once')).not.toBeInTheDocument()
})

it('pauses and rotates a configured trigger', async () => {
  vi.mocked(listEventTriggers).mockResolvedValue([trigger])
  vi.mocked(updateEventTrigger).mockResolvedValue({ ...trigger, enabled: false, revision: 2 })
  vi.mocked(rotateEventTriggerSecret).mockResolvedValue({
    trigger: { ...trigger, enabled: false, revision: 3 }, signing_secret: 'new-secret',
  })
  render(<EventTriggersPanel projects={[project]} />)
  await screen.findByText('New inquiry')
  fireEvent.click(screen.getByRole('button', { name: 'Pause' }))
  await waitFor(() => expect(updateEventTrigger).toHaveBeenCalledWith(
    project.id, trigger.id, { expected_revision: 1, enabled: false },
  ))
  fireEvent.click(screen.getByRole('button', { name: 'Rotate signing key' }))
  await screen.findByText('new-secret')
  expect(rotateEventTriggerSecret).toHaveBeenCalledWith(project.id, trigger.id)
})

it('creates a Shopify order trigger from an active shop grant without showing a key', async () => {
  const bindingId = 'marketplace_binding_' + 'a'.repeat(32)
  vi.mocked(getMarketplaceAccess).mockResolvedValue({
    project_id: project.id, revision: 1, bindings: [{
      id: bindingId, connector_id: 'shopify', shop_id: '1234', shop_name: 'Skate Demo',
      region: 'US', created_by: 'local_owner', created_at: '2026-09-24T00:00:00Z',
      expires_at: '2026-10-01T00:00:00Z', revoked_at: null, revoked_by: null,
      active: true,
    }],
  })
  const shopifyTrigger = {
    ...trigger, kind: 'store_order' as const, shopify_binding_id: bindingId,
  }
  vi.mocked(createEventTrigger).mockResolvedValue({
    trigger: shopifyTrigger, signing_secret: null,
  })
  render(<EventTriggersPanel projects={[project]} />)
  await screen.findByText('No event triggers in this project yet.')
  fireEvent.change(screen.getByLabelText('Event source'), { target: { value: 'store_order' } })
  fireEvent.change(screen.getByLabelText('Order delivery method'), { target: { value: 'shopify' } })
  await screen.findByRole('option', { name: 'Skate Demo · 1234' })
  fireEvent.change(screen.getByLabelText('Shopify shop grant'), { target: { value: bindingId } })
  fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'New order' } })
  fireEvent.change(screen.getByLabelText('Instructions for the Agent'), {
    target: { value: 'Review the order' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Create trigger' }))
  await waitFor(() => expect(createEventTrigger).toHaveBeenCalledWith(project.id, {
    name: 'New order', kind: 'store_order', prompt: 'Review the order',
    max_budget_usd: 1, permission_ceiling: [], shopify_binding_id: bindingId,
  }))
  expect(screen.getByLabelText('Delivery URL')).toHaveValue(
    'https://events.example.com/api/projects/project_1/event-triggers/event_trigger_1/shopify',
  )
  vi.mocked(registerShopifySubscription).mockResolvedValue({
    delivery_url: 'https://events.example.com/api/projects/project_1/event-triggers/event_trigger_1/shopify',
    status: 'confirmed', subscription_id: 'gid://shopify/WebhookSubscription/123',
    updated_at: '2026-09-24T00:00:00Z',
  })
  fireEvent.click(screen.getByRole('button', { name: 'Register or verify with Shopify' }))
  await waitFor(() => expect(registerShopifySubscription).toHaveBeenCalledWith(
    project.id, shopifyTrigger.id, 1, false,
  ))
  await screen.findByText('Last confirmed with Shopify.')
  vi.mocked(removeShopifySubscription).mockResolvedValue({
    delivery_url: 'https://events.example.com/api/projects/project_1/event-triggers/event_trigger_1/shopify',
    status: 'not_registered', subscription_id: null, updated_at: null,
  })
  fireEvent.click(screen.getByRole('button', { name: 'Remove Shopify subscription' }))
  await waitFor(() => expect(removeShopifySubscription).toHaveBeenCalledWith(
    project.id, shopifyTrigger.id, 1,
  ))
  await screen.findByText('Not registered with Shopify.')
  expect(screen.queryByText('Signing key — shown once')).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Rotate signing key' })).not.toBeInTheDocument()
})

it('requires an explicit retry after an uncertain Shopify registration', async () => {
  const bindingId = 'marketplace_binding_' + 'b'.repeat(32)
  vi.mocked(listEventTriggers).mockResolvedValue([{
    ...trigger, kind: 'store_order', shopify_binding_id: bindingId,
  }])
  vi.mocked(getShopifySubscription).mockResolvedValue({
    delivery_url: 'https://events.example.com/shopify',
    status: 'uncertain', subscription_id: null,
    updated_at: '2026-09-24T00:00:00Z',
  })
  vi.mocked(registerShopifySubscription).mockResolvedValue({
    delivery_url: 'https://events.example.com/shopify',
    status: 'confirmed', subscription_id: 'gid://shopify/WebhookSubscription/123',
    updated_at: '2026-09-24T00:01:00Z',
  })
  render(<EventTriggersPanel projects={[project]} />)
  const retry = await screen.findByRole('button', {
    name: 'Retry registration after checking Shopify',
  })
  fireEvent.click(retry)
  await waitFor(() => expect(registerShopifySubscription).toHaveBeenCalledWith(
    project.id, trigger.id, 1, true,
  ))
})

it('keeps Shopify removal available when the public origin is removed', async () => {
  const bindingId = 'marketplace_binding_' + 'c'.repeat(32)
  vi.mocked(listEventTriggers).mockResolvedValue([{
    ...trigger, kind: 'store_order', shopify_binding_id: bindingId,
  }])
  vi.mocked(getShopifySubscription).mockResolvedValue({
    delivery_url: null, status: 'origin_changed',
    subscription_id: 'gid://shopify/WebhookSubscription/123',
    updated_at: '2026-09-24T00:00:00Z',
  })
  vi.mocked(removeShopifySubscription).mockResolvedValue({
    delivery_url: null, status: 'unconfigured', subscription_id: null, updated_at: null,
  })
  render(<EventTriggersPanel projects={[project]} />)
  fireEvent.click(await screen.findByRole('button', { name: 'Remove Shopify subscription' }))
  await waitFor(() => expect(removeShopifySubscription).toHaveBeenCalledWith(
    project.id, trigger.id, 1,
  ))
})
