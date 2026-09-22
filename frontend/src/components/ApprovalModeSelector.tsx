import { useEffect, useRef, useState } from 'react'
import { errorMessage } from '../api/client'
import type { PermissionModePolicy } from '../api/types'
import { useT } from '../i18n'

type ApprovalModeSelectorProps = {
  mode: PermissionModePolicy | null
  loadError?: string
  disabled?: boolean
  onChange: (mode: PermissionModePolicy) => Promise<void>
}

function ApprovalModeIcon({ mode }: { mode: PermissionModePolicy }) {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      {mode === 'default' ? (
        <>
          <path d="M8 12.5V6a1.5 1.5 0 0 1 3 0v5" />
          <path d="M11 10.5V4.5a1.5 1.5 0 0 1 3 0v6" />
          <path d="M14 10.5V6.5a1.5 1.5 0 0 1 3 0V13c0 4.1-2.7 7-6.3 7-2.3 0-3.8-1-5.1-2.9l-2.3-3.3a1.5 1.5 0 0 1 2.4-1.8L8 14.5" />
        </>
      ) : (
        <>
          <path d="M12 3.5 19 6v5.6c0 4.3-2.9 7.8-7 8.9-4.1-1.1-7-4.6-7-8.9V6l7-2.5Z" />
          <path d="m9 12 2 2 4-4" />
        </>
      )}
    </svg>
  )
}

export default function ApprovalModeSelector({
  mode,
  loadError = '',
  disabled = false,
  onChange,
}: ApprovalModeSelectorProps) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')
  const rootRef = useRef<HTMLDivElement>(null)
  const menuOpen = open && !disabled
  // Only the SDK-native modes allowed by the sandbox red lines; there is no unsandboxed full-access level.
  const options = [
    { mode: 'default' as const, title: t('Ask for approval'), detail: t('Ask before consequential actions') },
    { mode: 'auto' as const, title: t('Approve for me'), detail: t('SDK auto mode reviews each action; deleting files still asks') },
  ]
  const current = options.find((option) => option.mode === mode)
  const error = saveError || loadError

  useEffect(() => {
    if (!menuOpen) return

    const closeOnOutsidePress = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }

    document.addEventListener('pointerdown', closeOnOutsidePress)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOnOutsidePress)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [menuOpen])

  const choose = async (nextMode: PermissionModePolicy) => {
    if (nextMode === mode) {
      setOpen(false)
      return
    }
    setSaving(true)
    setSaveError('')
    try {
      await onChange(nextMode)
      setOpen(false)
    } catch (changeError) {
      setSaveError(errorMessage(changeError))
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="conversation-approval-selector" ref={rootRef}>
      <button
        className="conversation-approval-trigger"
        type="button"
        aria-label={t('Open approval mode selector')}
        title={disabled
          ? t('Approval level cannot change while a task is running or waiting for approval.')
          : current?.title ?? t('Approval')}
        aria-haspopup="dialog"
        aria-expanded={menuOpen}
        disabled={disabled}
        onClick={() => setOpen((value) => !value)}
      >
        <ApprovalModeIcon mode={mode ?? 'default'} />
        <span>{current?.title ?? t('Approval')}</span>
        <i aria-hidden="true">{menuOpen ? '⌃' : '⌄'}</i>
      </button>

      {menuOpen && (
        <section className="conversation-approval-menu" role="dialog" aria-label={t('Choose an approval mode')}>
          <header>
            <strong>{t('How should TradeMind actions be approved?')}</strong>
            <a href="/app/settings">{t('Settings')}</a>
          </header>
          <div>
            {options.map((option) => (
              <button
                key={option.mode}
                type="button"
                data-active={mode === option.mode || undefined}
                disabled={saving || mode === null}
                onClick={() => void choose(option.mode)}
              >
                <ApprovalModeIcon mode={option.mode} />
                <span><strong>{option.title}</strong><small>{option.detail}</small></span>
                <i aria-hidden="true">{mode === option.mode ? '✓' : ''}</i>
              </button>
            ))}
          </div>
          {error
            ? <p role="alert">{error}</p>
            : <footer>{mode === null ? t('Loading approval mode…') : t('Applies to new tasks across this workspace.')}</footer>}
        </section>
      )}
    </div>
  )
}
