const productCategory = 'AI Global Commerce Workspace'

export function pageTitle(pathname: string) {
  if (pathname.startsWith('/app/settings')) return `TradeMind Settings - ${productCategory}`
  if (pathname.startsWith('/app/tasks')) return `TradeMind Tasks - ${productCategory}`
  if (pathname.startsWith('/app/schedules')) return `TradeMind Scheduled - ${productCategory}`
  if (pathname.startsWith('/app/agents')) return `TradeMind Agents - ${productCategory}`
  if (pathname.startsWith('/app/plugins/skills') || pathname.startsWith('/app/skills')) return `TradeMind Skills - ${productCategory}`
  if (pathname.startsWith('/app/plugins/connectors') || pathname.startsWith('/app/connectors')) return `TradeMind Connectors - ${productCategory}`
  if (pathname.startsWith('/app/plugins')) return `TradeMind Plugins - ${productCategory}`
  if (pathname.startsWith('/app/inbox')) return `TradeMind Inbox - ${productCategory}`
  if (pathname.startsWith('/app/mobile')) return `TradeMind Mobile - ${productCategory}`
  return `TradeMind - ${productCategory}`
}
