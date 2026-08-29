import { useEffect } from 'react'
import LandingPage from './components/LandingPage'
import ScheduledTasksPage from './components/ScheduledTasksPage'
import SettingsPage from './components/SettingsPage'
import SkillsPage from './components/SkillsPage'
import Workspace from './components/Workspace'
import I18nProvider from './I18nProvider'
import { pageTitle } from './pageMetadata'

function route(pathname: string) {
  if (pathname.startsWith('/app/settings')) return <SettingsPage />
  if (pathname.startsWith('/app/schedules')) return <ScheduledTasksPage />
  if (pathname.startsWith('/app/skills')) return <SkillsPage />
  return pathname.startsWith('/app') ? <Workspace /> : <LandingPage />
}

export default function App() {
  const pathname = window.location.pathname

  useEffect(() => {
    document.title = pageTitle(pathname)
  }, [pathname])

  return <I18nProvider>{route(pathname)}</I18nProvider>
}
