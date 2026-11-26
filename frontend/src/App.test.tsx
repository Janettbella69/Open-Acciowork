import { render, screen } from '@testing-library/react'
import App from './App'

beforeEach(() => {
  vi.stubGlobal('fetch', () => Promise.resolve(new Response('[]')))
})
afterEach(() => vi.unstubAllGlobals())

test('the root is the landing page and its form leads to the workspace', () => {
  window.history.replaceState(null, '', '/')
  const { container } = render(<App />)
  expect(container.querySelector('form.opportunity-prompt')).toHaveAttribute('action', '/app')
  expect(document.title).toBe('TradeMind - AI Global Commerce Workspace')
})

test('/app is the workspace', async () => {
  window.history.replaceState(null, '', '/app')
  render(<App />)
  expect(await screen.findByRole('textbox', { name: 'Message' })).toBeInTheDocument()
  expect(document.title).toBe('TradeMind Workspace - AI Global Commerce Workspace')
})

test('the workspace speaks Chinese to a Chinese browser', async () => {
  vi.spyOn(navigator, 'language', 'get').mockReturnValue('zh-CN')
  window.history.replaceState(null, '', '/app')
  render(<App />)
  expect(await screen.findByRole('textbox', { name: '消息' })).toHaveAttribute(
    'placeholder', '想卖什么、卖到哪里？说个大概就行。',
  )
})
