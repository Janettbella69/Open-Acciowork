import { useState } from 'react'
import type { Decision } from '../api'
import { useT } from '../i18n'
import type { Question, Request } from '../thread'

type Props = { request: Request; onDecide: (decision: Decision) => Promise<void> }

function actionText(request: Request): string {
  const input = request.input
  if (request.tool === 'Bash') return String(input.command ?? '')
  if (typeof input.file_path === 'string') return input.file_path
  return JSON.stringify(input, null, 2)
}

export default function RequestCard({ request, onDecide }: Props) {
  const t = useT()
  if (request.tool === 'AskUserQuestion') {
    return <QuestionCard request={request} onDecide={onDecide} />
  }
  if (request.resolved) {
    return (
      <p className="request-resolved" data-allow={request.resolved.allow}>
        {request.resolved.allow ? t('Allowed') : t('Denied')}: <code>{actionText(request)}</code>
      </p>
    )
  }
  const description = typeof request.input.description === 'string' ? request.input.description : ''
  return (
    <section className="request-card" aria-label={t('Needs your OK')}>
      <strong>{t('Needs your OK')}</strong>
      {description && <p>{description}</p>}
      <pre>{actionText(request)}</pre>
      <div className="request-actions">
        <button type="button" className="primary" onClick={() => void onDecide({ allow: true })}>
          {t('Allow')}
        </button>
        <button type="button" onClick={() => void onDecide({ allow: false })}>
          {t('Deny')}
        </button>
      </div>
    </section>
  )
}

function QuestionCard({ request, onDecide }: Props) {
  const t = useT()
  const questions = (request.input.questions as Question[] | undefined) ?? []
  const [picked, setPicked] = useState<Record<string, string[]>>({})
  const [other, setOther] = useState<Record<string, string>>({})

  if (request.resolved) {
    const answers = request.resolved.answers ?? {}
    return (
      <div className="request-resolved" data-allow="true">
        {questions.map((q) => (
          <p key={q.question}>
            {q.question} <strong>{String(answers[q.question] ?? '—')}</strong>
          </p>
        ))}
      </div>
    )
  }

  const toggle = (q: Question, label: string) =>
    setPicked((current) => {
      const chosen = current[q.question] ?? []
      if (!q.multiSelect) return { ...current, [q.question]: [label] }
      return {
        ...current,
        [q.question]: chosen.includes(label) ? chosen.filter((l) => l !== label) : [...chosen, label],
      }
    })

  const answers = Object.fromEntries(
    questions.map((q) => {
      const typed = other[q.question]?.trim()
      const chosen = picked[q.question] ?? []
      const all = typed ? [...chosen, typed] : chosen
      return [q.question, q.multiSelect ? all.join(', ') : (typed || chosen[0] || '')]
    }),
  )
  const complete = questions.every((q) => answers[q.question])

  return (
    <section className="request-card question-card" aria-label={t('Question')}>
      {questions.map((q) => (
        <fieldset key={q.question}>
          <legend>{q.question}</legend>
          <div className="question-options">
            {q.options.map((option) => (
              <button
                type="button"
                key={option.label}
                aria-pressed={(picked[q.question] ?? []).includes(option.label)}
                onClick={() => toggle(q, option.label)}
              >
                <span>{option.label}</span>
                {option.description && <small>{option.description}</small>}
              </button>
            ))}
          </div>
          <input
            aria-label={t('Other')}
            placeholder={t('Other: type your own answer')}
            value={other[q.question] ?? ''}
            onChange={(event) => setOther({ ...other, [q.question]: event.target.value })}
          />
        </fieldset>
      ))}
      <div className="request-actions">
        <button
          type="button"
          className="primary"
          disabled={!complete}
          onClick={() => void onDecide({ allow: true, answers })}
        >
          {t('Submit answer')}
        </button>
      </div>
    </section>
  )
}
