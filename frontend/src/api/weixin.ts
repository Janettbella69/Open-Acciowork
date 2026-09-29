export type WeixinAccount = {
  id: string
  bot_id: string
  owner_user_id: string
  base_url: string
  tenant_id: string | null
  project_id: string | null
  enabled: boolean
  enabled_at: string | null
  revision: number
  status: 'connected' | 'revoked' | 'expired'
  last_error_code: string | null
}

export type WeixinOverview = {
  identity_mode: 'local_only' | 'trusted_gateway'
  can_manage: boolean
  available: boolean
  direct_text: boolean
  group_chat: boolean
  delivery_enabled: boolean
  accounts: WeixinAccount[]
  projects: Array<{ id: string; name: string }>
}

export type WeixinPairing = {
  id: string
  status: 'wait' | 'scaned' | 'need_verifycode' | 'verify_code_blocked' | 'confirmed'
    | 'expired' | 'cancelled' | 'unsupported_redirect' | 'binded_redirect'
  expires_at: string
  poll_after_ms: number
  qr_svg_data_url?: string
  account_id?: string
}

export type WeixinBinding = { project_id: string | null; enabled: boolean; expected_revision: number }

// Keep the server-generated image in an <img>. Never inline its SVG markup or
// follow an upstream image/login URL in the browser.
export function weixinQrImage(value: string | undefined): string | null {
  if (!value || value.length > 1_000_000) return null
  const comma = value.indexOf(',')
  const header = value.slice(0, comma).toLowerCase()
  if (!['data:image/svg+xml', 'data:image/svg+xml;base64', 'data:image/svg+xml;charset=utf-8', 'data:image/svg+xml;charset=utf-8;base64'].includes(header)) return null
  const content = value.slice(comma + 1)
  return content && (!header.endsWith(';base64') || /^[a-z0-9+/=\s]+$/i.test(content)) ? value : null
}
