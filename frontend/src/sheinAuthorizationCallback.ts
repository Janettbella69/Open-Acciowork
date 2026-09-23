export const SHEIN_CALLBACK_PREFIX = '/app/connectors/shein-authorize/'
export type SheinCallback = { connectorId: string; appId: string; state: string; tempToken: string }

export function captureSheinCallback(location: Pick<Location, 'pathname' | 'search' | 'hash'>, history: Pick<History, 'replaceState'>): SheinCallback | null {
  if (!location.pathname.startsWith(SHEIN_CALLBACK_PREFIX)) return null
  const connectorId = location.pathname.slice(SHEIN_CALLBACK_PREFIX.length)
  const search = location.search
  const hash = location.hash
  // Remove callback credentials before React starts API calls or renders links.
  history.replaceState(null, '', location.pathname)
  if (search.length > 5000 || hash || !/^[A-Za-z0-9_-]{1,200}$/.test(connectorId)) return null
  const params = new URLSearchParams(search)
  if (['appid', 'state', 'tempToken'].some((key) => params.getAll(key).length !== 1)) return null
  const appId = params.get('appid')!
  const state = params.get('state')!
  const tempToken = params.get('tempToken')!
  if (!/^[A-Za-z0-9_-]{1,160}$/.test(appId) || !/^[a-f0-9]{32}\.[a-f0-9]{64}$/.test(state) || !/^[A-Za-z0-9_.~:+/=-]{1,4096}$/.test(tempToken)) return null
  return { connectorId, appId, state, tempToken }
}
