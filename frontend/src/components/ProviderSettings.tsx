import { useEffect, useState, type FormEvent } from 'react'
import {
  errorMessage,
  listProviders,
  preflightProvider,
  saveProvider,
  type ProviderInput,
} from '../api/client'
import type { ProviderPreflightResult, ProviderProfile } from '../api/types'

type EditorState = {
  id: string
  isNew: boolean
  display_name: string
  base_url: string
  auth_env_name: 'ANTHROPIC_API_KEY' | 'ANTHROPIC_AUTH_TOKEN'
  model: string
  subagent_model: string
  region: string
  production_eligible: boolean
  secret: string
  source: ProviderProfile | null
}

const capabilityLabels: Array<[keyof ProviderProfile['capabilities'], string]> = [
  ['images', 'Images'],
  ['prompt_caching', 'Prompt caching'],
  ['extended_thinking', 'Extended thinking'],
  ['tool_search', 'Tool search'],
]

function emptyEditor(): EditorState {
  return {
    id: '',
    isNew: true,
    display_name: '',
    base_url: '',
    auth_env_name: 'ANTHROPIC_AUTH_TOKEN',
    model: '',
    subagent_model: '',
    region: 'unspecified',
    production_eligible: false,
    secret: '',
    source: null,
  }
}

function editorFor(profile: ProviderProfile): EditorState {
  return {
    id: profile.id,
    isNew: false,
    display_name: profile.display_name,
    base_url: profile.base_url ?? '',
    auth_env_name: profile.auth_env_name,
    model: profile.model,
    subagent_model: profile.subagent_model ?? '',
    region: profile.region,
    production_eligible: profile.production_eligible,
    secret: '',
    source: profile,
  }
}

function payloadFrom(editor: EditorState): ProviderInput {
  const source = editor.source
  const payload: ProviderInput = {
    display_name: editor.display_name.trim(),
    base_url: editor.base_url.trim() || null,
    auth_env_name: editor.auth_env_name,
    model: editor.model.trim(),
    region: editor.region.trim() || 'unspecified',
    production_eligible: editor.production_eligible,
    // Round-trip fields the form does not edit so a save never resets them.
    default_haiku_model: source?.default_haiku_model ?? null,
    default_sonnet_model: source?.default_sonnet_model ?? null,
    default_opus_model: source?.default_opus_model ?? null,
    subagent_model: editor.subagent_model.trim() || null,
    budget_scale: source?.budget_scale ?? 1.0,
    terms_checked_at: source?.terms_checked_at ?? null,
    ...(source?.capabilities ? { capabilities: source.capabilities } : {}),
  }
  if (editor.secret.trim()) payload.secret = editor.secret.trim()
  return payload
}

