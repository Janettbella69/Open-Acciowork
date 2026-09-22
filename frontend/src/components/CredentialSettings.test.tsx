import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { deleteCredential, exportCredential, listCredentials } from '../api/client'
import type { CredentialArchive, CredentialView } from '../api/credentials'
import { I18nContext } from '../i18n'
import CredentialSettings from './CredentialSettings'

vi.mock('../api/client', () => ({
  listCredentials: vi.fn(), exportCredential: vi.fn(), deleteCredential: vi.fn(),
  errorMessage: (error: unknown) => error instanceof Error ? error.message : 'unknown error',
}))

const credential: CredentialView = {
  kind: 'provider', id: 'test', display_name: 'Test provider', revision: 'a'.repeat(64),
  configured: true, source: 'stored', deleted_at: null, deleted_by: null,
}
const deleted: CredentialView = {
  ...credential, configured: false, source: 'deleted', revision: 'b'.repeat(64),
  deleted_at: '2026-09-22T10:00:00Z', deleted_by: 'local-operator',
}
const archive: CredentialArchive = {
  format: 'ophira-encrypted-credential', version: 1, kdf: 'scrypt-32768-8-1',
  cipher: 'aes-256-gcm', salt: 'salt', nonce: 'nonce', ciphertext: 'encrypted bytes',
}
const password = 'private backup password'
const createURL = vi.fn<(blob: Blob) => string>(() => 'blob:private-archive')
const revokeURL = vi.fn()
let clicked: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listCredentials).mockResolvedValue([credential])
  vi.mocked(exportCredential).mockResolvedValue(archive)
  vi.mocked(deleteCredential).mockResolvedValue(deleted)
  vi.stubGlobal('URL', class extends URL {
    static createObjectURL = createURL
    static revokeObjectURL = revokeURL
  })
  clicked = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
})
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

async function openPanel(onChanged = vi.fn()) {
  const result = render(<CredentialSettings onChanged={onChanged} />)
  expect(listCredentials).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Credential backups and deletion' }))
  await screen.findByText('Test provider')
  return result
}
function chooseExport() {
  fireEvent.click(screen.getByRole('button', { name: 'Export Test provider' }))
  fireEvent.change(screen.getByLabelText('Archive password'), { target: { value: password } })
  fireEvent.change(screen.getByLabelText('Repeat archive password'), { target: { value: password } })
}

