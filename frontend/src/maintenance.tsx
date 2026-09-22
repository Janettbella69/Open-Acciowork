import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { consumeMaintenanceKey } from './api/maintenanceConsole'
import { MaintenanceApp } from './components/MaintenancePage'
import './styles.css'

// Consume once before StrictMode renders. The capability is never persisted.
const token = consumeMaintenanceKey()
createRoot(document.getElementById('root')!).render(<StrictMode><MaintenanceApp initialToken={token} /></StrictMode>)
