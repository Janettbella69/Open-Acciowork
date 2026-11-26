import { useEffect } from 'react'
import LandingPage from './components/LandingPage'
import Workspace from './components/Workspace'
import I18nProvider from './I18nProvider'
import { pageTitle } from './pageMetadata'

export default function App() {
  const pathname = window.location.pathname

  useEffect(() => {
    document.title = pageTitle(pathname)
  }, [pathname])

  return <I18nProvider>{pathname.startsWith('/app') ? <Workspace /> : <LandingPage />}</I18nProvider>
}
