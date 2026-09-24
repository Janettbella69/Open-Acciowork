import { useEffect } from 'react'
import AgentsPage from './components/AgentsPage'
import InboxPage from './components/InboxPage'
import LandingPage from './components/LandingPage'
import MobilePage from './components/MobilePage'
import PluginsPage from './components/PluginsPage'
import ScheduledTasksPage from './components/ScheduledTasksPage'
import SettingsPage from './components/SettingsPage'
import TaskOverviewPage from './components/TaskOverviewPage'
import Workspace from './components/Workspace'
import I18nProvider from './I18nProvider'
import { pageTitle } from './pageMetadata'

function route(pathname: string) {
  if (pathname.startsWith('/app/settings')) return <SettingsPage />
  if (pathname.startsWith('/app/tasks')) return <TaskOverviewPage />
  if (pathname.startsWith('/app/schedules')) return <ScheduledTasksPage />
  if (pathname.startsWith('/app/agents')) return <AgentsPage />
  if (pathname.startsWith('/app/inbox')) return <InboxPage />
  if (pathname.startsWith('/app/plugins')) return <PluginsPage />
  if (pathname.startsWith('/app/skills')) return <PluginsPage legacyTab="skills" />
  if (pathname.startsWith('/app/connectors')) return <PluginsPage legacyTab="connectors" />
  if (pathname.startsWith('/app/mobile')) return <MobilePage />
  return pathname.startsWith('/app') ? <Workspace /> : <LandingPage />
}

export default function App() {
  const pathname = window.location.pathname

  useEffect(() => {
    document.title = pageTitle(pathname)
  }, [pathname])

  return <I18nProvider>{route(pathname)}</I18nProvider>
}
