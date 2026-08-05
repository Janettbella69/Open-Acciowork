import LandingPage from './components/LandingPage'
import ScheduledTasksPage from './components/ScheduledTasksPage'
import SettingsPage from './components/SettingsPage'
import Workspace from './components/Workspace'

export default function App() {
  if (window.location.pathname.startsWith('/app/settings')) return <SettingsPage />
  if (window.location.pathname.startsWith('/app/schedules')) return <ScheduledTasksPage />
  return window.location.pathname.startsWith('/app') ? <Workspace /> : <LandingPage />
}
