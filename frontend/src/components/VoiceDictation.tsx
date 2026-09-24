import { useEffect, useRef, useState } from 'react'
import { useLanguage, useT } from '../i18n'
import './voice-dictation.css'

type SpeechResult = { isFinal: boolean; 0?: { transcript: string } }
type SpeechResultEvent = { results: ArrayLike<SpeechResult> }
type SpeechErrorEvent = { error: string }
type Recognition = {
  lang: string
  continuous: boolean
  interimResults: boolean
  onresult: ((event: SpeechResultEvent) => void) | null
  onerror: ((event: SpeechErrorEvent) => void) | null
  onend: (() => void) | null
  start: () => void
  stop: () => void
  abort: () => void
}
type RecognitionConstructor = new () => Recognition

function recognitionConstructor(): RecognitionConstructor | null {
  const browser = window as Window & {
    SpeechRecognition?: RecognitionConstructor
    webkitSpeechRecognition?: RecognitionConstructor
  }
  return browser.SpeechRecognition ?? browser.webkitSpeechRecognition ?? null
}

function errorText(error: string): string {
  if (error === 'not-allowed' || error === 'service-not-allowed') {
    return 'Microphone or speech recognition permission was denied.'
  }
  if (error === 'no-speech') return 'No speech was detected. Try again.'
  if (error === 'network') return 'The browser speech service is unavailable. Try again.'
  return 'Voice transcription failed. Try again or type your message.'
}

export default function VoiceDictation({ value, onChange, disabled = false, inline = false }: {
  value: string
  onChange: (next: string) => void
  disabled?: boolean
  inline?: boolean
}) {
  const t = useT()
  const language = useLanguage()
  const [spokenLanguage, setSpokenLanguage] = useState<'en-US' | 'zh-CN'>(
    language === 'zh-CN' ? 'zh-CN' : 'en-US',
  )
  const [phase, setPhase] = useState<'idle' | 'listening' | 'review'>('idle')
  const [draft, setDraft] = useState('')
  const [error, setError] = useState('')
  const recognition = useRef<Recognition | null>(null)
  const heard = useRef('')
  const available = recognitionConstructor() !== null && window.isSecureContext !== false

  const cancel = () => {
    const current = recognition.current
    recognition.current = null
    current?.abort()
    heard.current = ''
    setDraft('')
    setError('')
    setPhase('idle')
  }

  useEffect(() => {
    if (!disabled) return
    const current = recognition.current
    recognition.current = null
    current?.abort()
    heard.current = ''
    setDraft('')
    setError('')
    setPhase('idle')
  }, [disabled])

  useEffect(() => {
    const stopWhenHidden = () => {
      if (document.hidden) {
        const current = recognition.current
        recognition.current = null
        current?.abort()
        heard.current = ''
        setDraft('')
        setError('')
        setPhase('idle')
      }
    }
    document.addEventListener('visibilitychange', stopWhenHidden)
    return () => {
      document.removeEventListener('visibilitychange', stopWhenHidden)
      const current = recognition.current
      recognition.current = null
      current?.abort()
    }
  }, [])

  const start = () => {
    if (disabled || phase === 'listening') return
    const Constructor = recognitionConstructor()
    if (!Constructor || window.isSecureContext === false) return
    const current = new Constructor()
    current.lang = spokenLanguage
    current.continuous = true
    current.interimResults = true
    heard.current = ''
    setDraft('')
    setError('')
    current.onresult = (event) => {
      if (recognition.current !== current) return
      const parts: string[] = []
      for (let index = 0; index < event.results.length; index += 1) {
        const text = event.results[index]?.[0]?.transcript.trim()
        if (text) parts.push(text)
      }
      const next = parts.join(spokenLanguage === 'zh-CN' ? '' : ' ').slice(0, 40_000)
      heard.current = next
      setDraft(next)
    }
    current.onerror = (event) => {
      if (recognition.current !== current) return
      recognition.current = null
      current.abort()
      setError(t(errorText(event.error)))
      setPhase('review')
    }
    current.onend = () => {
      if (recognition.current !== current) return
      recognition.current = null
      if (!heard.current) setError((existing) => existing || t('No speech was detected. Try again.'))
      setPhase('review')
    }
    recognition.current = current
    setPhase('listening')
    try {
      current.start()
    } catch {
      recognition.current = null
      setError(t('Voice transcription failed. Try again or type your message.'))
      setPhase('review')
    }
  }

  const useDraft = () => {
    const text = draft.trim()
    if (!text || disabled) return
    const next = value.trimEnd() ? value.trimEnd() + '\n' + text : text
    if (next.length > 40_000) {
      setError(t('The reviewed text would exceed the message limit. Shorten it first.'))
      return
    }
    onChange(next)
    cancel()
  }

  return <div className="voice-dictation" data-inline={inline}>
    <button className="voice-dictation-trigger" type="button"
      aria-label={t(phase === 'listening' ? 'Stop dictation' : 'Dictate message')}
      title={t(phase === 'listening' ? 'Stop dictation'
        : 'Dictate message. Your browser may process the audio with its speech service.')}
      aria-pressed={phase === 'listening'}
      disabled={disabled || !available}
      onClick={phase === 'listening' ? () => recognition.current?.stop() : start}>
      <svg viewBox="0 0 24 24" aria-hidden="true"><rect x="9" y="3" width="6" height="12" rx="3" />
        <path d="M5 10a7 7 0 0 0 14 0M12 17v4m-4 0h8" /></svg>
      {inline && <span>{t(phase === 'listening' ? 'Stop dictation' : 'Dictate message')}</span>}
    </button>
    {!available && <small>{t('Voice input requires a supporting browser and a secure connection. You can use keyboard dictation instead.')}</small>}
    {available && inline && phase === 'idle' && <>
      <label className="voice-dictation-language">{t('Speech language')}
        <select value={spokenLanguage} disabled={disabled}
          onChange={(event) => setSpokenLanguage(event.target.value as 'en-US' | 'zh-CN')}>
          <option value="en-US">English</option><option value="zh-CN">中文</option>
        </select>
      </label>
      <small>{t('Your browser may process audio with its speech service. Nothing is sent to the Agent until you submit the message.')}</small>
    </>}
    {phase !== 'idle' && <div className="voice-dictation-review">
      <label>{t('Speech language')}
        <select value={spokenLanguage} disabled={phase === 'listening'}
          onChange={(event) => setSpokenLanguage(event.target.value as 'en-US' | 'zh-CN')}>
          <option value="en-US">English</option><option value="zh-CN">中文</option>
        </select>
      </label>
      <p role="status">{t(phase === 'listening' ? 'Listening… Speak, then stop to review.' : 'Review or edit the transcription before using it.')}</p>
      <label>{t('Transcription draft')}
        <textarea value={draft} rows={4} maxLength={40_000} disabled={phase === 'listening'}
          onChange={(event) => { setDraft(event.target.value); setError('') }} />
      </label>
      {error && <p className="voice-dictation-error" role="alert">{error}</p>}
      <small>{t('Your browser may process audio with its speech service. Nothing is sent to the Agent until you submit the message.')}</small>
      <div className="voice-dictation-actions">
        <button type="button" onClick={cancel}>{t('Cancel dictation')}</button>
        {phase === 'review' && <button type="button" disabled={!draft.trim()} onClick={useDraft}>{t('Use reviewed text')}</button>}
      </div>
    </div>}
  </div>
}
