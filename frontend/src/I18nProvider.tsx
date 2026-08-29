import { useEffect, useState, type ReactNode } from 'react'
import {
  I18nContext,
  languageChangedEvent,
  readLanguagePreference,
  resolveLanguage,
  type ResolvedLanguage,
} from './i18n'

export default function I18nProvider({ children }: { children: ReactNode }) {
  const [language, setLanguage] = useState<ResolvedLanguage>(() => resolveLanguage(readLanguagePreference()))
  useEffect(() => {
    const sync = () => setLanguage(resolveLanguage(readLanguagePreference()))
    window.addEventListener('storage', sync)
    window.addEventListener(languageChangedEvent, sync)
    return () => {
      window.removeEventListener('storage', sync)
      window.removeEventListener(languageChangedEvent, sync)
    }
  }, [])
  useEffect(() => {
    document.documentElement.lang = language
  }, [language])
  return <I18nContext.Provider value={language}>{children}</I18nContext.Provider>
}