export default function ProviderSettings() {
  const [providers, setProviders] = useState<ProviderProfile[]>([])
  const [editor, setEditor] = useState<EditorState | null>(null)
  const [preflights, setPreflights] = useState<Record<string, ProviderPreflightResult>>({})
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    let active = true
    void listProviders()
      .then((items) => { if (active) setProviders(items) })
      .catch((loadError) => { if (active) setError(errorMessage(loadError)) })
    return () => { active = false }
  }, [])

  const update = <Key extends keyof EditorState>(key: Key, value: EditorState[Key]) => {
    setEditor((current) => (current ? { ...current, [key]: value } : current))
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!editor) return
    const providerId = editor.isNew
      ? editor.id.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, '-')
      : editor.id
    if (!providerId || !editor.display_name.trim() || !editor.model.trim()) return
    setBusy('save')
    setError('')
    try {
      const saved = await saveProvider(providerId, payloadFrom(editor))
      setProviders((current) => {
        const others = current.filter((item) => item.id !== saved.id)
        return [...others, saved].sort((a, b) => a.id.localeCompare(b.id))
      })
      setEditor(null)
    } catch (saveError) {
      setError(errorMessage(saveError))
    } finally {
      setBusy('')
    }
  }

  const runPreflight = async (providerId: string) => {
    setBusy(`preflight:${providerId}`)
    setError('')
    try {
      const result = await preflightProvider(providerId)
      setPreflights((current) => ({ ...current, [providerId]: result }))
      const refreshed = await listProviders()
      setProviders(refreshed)
    } catch (preflightError) {
      setError(errorMessage(preflightError))
    } finally {
      setBusy('')
    }
  }

  return (
    <div className="settings-card provider-settings">
      {error && <p className="conversation-error" role="alert">{error}</p>}

      {providers.map((provider) => {
        const preflight = preflights[provider.id]
        return (
          <article key={provider.id} className="provider-row">
            <header>
              <div>
                <strong>{provider.display_name}</strong>
                <small>
                  <code>{provider.model}</code>
                  {provider.base_url ? <> · {provider.base_url}</> : ' · Anthropic default endpoint'}
                </small>
              </div>
              <div className="provider-row-actions">
                <button
                  type="button"
                  disabled={busy === `preflight:${provider.id}`}
                  onClick={() => void runPreflight(provider.id)}
                >
                  {busy === `preflight:${provider.id}` ? 'Preflighting…' : 'Run preflight'}
                </button>
                <button type="button" onClick={() => setEditor(editorFor(provider))}>Edit</button>
              </div>
            </header>
            <div className="provider-row-badges">
              <span data-tone={provider.secret_configured ? 'ok' : 'warn'}>
                {provider.secret_configured ? 'Key configured' : 'No key stored'}
              </span>
              <span data-tone={provider.production_eligible ? 'ok' : 'neutral'}>
                {provider.production_eligible ? 'Production eligible' : 'Not production-cleared'}
              </span>
              <span data-tone="neutral">{provider.region}</span>
              {capabilityLabels.map(([key, label]) => (
                <span key={key} data-tone={provider.capabilities[key] ? 'ok' : 'off'}>
                  {label}: {provider.capabilities[key] ? 'yes' : 'no'}
                </span>
              ))}
            </div>
            <p className="provider-row-status">
              {provider.last_error_code ? (
                <span data-tone="error">
                  {provider.last_error_code} · {provider.last_error_message ?? 'See preflight details.'}
                </span>
              ) : provider.last_verified_at ? (
                <span data-tone="ok">
                  Verified {new Date(provider.last_verified_at).toLocaleString()}
                </span>
              ) : (
                <span data-tone="neutral">Never verified — run preflight before first use.</span>
              )}
            </p>
            {preflight && (
              <p className="provider-row-preflight" data-ok={preflight.ok}>
                {preflight.ok
                  ? `预检通过 · session ${preflight.sdk_session_id ?? 'n/a'} · ${preflight.discovered_skills.length} skills discovered`
                  : preflight.message}
              </p>
            )}
          </article>
        )
      })}

      {!providers.length && !error && (
        <p className="settings-integration-note">
          No provider profiles yet. Add one to run the Agent on a real Anthropic-compatible endpoint.
        </p>
      )}

      {editor ? (
        <form className="provider-editor" aria-label="Provider editor" onSubmit={(event) => void submit(event)}>
          <h3>{editor.isNew ? 'Add provider' : `Edit ${editor.id}`}</h3>
          {editor.isNew && (
            <label>Profile id
              <input value={editor.id} onChange={(event) => update('id', event.target.value)} placeholder="deepseek" required />
            </label>
          )}
          <label>Display name
            <input value={editor.display_name} onChange={(event) => update('display_name', event.target.value)} required />
          </label>
          <label>Base URL
            <input value={editor.base_url} onChange={(event) => update('base_url', event.target.value)} placeholder="https://api.deepseek.com/anthropic" />
          </label>
          <label>Auth variable
            <select value={editor.auth_env_name} onChange={(event) => update('auth_env_name', event.target.value as EditorState['auth_env_name'])}>
              <option value="ANTHROPIC_AUTH_TOKEN">ANTHROPIC_AUTH_TOKEN</option>
              <option value="ANTHROPIC_API_KEY">ANTHROPIC_API_KEY</option>
            </select>
          </label>
          <label>Model
            <input value={editor.model} onChange={(event) => update('model', event.target.value)} placeholder="deepseek-v4-pro" required />
          </label>
          <label>Subagent model
            <input value={editor.subagent_model} onChange={(event) => update('subagent_model', event.target.value)} placeholder="deepseek-v4-flash" />
          </label>
          <label>API key
            <input
              type="password"
              autoComplete="off"
              value={editor.secret}
              onChange={(event) => update('secret', event.target.value)}
              placeholder={editor.isNew ? 'Required for first preflight' : 'Leave blank to keep the stored key'}
            />
          </label>
          <label>Region
            <input value={editor.region} onChange={(event) => update('region', event.target.value)} />
          </label>
          <label className="provider-editor-checkbox">
            <input
              type="checkbox"
              checked={editor.production_eligible}
              onChange={(event) => update('production_eligible', event.target.checked)}
            />
            Production eligible (terms reviewed; never a coding-plan key)
          </label>
          <footer>
            <button type="button" onClick={() => setEditor(null)}>Cancel</button>
            <button type="submit" disabled={busy === 'save'}>
              {busy === 'save' ? 'Saving…' : 'Save provider'}
            </button>
          </footer>
        </form>
      ) : (
        <button type="button" className="provider-add" onClick={() => setEditor(emptyEditor())}>
          ＋ Add provider
        </button>
      )}
    </div>
  )
}
