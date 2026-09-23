export type MarketplaceGuide = {
  type: string
  name: string
  read_adapter: boolean
  documentation_url: string
}

export type MarketplaceQuote = { amount: string; currency: string | null }

export type SheinAuthorizationView = {
  app_id: string
  endpoint: string
  scope: string
  supplier_id: string | null
  expires_at: number
  status: 'pending' | 'exchanging' | 'installing' | 'complete' | 'cancelled' | 'failed' | 'expired'
}

export type MarketplacePriceStock = {
  prices?: { currency: string; current: string | null; original: string | null }[]
  stock?: { total_available_stock: number | null; total_reserved_stock: number | null }
  reported_stock?: number | null
  supplier_price?: { amount: string; currency: string } | null
  listed_price?: MarketplaceQuote | null
  daily_discount_price?: MarketplaceQuote | null
  country_discount_prices?: { country: string; quote: MarketplaceQuote | null }[]
  in_stock?: boolean | null
}

export type MarketplaceProduct = MarketplacePriceStock & {
  id: string
  title: string
  status: string
  vendor?: string
  product_type?: string
  updated_at?: string
  category_id?: string
  sku?: string
  details_available?: boolean
  has_variants?: boolean
  variants_loaded?: boolean
  variants_offset?: number
  variants_complete?: boolean
  reported_sku_count?: number
  price_range?: { currency: string | null; min: string | null; max: string | null }
  has_country_pricing?: boolean
  variants?: (MarketplacePriceStock & { id: string; title: string; status: string; sku?: string; options?: { name: string; value: string }[]; id_kind?: 'platform' | 'properties'; property_signature?: string; attributes?: { property_id: string; value_id: string | null; value: string }[] })[]
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
  operation: 'authorized_shops' | 'products' | 'orders' | 'product_details'
  shop_id: string | null
  shops: { id: string; name: string; region: string; seller_type: string; domain?: string; currency?: string | null; region_id?: string; product_reads_available?: boolean; sku_reads_available?: boolean; identity_kind?: 'seller_account' }[]
  products: MarketplaceProduct[]
  orders?: MarketplaceOrder[]
  partial_data?: boolean
  source_requests?: { source_url: string; request_id: string; api_name?: string }[]
  api_name?: string
  authorization_expires_at?: string
  supplier_price_access?: 'disabled' | 'not_authorized' | 'allowed' | 'unsupported_identifier'
  granted_scopes?: string[]
  order_window?: 'last_60_days' | 'all'
  next_page_token: string | null
  pagination_unit?: 'SPU' | 'SKU'
  page_number?: number
  total_pages?: number
  product_status_filter?: string
  total_count?: number
  source_url: string
  retrieved_at: string
  request_id: string
  data_scope: 'authorized_seller_data'
}

export type MarketplaceReadInput = {
  operation: 'authorized_shops' | 'products' | 'orders' | 'product_details'
  shop_id?: string
  page_size?: number
  page_token?: string
  product_status?: string
  product_id?: string
}

// Configured permission a connector needs before a project can grant Agent shop reads.
export const PRODUCT_READ_SCOPE: Record<string, string> = {
  tiktok_shop: 'seller.product.basic',
  shopify: 'read_products',
  shein: 'shein:products:read',
  shopee: 'shopee:products:read',
  temu: 'temu:products:read',
  aliexpress: 'aliexpress:products:read',
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
