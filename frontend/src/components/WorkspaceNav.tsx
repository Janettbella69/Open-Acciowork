import { useT } from '../i18n'

type WorkspaceNavProps = {
  active: 'workspace' | 'skills' | 'schedules'
}

function WorkspaceNavIcon({ name }: { name: WorkspaceNavProps['active'] }) {
  if (name === 'skills') {
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
  return (
    <svg aria-hidden="true" viewBox="0 0 24 24">
      <rect x="4" y="4" width="16" height="16" rx="4" />
      <path d="M8 12h8M12 8v8" />
    </svg>
  )
}

export default function WorkspaceNav({ active }: WorkspaceNavProps) {
  const t = useT()
  const items = [
    { id: 'workspace' as const, href: '/app', label: 'Agent workspace' },
    { id: 'skills' as const, href: '/app/skills', label: 'Skill Studio' },
    { id: 'schedules' as const, href: '/app/schedules', label: 'Scheduled tasks' },
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
