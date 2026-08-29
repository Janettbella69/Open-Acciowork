const productCategory = 'AI Global Commerce Workspace'

export function pageTitle(pathname: string) {
  if (pathname.startsWith('/app/settings')) return `TradeMind Settings - ${productCategory}`
  if (pathname.startsWith('/app/schedules')) return `TradeMind Scheduled - ${productCategory}`
  if (pathname.startsWith('/app/skills')) return `TradeMind Skills - ${productCategory}`
  return `TradeMind - ${productCategory}`
}
