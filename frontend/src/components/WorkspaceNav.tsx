import { useT } from '../i18n'

type WorkspaceNavItemId = 'agents' | 'plugins' | 'schedules' | 'mobile'

type WorkspaceNavProps = {
  active?: WorkspaceNavItemId
}

function WorkspaceNavIcon({ name }: { name: WorkspaceNavItemId }) {
  if (name === 'plugins') {
    return (
      <svg aria-hidden="true" viewBox="0 0 24 24">
        <path d="M12 3.5 14.6 9l5.9 3-5.9 3L12 20.5 9.4 15l-5.9-3 5.9-3L12 3.5Z" />
      </svg>
    )
  }
  if (name === 'schedules') {
    return (
      <svg aria-hidden="true" viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="8.5" />
        <path d="M12 7.5V12l3 2" />
      </svg>
    )
  }
  if (name === 'mobile') {
    return (
      <svg aria-hidden="true" viewBox="0 0 24 24">
        <rect x="6.5" y="2.5" width="11" height="19" rx="2.5" />
        <path d="M10 5h4m-3 13.5h2" />
      </svg>
    )
  }
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24">
      <circle cx="12" cy="7" r="3" />
      <circle cx="6.5" cy="16.5" r="2.25" />
      <circle cx="17.5" cy="16.5" r="2.25" />
      <path d="m10.4 9.6-2.5 4.3m5.7-4.3 2.5 4.3" />
    </svg>
  )
}

export default function WorkspaceNav({ active }: WorkspaceNavProps) {
  const t = useT()
  const items = [
    { id: 'agents' as const, href: '/app/agents', label: 'Agents' },
    { id: 'plugins' as const, href: '/app/plugins', label: 'Plugins' },
    { id: 'schedules' as const, href: '/app/schedules', label: 'Scheduled tasks' },
    { id: 'mobile' as const, href: '/app/mobile', label: 'Mobile' },
  ]

  return (
    <nav className="conversation-sidebar-nav" aria-label={t('Workspace navigation')}>
      {items.map((item) => (
        <a key={item.id} href={item.href} aria-current={active === item.id ? 'page' : undefined}>
          <WorkspaceNavIcon name={item.id} />
          <span>{t(item.label)}</span>
        </a>
      ))}
    </nav>
  )
}