describe('CredentialSettings', () => {
  it('loads on demand and disables actions for deleted or missing credentials', async () => {
    vi.mocked(listCredentials).mockResolvedValue([credential, {
      ...deleted, kind: 'connector', id: 'revoked', display_name: 'Revoked connector',
    }, { ...credential, id: 'missing', display_name: 'Empty provider', configured: false, source: 'missing' }])
    await openPanel()
    expect(screen.queryByText(credential.revision)).not.toBeInTheDocument()
    for (const name of ['Revoked connector', 'Empty provider']) {
      expect(screen.getByRole('button', { name: `Export ${name}` })).toBeDisabled()
      expect(screen.getByRole('button', { name: `Delete ${name}` })).toBeDisabled()
    }
  })

  it('requires matching passwords, downloads the encrypted payload and clears the form', async () => {
    await openPanel()
    chooseExport()
    fireEvent.change(screen.getByLabelText('Repeat archive password'), { target: { value: 'different password' } })
    fireEvent.submit(screen.getByRole('form', { name: 'Credential review' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Use matching archive passwords')
    expect(exportCredential).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('Repeat archive password'), { target: { value: password } })
    fireEvent.submit(screen.getByRole('form', { name: 'Credential review' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Encrypted credential downloaded')
    expect(exportCredential).toHaveBeenCalledWith(credential, password, expect.any(AbortSignal))
    const blob = createURL.mock.calls[0][0] as Blob
    const contents = await new Promise<string>((resolve) => {
      const reader = new FileReader()
      reader.onload = () => resolve(reader.result as string)
      reader.readAsText(blob)
    })
    expect(JSON.parse(contents)).toEqual(archive)
    expect(contents).not.toContain(password)
    expect(clicked).toHaveBeenCalledTimes(1)
    expect(screen.queryByLabelText('Archive password')).not.toBeInTheDocument()
    expect(deleteCredential).not.toHaveBeenCalled()
  })

  it('requires explicit deletion confirmation and prevents duplicate submissions', async () => {
    let finish!: (value: CredentialView) => void
    vi.mocked(deleteCredential).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const changed = vi.fn()
    await openPanel(changed)
    fireEvent.click(screen.getByRole('button', { name: 'Delete Test provider' }))
    const form = screen.getByRole('form', { name: 'Credential review' })
    expect(screen.getByRole('button', { name: 'Confirm credential deletion' })).toBeDisabled()
    fireEvent.submit(form)
    expect(deleteCredential).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.submit(form)
    fireEvent.submit(form)
    expect(deleteCredential).toHaveBeenCalledTimes(1)
    expect(deleteCredential).toHaveBeenCalledWith(credential, expect.any(AbortSignal))
    expect(screen.getByRole('button', { name: 'Refresh credentials' })).toBeDisabled()
    await act(async () => { finish(deleted) })
    expect(await screen.findByRole('status')).toHaveTextContent('Local credential access deleted')
    expect(screen.getByRole('button', { name: 'Export Test provider' })).toBeDisabled()
    expect(screen.queryByRole('form')).not.toBeInTheDocument()
    expect(changed).toHaveBeenCalledTimes(1)
  })

  it('refreshes after an ambiguous deletion failure and clears its previous confirmation', async () => {
    vi.mocked(deleteCredential).mockRejectedValueOnce(new Error('Response lost'))
    await openPanel()
    vi.mocked(listCredentials).mockResolvedValueOnce([deleted])
    fireEvent.click(screen.getByRole('button', { name: 'Delete Test provider' }))
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.submit(screen.getByRole('form'))
    expect(await screen.findByRole('alert')).toHaveTextContent('Response lost')
    await waitFor(() => expect(listCredentials).toHaveBeenCalledTimes(2))
    expect(await screen.findByRole('button', { name: 'Delete Test provider' })).toBeDisabled()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('requires fresh review after key rotation and clears passwords on export failure', async () => {
    vi.mocked(exportCredential).mockRejectedValueOnce(new Error('Credential changed. Refresh and review it again.'))
    await openPanel()
    chooseExport()
    const rotated = { ...credential, revision: 'c'.repeat(64) }
    vi.mocked(listCredentials).mockResolvedValueOnce([rotated])
    fireEvent.submit(screen.getByRole('form'))
    expect(await screen.findByRole('alert')).toHaveTextContent('Credential changed')
    await screen.findByRole('button', { name: 'Export Test provider' })
    expect(screen.queryByLabelText('Archive password')).not.toBeInTheDocument()
    expect(clicked).not.toHaveBeenCalled()
    chooseExport()
    fireEvent.submit(screen.getByRole('form'))
    await screen.findByRole('status')
    expect(exportCredential).toHaveBeenLastCalledWith(rotated, password, expect.any(AbortSignal))
  })

  it('aborts closed panels and never downloads a late export', async () => {
    let finish!: (value: CredentialArchive) => void
    vi.mocked(exportCredential).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    await openPanel()
    chooseExport()
    fireEvent.submit(screen.getByRole('form'))
    const signal = vi.mocked(exportCredential).mock.calls[0][2]!
    fireEvent.click(screen.getByRole('button', { name: 'Credential backups and deletion' }))
    expect(signal.aborted).toBe(true)
    await act(async () => { finish(archive) })
    expect(createURL).not.toHaveBeenCalled()
    expect(clicked).not.toHaveBeenCalled()
  })

  it('discards metadata from a closed panel when it is reopened', async () => {
    let finish!: (value: CredentialView[]) => void
    vi.mocked(listCredentials).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    render(<CredentialSettings onChanged={vi.fn()} />)
    const toggle = screen.getByRole('button', { name: 'Credential backups and deletion' })
    fireEvent.click(toggle)
    const signal = vi.mocked(listCredentials).mock.calls[0][0]!
    fireEvent.click(toggle)
    expect(signal.aborted).toBe(true)
    vi.mocked(listCredentials).mockResolvedValueOnce([deleted])
    fireEvent.click(toggle)
    await screen.findByText('Test provider')
    await act(async () => { finish([credential]) })
    expect(screen.getByRole('button', { name: 'Delete Test provider' })).toBeDisabled()
  })

  it('shows Chinese confirmation, passwords and the scope of deletion', async () => {
    render(<I18nContext.Provider value="zh-CN"><CredentialSettings onChanged={vi.fn()} /></I18nContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: '凭据备份与删除' }))
    fireEvent.click(await screen.findByRole('button', { name: '导出 Test provider' }))
    expect(screen.getByLabelText('备份密码')).toHaveAttribute('type', 'password')
    fireEvent.click(screen.getByRole('button', { name: '删除 Test provider' }))
    expect(screen.getByRole('button', { name: '确认删除凭据' })).toBeDisabled()
    expect(screen.getByText(/此操作不会撤销服务商端的密钥/)).toBeInTheDocument()
  })
})
