export type MarketplaceGuide = {
  type: string
  name: string
  read_adapter: boolean
  documentation_url: string
}

export type MarketplaceProduct = {
  id: string
  title: string
  status: string
  vendor?: string
  product_type?: string
  updated_at?: string
  category_id?: string
  variants?: { id: string; title: string; status: string }[]
}

export type MarketplaceOrder = {
  id: string
  name: string
  created_at: string
  cancelled_at: string | null
  test: boolean
  financial_status: string | null
  fulfillment_status: string
  item_quantity: number
  total_amount: string
  currency: string
}

export type MarketplaceRead = {
  connector_id: string
  operation: 'authorized_shops' | 'products' | 'orders'
  shop_id: string | null
  shops: { id: string; name: string; region: string; seller_type: string; domain?: string; currency?: string }[]
  products: MarketplaceProduct[]
  orders?: MarketplaceOrder[]
  granted_scopes?: string[]
  order_window?: 'last_60_days' | 'all'
  next_page_token: string | null
  pagination_unit?: 'SPU'
  page_number?: number
  total_count?: number
  source_url: string
  retrieved_at: string
  request_id: string
  data_scope: 'authorized_seller_data'
}

export type MarketplaceReadInput = {
  operation: 'authorized_shops' | 'products' | 'orders'
  shop_id?: string
  page_size?: number
  page_token?: string
  product_status?: string
}

// Configured permission a connector needs before a project can grant Agent shop reads.
export const PRODUCT_READ_SCOPE: Record<string, string> = {
  tiktok_shop: 'seller.product.basic',
  shopify: 'read_products',
  shein: 'shein:products:read',
}

export type MarketplaceBinding = {
  id: string
  connector_id: string
  shop_id: string
  shop_name: string
  region: string
  created_by: string
  created_at: string
  expires_at: string
  revoked_at: string | null
  revoked_by: string | null
  active: boolean
}

export type MarketplaceAccess = {
  project_id: string
  revision: number
  bindings: MarketplaceBinding[]
}
