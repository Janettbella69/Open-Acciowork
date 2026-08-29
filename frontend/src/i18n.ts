import { createContext, useContext } from 'react'
import { zh } from './locales/zh'

export type LanguagePreference = 'auto' | 'en' | 'zh-CN'
export type ResolvedLanguage = 'en' | 'zh-CN'
export type Translate = (text: string, vars?: Record<string, string | number>) => string

const settingsKey = 'ophira-workspace-settings'
export const languageChangedEvent = 'ophira-language-changed'

export function readLanguagePreference(): LanguagePreference {
  try {
    const saved = window.localStorage.getItem(settingsKey)
    if (!saved) return 'auto'
    const parsed = JSON.parse(saved) as { language?: LanguagePreference }
    return parsed.language === 'en' || parsed.language === 'zh-CN' ? parsed.language : 'auto'
  } catch {
    return 'auto'
  }
}

export function resolveLanguage(preference: LanguagePreference): ResolvedLanguage {
  if (preference !== 'auto') return preference
  return navigator.language?.toLowerCase().startsWith('zh') ? 'zh-CN' : 'en'
}

export const I18nContext = createContext<ResolvedLanguage>('en')

export function useLanguage(): ResolvedLanguage {
  return useContext(I18nContext)
}

/** t('Using {tool}', {tool}) — keys are the English copy; missing entries fall back to English. */
export function useT(): Translate {
  const language = useContext(I18nContext)
  return (text: string, vars?: Record<string, string | number>) => {
    let out = language === 'zh-CN' ? (zh[text] ?? text) : text
    if (vars) {
      for (const [key, value] of Object.entries(vars)) out = out.split(`{${key}}`).join(String(value))
    }
    return out
  }
}
