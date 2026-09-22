export interface CredentialView {
  kind: 'provider' | 'connector' | 'service'
  id: string
  display_name: string
  revision: string
  configured: boolean
  source: 'stored' | 'environment' | 'missing' | 'deleted'
  deleted_at: string | null
  deleted_by: string | null
  effect?: string
}

export interface CredentialArchive {
  format: 'ophira-encrypted-credential'
  version: 1
  kdf: string
  cipher: string
  salt: string
  nonce: string
  ciphertext: string
}
