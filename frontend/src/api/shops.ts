import type { Artifact, Evidence } from './types'

export type ShopIdentityInput = {
  name: string
  platform: 'shein' | 'temu' | 'tiktok_shop' | 'aliexpress' | 'shopee' | 'shopify' | 'other'
  seller_shop_id: string
  connector_id: string | null
  entry_url: string
  additional_domains: string[]
}

export type ProjectShop = ShopIdentityInput & {
  id: string
  project_id: string
  revision: number
  browser_state: 'closed' | 'starting' | 'open'
  session_id: string | null
  agent_access: boolean
  browser_profile_present: boolean
  browser_login_data_present: boolean
  grant_expires_at: string | null
  capture_count: number
}

export type ShopData = { evidence: Evidence; artifacts: Artifact[] }
