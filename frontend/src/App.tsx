import LandingPage from './components/LandingPage'
import ScheduledTasksPage from './components/ScheduledTasksPage'
import SettingsPage from './components/SettingsPage'
import SkillsPage from './components/SkillsPage'
import Workspace from './components/Workspace'

export default function App() {
  if (window.location.pathname.startsWith('/app/settings')) return <SettingsPage />
  if (window.location.pathname.startsWith('/app/schedules')) return <ScheduledTasksPage />
  if (window.location.pathname.startsWith('/app/skills')) return <SkillsPage />
  return window.location.pathname.startsWith('/app') ? <Workspace /> : <LandingPage />
}
