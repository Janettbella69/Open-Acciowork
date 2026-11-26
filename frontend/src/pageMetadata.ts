const productCategory = 'AI Global Commerce Workspace'

export function pageTitle(pathname: string) {
  if (pathname.startsWith('/app')) return `TradeMind Workspace - ${productCategory}`
  return `TradeMind - ${productCategory}`
}
