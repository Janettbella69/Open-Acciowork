import { useEffect, useMemo, useState } from 'react'
import { errorMessage } from '../api/client'
import {
  getCompanionImagePreview, getCompanionRunActivity,
} from '../api/companion'
import type { Artifact, OphiraEvent, RunStatus } from '../api/types'
import { useT } from '../i18n'
import NativeTaskProgress from './NativeTaskProgress'

const terminal = new Set<RunStatus>(['completed', 'failed', 'cancelled'])

function stepLabel(event: OphiraEvent): string {
  const description = event.data.description
  if (typeof description === 'string' && description.trim()) return description
  const tool = event.data.tool_name
  if (typeof tool === 'string' && tool.trim()) return tool.replace(/^mcp__ophira__/, '')
  return event.type.replaceAll('.', ' ')
}

export default function MobileRunProgress({ runId }: { runId: string }) {
  const t = useT()
  const [events, setEvents] = useState<OphiraEvent[]>([])
  const [artifacts, setArtifacts] = useState<Artifact[]>([])
  const [status, setStatus] = useState<RunStatus | null>(null)
  const [error, setError] = useState('')
  const [preview, setPreview] = useState<string | null>(null)
  const screenshot = artifacts.find((item) =>
    ['image/png', 'image/jpeg', 'image/webp'].includes(item.mime) && item.size_bytes <= 5_000_000)
  const screenshotId = screenshot?.id ?? null

  useEffect(() => {
    let disposed = false
    let cursor = 0
    let waitForUpdate = false
    let timer: number | undefined
    const controller = new AbortController()
    setEvents([])
    setArtifacts([])
    setStatus(null)
    setError('')
    const poll = async () => {
      try {
        const page = await getCompanionRunActivity(runId, cursor, controller.signal, waitForUpdate)
        if (disposed) return
        cursor = page.next_sequence
        setEvents((current) => {
          const known = new Set(current.map((item) => item.id))
          return [...current, ...page.events.filter((item) => !known.has(item.id))].slice(-1_000)
        })
        setArtifacts(page.artifacts)
        setStatus(page.status)
        setError('')
        waitForUpdate = !page.has_more && !terminal.has(page.status)
        timer = window.setTimeout(poll, page.has_more ? 0 : terminal.has(page.status) ? 5_000 : 0)
      } catch (cause) {
        if (disposed || controller.signal.aborted) return
        setError(errorMessage(cause))
        if (cause && typeof cause === 'object' && 'status' in cause
          && (cause.status === 401 || cause.status === 403)) {
          setEvents([])
          setArtifacts([])
          return
        }
        timer = window.setTimeout(poll, 5_000)
      }
    }
    void poll()
    return () => {
      disposed = true
      controller.abort()
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [runId])

  useEffect(() => {
    setPreview(null)
    if (!screenshotId) {
      return
    }
    const controller = new AbortController()
    let objectUrl: string | null = null
    void getCompanionImagePreview(runId, screenshotId, controller.signal).then((blob) => {
      if (controller.signal.aborted) return
      objectUrl = URL.createObjectURL(blob)
      setPreview(objectUrl)
    }).catch(() => {
      if (!controller.signal.aborted) setPreview(null)
    })
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [runId, screenshotId])

  const latestStep = useMemo(() => [...events].reverse().find((event) =>
    event.type === 'tool.started' || event.type === 'task.progress'
    || event.type === 'task.started'), [events])
  const changes = events.filter((event) =>
    event.type === 'artifact.registered' || event.type === 'artifact.updated')

  return <section className="mobile-run-progress" aria-label={t('Live run progress')}>
    <h4>{t('Live run progress')}</h4>
    {status && <small>{t(status)}</small>}
    {error && <p role="alert">{error}</p>}
    {!status && !error && <p role="status">{t('Loading run progress…')}</p>}
    {latestStep && <p><strong>{t('Current step')}:</strong> {stepLabel(latestStep)}</p>}
    <NativeTaskProgress events={events} runId={runId} />
    {screenshot && <div className="mobile-run-screenshot">
      <strong>{t('Latest screenshot')}</strong>
      {preview ? <img src={preview} alt={screenshot.name} />
        : <p>{t('Preview is loading or unavailable.')}</p>}
      <small>{screenshot.name}</small>
    </div>}
    {artifacts.length > 0 && <div className="mobile-run-artifacts">
      <strong>{t('Artifact changes')}</strong>
      <ul>{artifacts.slice(0, 10).map((item) => <li key={item.id}>
        {item.name} · {t('Version')} {item.version}
      </li>)}</ul>
      {changes.length > 0 && <small>{t('{count} artifact updates in this run', { count: changes.length })}</small>}
    </div>}
  </section>
}
