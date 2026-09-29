import type { WeixinAccount, WeixinOverview, WeixinPairing } from '../api/weixin'

export const weixinAccount: WeixinAccount = {
  id: 'account_1', bot_id: 'wechat-bot-1', owner_user_id: 'wechat-user-1',
  base_url: 'https://example.invalid', tenant_id: null, project_id: null,
  enabled: false, enabled_at: null, revision: 1, status: 'connected', last_error_code: null,
}
export const weixinOverview: WeixinOverview = {
  identity_mode: 'local_only', can_manage: true, available: true,
  direct_text: true, group_chat: false, delivery_enabled: false,
  accounts: [], projects: [{ id: 'project_1', name: 'Local project' }, { id: 'project_2', name: 'Another project' }],
}
export const weixinPairing: WeixinPairing = {
  id: 'pairing_1', status: 'wait', expires_at: '2026-09-30T12:05:00Z', poll_after_ms: 1000,
  qr_svg_data_url: 'data:image/svg+xml;base64,PHN2Zy8+',
}
