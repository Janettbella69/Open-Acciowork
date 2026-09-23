export type MarketplaceGuide = {
  type: string
  name: string
  read_adapter: boolean
  documentation_url: string
}

export type MarketplaceQuote = { amount: string; currency: string | null }

export type SheinInventoryType = 'PI' | 'VI' | 'JI'
export const SHEIN_INVENTORY_LABELS: Record<SheinInventoryType, string> = {
  PI: 'SHEIN warehouse stock (PI)', VI: 'Merchant virtual stock (VI)', JI: 'Merchant JIT stock (JI)',
}
export type SheinInventoryQuantities = {
  total: number | null
  locked: number | null
  temporary_locked: number | null
  usable: number | null
  in_transit: number | null
  stockout_demand: number | null
}
export type SheinSkuInventory = {
  totals: SheinInventoryQuantities
  warehouses: (SheinInventoryQuantities & { code: string; warehouse_type: string })[]
}

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
  site_prices?: { site: string | null; currency: string | null; base_price: string | null; special_price: string | null }[]
  supply_prices?: { currency: string | null; amount: string | null }[]
  inventory?: SheinSkuInventory | null
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
  variants?: (MarketplacePriceStock & { id: string; title: string; status: string; sku?: string; skc_id?: string; skc_title?: string; purchase_status?: string; options?: { name: string; value: string }[]; id_kind?: 'platform' | 'properties'; property_signature?: string; attributes?: { property_id: string; value_id: string | null; value: string }[] })[]
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
  inventory_type?: SheinInventoryType | null
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
  inventory_type?: SheinInventoryType
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
