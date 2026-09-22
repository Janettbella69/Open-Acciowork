import { useT } from '../i18n'

export default function ApplicationRecoveryHelp() {
  const t = useT()
  return <section id="application-recovery" className="settings-section" aria-labelledby="application-recovery-heading">
    <div className="settings-section-heading"><span>11</span><div><h2 id="application-recovery-heading">{t('Full application backup and recovery')}</h2><p>{t('Open the separate local maintenance interface after stopping this installation.')}</p></div></div>
    <div className="settings-card"><div className="settings-row"><div>
      <p>{t('The local operator first finishes or cancels active conversations, closes shop browsers, and stops every application worker. The maintenance interface verifies and restores complete backups without starting saved automation.')}</p>
      <p>{t('Build the frontend once with npm run build. On the backend computer, launch maintenance with this installation’s data and Plugin paths and a separate private workspace:')}</p>
      <pre className="settings-maintenance-command"><code>{'python -m ophira_backend.maintenance_console \\\n  --data-dir /absolute/app-data \\\n  --plugin-dir /absolute/ophira-capabilities \\\n  --workspace /absolute/private-maintenance'}</code></pre>
      <p>{t('Open the private loopback link printed by the command. In that interface, prepare a backup or select an existing cold backup, inspect its counts and destination, then confirm. Keep the link private.')}</p>
      <p>{t('Recovery creates a new directory. The local operator reviews restored schedules and connections before switching the deployment to it; existing data is preserved.')}</p>
    </div></div></div>
  </section>
}
