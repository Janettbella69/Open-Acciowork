import LandingPage from './components/LandingPage'
import SettingsPage from './components/SettingsPage'
import Workspace from './components/Workspace'

export default function App() {
  if (window.location.pathname.startsWith('/app/settings')) return <SettingsPage />
  return window.location.pathname.startsWith('/app') ? <Workspace /> : <LandingPage />
}
