import { useState } from 'react'
import type { AskUserQuestionItem, PermissionAnswers } from '../api/types'
import { useT } from '../i18n'

const sensitiveKey = /(authorization|cookie|credential|password|secret|token|api[_-]?key)/i
const scopeKeys = ['file_path', 'path', 'url', 'command'] as const

function redactValue(value: unknown, key = ''): unknown {
  if (sensitiveKey.test(key)) return '[REDACTED]'
  if (Array.isArray(value)) return value.map((item) => redactValue(item))
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([entryKey, entryValue]) => [
        entryKey,
        redactValue(entryValue, entryKey),
      ]),
    )
  }
  return value
}

function redactedToolInput(toolInput: unknown): string {
  try {
    return JSON.stringify(redactValue(toolInput), null, 2)
  } catch {
    return '{}'
  }
}

function permissionScope(toolName: string, toolInput: unknown): string {
  if (toolInput && typeof toolInput === 'object') {
    const input = toolInput as Record<string, unknown>
    for (const key of scopeKeys) {
      const value = input[key]
      if (typeof value === 'string' && value.trim()) return `${key}: ${value.trim().slice(0, 500)}`
    }
  }
  return `${toolName} · current project`
}

function parsePermissionQuestions(toolInput: unknown): AskUserQuestionItem[] {
  if (!toolInput || typeof toolInput !== 'object') return []
  const questions = (toolInput as { questions?: unknown }).questions
  if (!Array.isArray(questions) || !questions.length) return []
  const valid = questions.every(
    (item): item is AskUserQuestionItem =>
      !!item && typeof item === 'object'
      && typeof (item as AskUserQuestionItem).question === 'string'
      && (item as AskUserQuestionItem).question.trim().length > 0
      && Array.isArray((item as AskUserQuestionItem).options)
      && (item as AskUserQuestionItem).options.length > 0
      && (item as AskUserQuestionItem).options.every(
        (option) => !!option && typeof option === 'object' && typeof option.label === 'string' && option.label.trim().length > 0,
      ),
  )
  if (!valid) return []
  const parsed = questions as AskUserQuestionItem[]
  return new Set(parsed.map((question) => question.question)).size === parsed.length ? parsed : []
}

export function PermissionDetails({
  toolName,
  toolInput,
  scope,
  destructive,
  persistable,
  timeoutSeconds,
}: {
  toolName: string
  toolInput: unknown
  scope?: string
  destructive: boolean
  persistable: boolean
  timeoutSeconds?: number
}) {
  const t = useT()
  const displayScope = scope === undefined
    ? permissionScope(toolName, redactValue(toolInput))
    : scope.trim() || t('No reusable scope')
  return (
    <div className="permission-request-details">
      <dl>
        <div><dt>{t('Scope')}</dt><dd>{displayScope}</dd></div>
        <div>
          <dt>{t('Risk')}</dt>
          <dd>{destructive ? t('Destructive · one-time approval only') : t('Non-destructive')}</dd>
        </div>
        <div>
          <dt>{t('Approval effect')}</dt>
          <dd>{persistable && !destructive ? t('Once · current run · 30 days') : t('Once only')}</dd>
        </div>
        {timeoutSeconds !== undefined && (
          <div><dt>{t('Approval window')}</dt><dd>{t('{minutes} minutes', { minutes: Math.max(1, Math.ceil(timeoutSeconds / 60)) })}</dd></div>
        )}
        {persistable && !destructive && (
          <div><dt>{t('Persistent access')}</dt><dd>{t('Scoped to the target above · expires after 30 days')}</dd></div>
        )}
      </dl>
      <details open>
        <summary>{t('Review tool input')}</summary>
        <pre>{redactedToolInput(toolInput)}</pre>
      </details>
    </div>
  )
}

export function PermissionQuestionCard({
  toolInput,
  scope,
  timeoutSeconds,
  busy = false,
  onSubmit,
  onDeny,
}: {
  toolInput: unknown
  scope?: string
  timeoutSeconds?: number
  busy?: boolean
  onSubmit: (answers: PermissionAnswers) => void
  onDeny: () => void
}) {
  const t = useT()
  const questions = parsePermissionQuestions(toolInput)
  const [selected, setSelected] = useState<Record<string, string[]>>({})

  const toggle = (question: AskUserQuestionItem, label: string) => {
    setSelected((current) => {
      const existing = current[question.question] ?? []
      if (question.multiSelect) {
        const next = existing.includes(label)
          ? existing.filter((item) => item !== label)
          : [...existing, label]
        return { ...current, [question.question]: next }
      }
      return { ...current, [question.question]: [label] }
    })
  }

  const complete = questions.length > 0
    && questions.every((question) => (selected[question.question] ?? []).length > 0)

  const submit = () => {
    if (!complete) return
    const answers: PermissionAnswers = {}
    for (const question of questions) {
      const labels = selected[question.question] ?? []
      answers[question.question] = question.multiSelect ? labels : (labels[0] ?? '')
    }
    onSubmit(answers)
  }

  return (
    <section className="conversation-permission conversation-question" aria-label={t('Agent question')}>
      <div><span>{t('TradeMind needs your input')}</span></div>
      {questions.length ? questions.map((question) => (
        <fieldset key={question.question}>
          <legend>
            {question.header && <small>{question.header}</small>}
            <strong>{question.question}</strong>
          </legend>
          <div className="conversation-question-options" role={question.multiSelect ? 'group' : 'radiogroup'}>
            {question.options.map((option) => {
              const active = (selected[question.question] ?? []).includes(option.label)
              return (
                <button
                  type="button"
                  key={option.label}
                  data-active={active || undefined}
                  aria-pressed={active}
                  disabled={busy}
                  onClick={() => toggle(question, option.label)}
                >
                  <strong>{option.label}</strong>
                  {option.description && <small>{option.description}</small>}
                </button>
              )
            })}
          </div>
        </fieldset>
      )) : (
        <p role="alert">{t('This question could not be decoded. Deny it and ask the Agent to try again.')}</p>
      )}
      <PermissionDetails
        toolName="AskUserQuestion"
        toolInput={toolInput}
        scope={scope}
        destructive={false}
        persistable={false}
        timeoutSeconds={timeoutSeconds}
      />
      <div>
        <button type="button" disabled={!complete || busy} onClick={submit}>{t('Submit answers')}</button>
        <button type="button" disabled={busy} onClick={onDeny}>{t('Skip — continue with what is known')}</button>
      </div>
    </section>
  )
}
