import { useState } from 'react'
import { useT } from '../i18n'
import MemoryReview from './MemoryReview'

export default function RunMemoryPanel({ runId, revision }: { runId: string; revision: number }) {
  const t = useT()
  const [opened, setOpened] = useState(false)
  return (
    <details className="conversation-project-truth" onToggle={(event) => setOpened(event.currentTarget.open)}>
      <summary>{t('Run memories')}</summary>
      <p>{t('Review memories proposed during this run. They take effect only after approval.')}</p>
      {opened && <MemoryReview sourceRunId={runId} revision={revision} />}
    </details>
  )
}
