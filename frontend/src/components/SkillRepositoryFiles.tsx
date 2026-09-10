import { useEffect, useState } from 'react'
import type { SkillFilePreview } from '../api/skillRepository'
import type { SkillProposalFile } from '../api/skillProposals'
import { useT } from '../i18n'
import { useCollaborationRequest } from './useCollaborationRequest'

type Props = { files: SkillProposalFile[]; read: (path: string, signal: AbortSignal) => Promise<SkillFilePreview>; download?: (path: string) => string; onUnavailable: () => void }
export default function SkillRepositoryFiles({ files, read, download, onUnavailable }: Props) {
  const t = useT()
  const [path, setPath] = useState('SKILL.md')
  const [preview, setPreview] = useState<SkillFilePreview | null>(null)
  const { run, busy, error } = useCollaborationRequest(onUnavailable)
  useEffect(() => {
    const file = files.find((row) => row.path === path)
    if (!file) return
    void run(async (signal) => {
      const value = await read(path, signal)
      if (value.path !== path || value.content_hash !== file.content_hash || value.size_bytes !== file.size_bytes) throw new Error('Private Skill file did not match its recorded checksum.')
      return value
    }, setPreview)
  }, [path, files, read, run])
  return <section className="private-skill-files" aria-label={t('Package files')}>
    <label>{t('Inspect package file')}<select disabled={busy} value={path} onChange={(e) => { setPreview(null); setPath(e.target.value) }}>{files.map((file) => <option key={file.path} value={file.path}>{file.path} · {file.size_bytes.toLocaleString()} B</option>)}</select></label>
    {error && <p role="alert">{t(error)}</p>}
    {busy && <p role="status">{t('Loading package file…')}</p>}
    {preview && <>
      <small>SHA-256 {preview.content_hash}</small>
      {preview.text === null ? <p>{t('Binary file. Download the original package to inspect it.')}</p> : <pre>{preview.text}</pre>}
      {preview.truncated && <p>{t('Preview is shortened. The original download contains the complete file.')}</p>}
      {download && <a href={download(path)} download rel="noreferrer">{t('Download original file')}</a>}
    </>}
  </section>
}
