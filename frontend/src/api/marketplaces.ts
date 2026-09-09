export type MarketplaceGuide = {
  type: string
  name: string
  read_adapter: boolean
  documentation_url: string
}

export type MarketplaceRead = {
  connector_id: string
  operation: 'authorized_shops' | 'products'
  shop_id: string | null
  shops: { id: string; name: string; region: string; seller_type: string }[]
  products: { id: string; title: string; status: string }[]
  next_page_token: string | null
  source_url: string
  retrieved_at: string
  request_id: string
  data_scope: 'authorized_seller_data'
}

export type MarketplaceReadInput = {
  operation: 'authorized_shops' | 'products'
  shop_id?: string
  page_size?: number
  page_token?: string
  product_status?: string
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
