import type { ProviderProfile } from './api/types'

export function providerIsRunnable(provider: ProviderProfile): boolean {
  return provider.enabled
    && provider.secret_configured
    && provider.production_eligible
    && Boolean(provider.last_verified_at)
    && !provider.last_error_code
}

export function providerUnavailableReason(provider: ProviderProfile): string {
  if (!provider.enabled) return 'Disabled'
  if (!provider.secret_configured) return 'Needs configuration'
  if (provider.last_error_code) return 'Preflight failed'
  if (!provider.last_verified_at) return 'Preflight required'
  if (!provider.production_eligible) return 'Not production-cleared'
  return ''
}
