import { useState } from 'react'
import ReactMarkdown from 'react-markdown'
import type { Decision } from '../api'
import { useT } from '../i18n'
import { describeStep, type TurnView } from '../thread'
import RequestCard from './RequestCard'

type Props = {
  prompt: string
  view: TurnView
  devMode: boolean
  onDecide: (requestId: string, decision: Decision) => Promise<void>
  onFeedback: (text: string) => Promise<void>
}

export default function Turn({ prompt, view, devMode, onDecide, onFeedback }: Props) {
  const t = useT()
  return (
    <article className="turn">
      <p className="turn-prompt">{prompt}</p>
      <div className="turn-reply">
        {view.items.map((item, index) => {
          if (item.kind === 'text') {
            return (
              <div className="turn-text" key={index}>
                <ReactMarkdown>{item.text}</ReactMarkdown>
              </div>
            )
          }
          if (item.kind === 'thinking') {
            return (
              <details className="turn-thinking" key={index}>
                <summary>{t('Thinking summary')}</summary>
                <p>{item.text}</p>
              </details>
            )
          }
          if (item.kind === 'steps') {
            return (
              <details className="turn-steps" key={index}>
                <summary>{t('Steps ({count})', { count: item.steps.length })}</summary>
                <ol>
                  {item.steps.map((step) => (
                    <li key={step.id} data-error={step.isError ? 'true' : undefined}>
                      <span className="step-tool">{step.tool}</span> {describeStep(step)}
                      {step.isError && <em> · {t('failed')}</em>}
                      {devMode && step.result && <pre>{step.result}</pre>}
                    </li>
                  ))}
                </ol>
              </details>
            )
          }
          return (
            <RequestCard
              key={item.request.requestId}
              request={item.request}
              onDecide={(decision) => onDecide(item.request.requestId, decision)}
            />
          )
        })}
        {view.status === 'running' && <p className="turn-status" role="status">{t('Working…')}</p>}
        {view.status === 'cancelled' && <p className="turn-status">{t('Stopped')}</p>}
        {view.status === 'failed' && (
          <p className="turn-status turn-error" role="alert">
            {t('This turn did not finish: {error}', { error: view.error ?? '' })}
          </p>
        )}
        {view.status !== 'running' && <FeedbackLink onFeedback={onFeedback} />}
        {devMode && view.costUsd != null && <p className="turn-cost">${view.costUsd.toFixed(4)}</p>}
      </div>
    </article>
  )
}

function FeedbackLink({ onFeedback }: { onFeedback: Props['onFeedback'] }) {
  const t = useT()
  const [open, setOpen] = useState(false)
  const [text, setText] = useState('')
  const [sent, setSent] = useState(false)
  if (sent) return <p className="turn-feedback-sent">{t('Thanks, noted.')}</p>
  if (!open) {
    return (
      <button type="button" className="turn-feedback-link" onClick={() => setOpen(true)}>
        {t('Something wrong?')}
      </button>
    )
  }
  return (
    <form
      className="turn-feedback"
      onSubmit={(event) => {
        event.preventDefault()
        if (!text.trim()) return
        void onFeedback(text).then(() => setSent(true))
      }}
    >
      <textarea
        aria-label={t('What is wrong?')}
        placeholder={t('What is wrong? A wrong number, an unreliable source…')}
        value={text}
        onChange={(event) => setText(event.target.value)}
      />
      <button type="submit">{t('Send feedback')}</button>
    </form>
  )
}
