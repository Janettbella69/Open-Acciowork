export type SitePublication = {
  id: string
  project_id: string
  artifact_id: string
  artifact_name: string
  artifact_checksum: string
  size_bytes: number
  bundle: boolean
  allow_scripts: boolean
  origin: string
  state: 'draft' | 'published' | 'revoked'
  revision: number
  created_by: string
  created_at: string
  published_by: string | null
  published_at: string | null
  revoked_by: string | null
  revoked_at: string | null
}

export type SitePublicationView = {
  publication: SitePublication
  public_url: string
}

export type SitePublicationCatalog = {
  public_origin: string | null
  can_stage: boolean
  can_publish: boolean
  publications: SitePublicationView[]
}
