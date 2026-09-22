import type { ProviderProfile } from '../api/types'
import { useT, type Translate } from '../i18n'
import './provider-data-notice.css'

type Location = Pick<ProviderProfile, 'base_url' | 'region'>

function endpointLabel(profile: Location, t: Translate) {
  if (!profile.base_url) return t('Anthropic default endpoint')
  try {
    const url = new URL(profile.base_url)
    if (url.protocol === 'https:' || url.protocol === 'http:') return url.origin
  } catch { /* An invalid configuration has no verified destination. */ }
  return t('Endpoint not recognized')
}

export function ProviderDataSummary({ provider }: { provider: Location }) {
  const t = useT()
  const region = provider.region.trim()
  const unspecified = ['', 'unspecified', 'unknown', 'configured environment', 'environment'].includes(region.toLowerCase())
  return <span className="provider-data-summary">
    <span>{t('Endpoint: {endpoint}', { endpoint: endpointLabel(provider, t) })}</span>
    <span>{t('Reported region: {region}', { region: unspecified ? t('Not specified') : region })}</span>
  </span>
}

export default function ProviderDataNotice({ provider, automatic = false }: { provider?: Location | null; automatic?: boolean }) {
  const t = useT()
  return <aside className="provider-data-notice" aria-label={t('Model data and region')}>
    {provider ? <ProviderDataSummary provider={provider} /> : <p>{t(automatic
      ? 'Auto uses server-side Provider selection. Choose a specific model to review its endpoint and reported region before sending.'
      : 'The selected Provider details are unavailable. Refresh or choose another model to review its destination.')}</p>}
    <p>{t('Model requests may cross borders. Check the Provider’s processing locations and data terms before sending sensitive material.')}</p>
    <details>
      <summary>{t('What data is shared?')}</summary>
      <p>{t('Model requests include your messages and the context used by the Agent, which can include approved memories, file content and tool results.')}</p>
      <p>{t('The reported region is entered by your administrator. TradeMind does not verify processing or storage locations from an endpoint address; a proxy can forward requests elsewhere.')}</p>
      <p>{t('Search, browsing and connected tools can also send task data to their own services. Selecting a model does not set a region restriction for those services.')}</p>
    </details>
  </aside>
}
