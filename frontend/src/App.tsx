import LandingPage from './components/LandingPage'
import Workspace from './components/Workspace'

export default function App() {
  return window.location.pathname.startsWith('/app') ? <Workspace /> : <LandingPage />
}
