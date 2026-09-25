export type LocalSettings = {
  workspaceAccess: boolean
  autoReview: boolean
  fullAccess: boolean
  keepAwake: boolean
  rememberProjects: boolean
  approvalMode: 'review_external' | 'review_all' | 'allow_low_risk'
  language: 'auto' | 'en' | 'zh-CN'
  permissionTimeout: '120' | '300' | '600'
  pluginAccess: 'ask' | 'disabled'
  browserAccess: 'ask' | 'disabled'
  computerDiscoverable: boolean
  workspaceMemory: boolean
  conversationMemory: boolean
}

export const localSettingsStorageKey = 'ophira-workspace-settings'

export const defaultLocalSettings: LocalSettings = {
  workspaceAccess: true,
  autoReview: true,
  fullAccess: false,
  keepAwake: true,
  rememberProjects: true,
  approvalMode: 'review_external',
  language: 'auto',
  permissionTimeout: '300',
  pluginAccess: 'ask',
  browserAccess: 'ask',
  computerDiscoverable: false,
  workspaceMemory: true,
  conversationMemory: false,
}

export function loadLocalSettings(): LocalSettings {
  try {
    const saved = window.localStorage.getItem(localSettingsStorageKey)
    return saved
      ? { ...defaultLocalSettings, ...JSON.parse(saved) as Partial<LocalSettings> }
      : defaultLocalSettings
  } catch {
    return defaultLocalSettings
  }
}

export function saveLocalSettings(settings: LocalSettings): void {
  window.localStorage.setItem(localSettingsStorageKey, JSON.stringify(settings))
}
