import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import VoiceDictation from './VoiceDictation'

class FakeRecognition {
  static current: FakeRecognition
  lang = ''
  continuous = false
  interimResults = false
  onresult: ((event: { results: { isFinal: boolean; 0: { transcript: string } }[] }) => void) | null = null
  onerror: ((event: { error: string }) => void) | null = null
  onend: (() => void) | null = null
  start = vi.fn()
  stop = vi.fn(() => this.onend?.())
  abort = vi.fn()

  constructor() { FakeRecognition.current = this }
}

afterEach(() => vi.unstubAllGlobals())

it('requires review before inserting recognized speech into a message', () => {
  vi.stubGlobal('webkitSpeechRecognition', FakeRecognition)
  const onChange = vi.fn()
  render(<VoiceDictation value="Existing text" onChange={onChange} inline />)
  fireEvent.click(screen.getByRole('button', { name: 'Dictate message' }))
  expect(FakeRecognition.current.start).toHaveBeenCalledOnce()
  act(() => FakeRecognition.current.onresult?.({ results: [
    { isFinal: true, 0: { transcript: 'Check the order' } },
  ] }))
  expect(onChange).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Stop dictation' }))
  expect(screen.getByRole('textbox', { name: 'Transcription draft' })).toHaveValue('Check the order')
  fireEvent.change(screen.getByRole('textbox', { name: 'Transcription draft' }), {
    target: { value: 'Check the new order' },
  })
  expect(onChange).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Use reviewed text' }))
  expect(onChange).toHaveBeenCalledWith('Existing text\nCheck the new order')
})

it('discards audio and draft when cancelled or the page closes', () => {
  vi.stubGlobal('SpeechRecognition', FakeRecognition)
  const onChange = vi.fn()
  const view = render(<VoiceDictation value="" onChange={onChange} />)
  fireEvent.click(screen.getByRole('button', { name: 'Dictate message' }))
  const first = FakeRecognition.current
  fireEvent.click(screen.getByRole('button', { name: 'Cancel dictation' }))
  expect(first.abort).toHaveBeenCalledOnce()
  first.onresult?.({ results: [{ isFinal: true, 0: { transcript: 'late result' } }] })
  expect(screen.queryByRole('button', { name: 'Use reviewed text' })).not.toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'Dictate message' }))
  const second = FakeRecognition.current
  view.unmount()
  expect(second.abort).toHaveBeenCalledOnce()
  expect(onChange).not.toHaveBeenCalled()
})

it('keeps typed input available when browser speech recognition is unsupported', () => {
  const onChange = vi.fn()
  render(<VoiceDictation value="Typed message" onChange={onChange} inline />)
  expect(screen.getByRole('button', { name: 'Dictate message' })).toBeDisabled()
  expect(screen.getByText(/supporting browser and a secure connection/)).toBeInTheDocument()
})

it('does not start recognition on an insecure origin', () => {
  vi.stubGlobal('SpeechRecognition', FakeRecognition)
  vi.stubGlobal('isSecureContext', false)
  render(<VoiceDictation value="Typed message" onChange={vi.fn()} inline />)
  expect(screen.getByRole('button', { name: 'Dictate message' })).toBeDisabled()
  expect(screen.getByText(/secure connection/)).toBeVisible()
})

it('lets the user choose Chinese and reports a denied microphone without inserting text', () => {
  vi.stubGlobal('SpeechRecognition', FakeRecognition)
  const onChange = vi.fn()
  render(<VoiceDictation value="" onChange={onChange} inline />)
  fireEvent.change(screen.getByRole('combobox', { name: 'Speech language' }), {
    target: { value: 'zh-CN' },
  })
  fireEvent.click(screen.getByRole('button', { name: 'Dictate message' }))
  expect(FakeRecognition.current.lang).toBe('zh-CN')
  act(() => FakeRecognition.current.onerror?.({ error: 'not-allowed' }))
  expect(FakeRecognition.current.abort).toHaveBeenCalledOnce()
  expect(screen.getByRole('alert')).toHaveTextContent('permission was denied')
  expect(onChange).not.toHaveBeenCalled()
})
