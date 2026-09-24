import type { SchedulePermissionRule } from './types'

export type EventTriggerKind = 'webhook' | 'inquiry_email' | 'store_order'

export type EventTrigger = {
  id: string
  project_id: string
  name: string
  kind: EventTriggerKind
  shopify_binding_id?: string | null
  prompt: string
  provider_profile_id: string | null
  max_budget_usd: number
  permission_ceiling: SchedulePermissionRule[]
  enabled: boolean
  revision: number
  created_at: string
  updated_at: string
}

export type EventTriggerCreated = {
  trigger: EventTrigger
  signing_secret: string | null
}

export type EventTriggerCreate = {
  name: string
  kind: EventTriggerKind
  prompt: string
  max_budget_usd: number
  permission_ceiling: SchedulePermissionRule[]
  shopify_binding_id?: string
}

export type ShopifySubscriptionView = {
  delivery_url: string | null
  status: 'unconfigured' | 'not_registered' | 'attempting' | 'uncertain' | 'confirmed' | 'rejected' | 'origin_changed'
  subscription_id: string | null
  updated_at: string | null
}
