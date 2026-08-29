import { render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import I18nProvider from './I18nProvider'
import { useT } from './i18n'
import LandingPage from './components/LandingPage'
import { zh } from './locales/zh'

const componentSources = import.meta.glob('./components/*.tsx', {
  eager: true,
  query: '?raw',
  import: 'default',
}) as Record<string, string>

function Probe() {
  const t = useT()
  return <p>{t('Approval required')}</p>
}

describe('i18n', () => {
  // 本测试环境不带 localStorage；给 window 塞一个内存版。
  beforeEach(() => {
    const store = new Map<string, string>()
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: (key: string) => store.get(key) ?? null,
        setItem: (key: string, value: string) => void store.set(key, value),
        removeItem: (key: string) => void store.delete(key),
        clear: () => store.clear(),
      },
    })
  })
  afterEach(() => {
    Reflect.deleteProperty(window, 'localStorage')
  })

  it('falls back to English when no preference is saved', () => {
    render(<I18nProvider><Probe /></I18nProvider>)
    expect(screen.getByText('Approval required')).toBeInTheDocument()
  })

  it('renders Chinese when the saved workspace setting selects zh-CN', () => {
    window.localStorage.setItem('ophira-workspace-settings', JSON.stringify({ language: 'zh-CN' }))
    render(<I18nProvider><Probe /></I18nProvider>)
    expect(screen.getByText('需要你的批准')).toBeInTheDocument()
    expect(document.documentElement.lang).toBe('zh-CN')
  })

  it('keeps the English key visible when a dictionary entry is missing', () => {
    window.localStorage.setItem('ophira-workspace-settings', JSON.stringify({ language: 'zh-CN' }))
    function MissingProbe() {
      const t = useT()
      return <p>{t('An untranslated sentence')}</p>
    }
    render(<I18nProvider><MissingProbe /></I18nProvider>)
    expect(screen.getByText('An untranslated sentence')).toBeInTheDocument()
  })

  it('renders the current TradeMind landing proposition and commerce story in Chinese', () => {
    window.localStorage.setItem('ophira-workspace-settings', JSON.stringify({ language: 'zh-CN' }))
    render(<I18nProvider><LandingPage /></I18nProvider>)

    expect(screen.getByText('从产品构想到全球销售。')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /在爆发前发现潜力商品/ })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '你的 AI 商业团队' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '让商业环节彼此连接' })).toBeInTheDocument()
  })

  it('defines Chinese copy for every literal translation key used by a component', () => {
    const keys = Object.entries(componentSources)
      .filter(([path]) => !path.includes('.test.'))
      .flatMap(([, source]) => [...source.matchAll(/\bt\('([^']+)'/g)].map((match) => match[1]))
    const missing = [...new Set(keys)].filter((key) => !zh[key]).sort()

    expect(missing).toEqual([])
  })
})
