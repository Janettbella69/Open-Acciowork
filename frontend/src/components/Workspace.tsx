import { useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  api,
  followRun,
  type Decision,
  type Project,
  type ProjectDetail,
  type RunEvent,
  type WorkspaceFile,
} from '../api'
import { useT } from '../i18n'
import { buildTurn } from '../thread'
import Brand from './Brand'
import Turn from './Turn'
import './workspace.css'

function setQuery(params: Record<string, string | null>) {
  const url = new URL(window.location.href)
  for (const [key, value] of Object.entries(params)) {
    if (value === null) url.searchParams.delete(key)
    else url.searchParams.set(key, value)
  }
  window.history.replaceState(null, '', url)
}

export default function Workspace() {
  const t = useT()
  const query = new URLSearchParams(window.location.search)
  const devMode = query.get('dev') === '1'
  const [projects, setProjects] = useState<Project[]>([])
  const [current, setCurrent] = useState<ProjectDetail | null>(null)
  const [events, setEvents] = useState<Record<string, RunEvent[]>>({})
  const [files, setFiles] = useState<WorkspaceFile[]>([])
  const [showFiles, setShowFiles] = useState(false)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<string | null>(null)
  const followed = useRef(new Map<string, () => void>())
  const started = useRef(false)
  const threadEnd = useRef<HTMLDivElement>(null)

  const explain = useCallback(
    (error: unknown) => {
      if (error instanceof ApiError && error.status === 401) {
        setNotice(t('The workspace needs you to sign in. Reload the page to sign in.'))
      } else if (error instanceof ApiError) {
        setNotice(error.message)
      } else {
        setNotice(t('Could not reach the backend.'))
      }
    },
    [t],
  )

  const refresh = useCallback(async (projectId: string) => {
    const [detail, list] = await Promise.all([api.project(projectId), api.files(projectId)])
    setCurrent(detail)
    setFiles(list)
  }, [])

  const follow = useCallback(
    (projectId: string, runId: string) => {
      if (followed.current.has(runId)) return
      const stop = followRun(
        runId,
        (event) =>
          setEvents((all) => {
            const list = all[runId] ?? []
            if (list.some((e) => e.seq === event.seq)) return all
            return { ...all, [runId]: [...list, event] }
          }),
        () => void refresh(projectId).catch(explain),
      )
      followed.current.set(runId, stop)
    },
    [refresh, explain],
  )

  const open = useCallback(
    async (projectId: string) => {
      followed.current.forEach((stop) => stop())
      followed.current.clear()
      setEvents({})
      setNotice(null)
      try {
        const [detail, list] = await Promise.all([api.project(projectId), api.files(projectId)])
        setCurrent(detail)
        setFiles(list)
        setQuery({ project: projectId })
        detail.runs.forEach((run) => follow(projectId, run.id))
      } catch (error) {
        explain(error)
      }
    },
    [follow, explain],
  )

  const send = useCallback(
    async (text: string) => {
      const prompt = text.trim()
      if (!prompt || busy) return
      setBusy(true)
      setNotice(null)
      try {
        let project = current
        if (!project) {
          const created = await api.createProject(prompt.slice(0, 24))
          setProjects((list) => [created, ...list])
          project = { ...created, runs: [], active_run_id: null }
          setQuery({ project: created.id, brief: null })
        }
        const run = await api.startRun(project.id, prompt)
        setCurrent({ ...project, runs: [...project.runs, run], active_run_id: run.id })
        setDraft('')
        follow(project.id, run.id)
      } catch (error) {
        explain(error)
      } finally {
        setBusy(false)
      }
    },
    [busy, current, follow, explain],
  )

  useEffect(() => {
    if (started.current) return
    started.current = true
    const brief = query.get('brief')
    const wanted = query.get('project')
    api
      .projects()
      .then((list) => {
        setProjects(list)
        if (brief) void send(brief)
        else if (wanted && list.some((p) => p.id === wanted)) void open(wanted)
        else if (list[0]) void open(list[0].id)
      })
      .catch(explain)
    // Run once on arrival; later navigation happens through open() and send().
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    threadEnd.current?.scrollIntoView?.({ block: 'end' })
  }, [events])

  useEffect(() => () => followed.current.forEach((stop) => stop()), [])

  const runs = current?.runs ?? []
  const views = runs.map((run) => ({ run, view: buildTurn(events[run.id] ?? [], run.status) }))
  const active = views.find(({ view }) => view.status === 'running')?.run

  const decide = async (runId: string, requestId: string, decision: Decision) => {
    try {
      await api.decide(runId, requestId, decision)
    } catch (error) {
      explain(error)
    }
  }

  const upload = async (file: File) => {
    setNotice(null)
    try {
      let projectId = current?.id
      if (!projectId) {
        const created = await api.createProject(file.name.slice(0, 24))
        setProjects((list) => [created, ...list])
        setCurrent({ ...created, runs: [], active_run_id: null })
        setQuery({ project: created.id })
        projectId = created.id
      }
      const { saved } = await api.upload(projectId, file)
      setFiles(await api.files(projectId))
      setDraft((text) => `${text}${text ? '\n' : ''}${t('I uploaded {path}', { path: saved[0] })}`)
    } catch (error) {
      explain(error)
    }
  }

  const startOver = () => {
    followed.current.forEach((stop) => stop())
    followed.current.clear()
    setCurrent(null)
    setEvents({})
    setFiles([])
    setNotice(null)
    setQuery({ project: null })
  }

  return (
    <div className="workspace" data-files={showFiles}>
      <aside className="workspace-sidebar">
        <a href="/" className="workspace-home" aria-label={t('TradeMind home')}>
          <Brand compact />
        </a>
        <button type="button" className="workspace-new" onClick={startOver}>
          + {t('New project')}
        </button>
        <nav aria-label={t('Projects')}>
          {projects.map((project) => (
            <button
              type="button"
              key={project.id}
              aria-current={project.id === current?.id ? 'page' : undefined}
              onClick={() => void open(project.id)}
            >
              {project.name}
            </button>
          ))}
        </nav>
      </aside>

      <main className="workspace-main">
        <header className="workspace-header">
          <h1>{current?.name ?? t('New project')}</h1>
          {current && (
            <button type="button" onClick={() => setShowFiles((value) => !value)}>
              {t('Files')} {files.length > 0 && <span>{files.length}</span>}
            </button>
          )}
        </header>

        <section className="workspace-thread" aria-live="polite">
          {views.length === 0 && (
            <p className="workspace-empty">{t('Tell TradeMind what you want to sell, and where.')}</p>
          )}
          {views.map(({ run, view }) => (
            <Turn
              key={run.id}
              prompt={run.prompt}
              view={view}
              devMode={devMode}
              onDecide={(requestId, decision) => decide(run.id, requestId, decision)}
              onFeedback={async (text) => {
                await api.feedback(run.project_id, text, run.id).catch(explain)
              }}
            />
          ))}
          <div ref={threadEnd} />
        </section>

        {notice && (
          <p className="workspace-notice" role="alert">
            {notice}
          </p>
        )}

        <form
          className="workspace-composer"
          onSubmit={(event) => {
            event.preventDefault()
            void send(draft)
          }}
        >
          <textarea
            aria-label={t('Message')}
            placeholder={
              current ? t('Reply…') : t('What do you want to sell, and where? A rough idea is fine.')
            }
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault()
                void send(draft)
              }
            }}
            rows={3}
          />
          <div className="composer-bar">
            <label className="composer-upload">
              {t('Upload file')}
              <input
                type="file"
                hidden
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  event.target.value = ''
                  if (file) void upload(file)
                }}
              />
            </label>
            {active ? (
              <button type="button" onClick={() => void api.cancel(active.id).catch(explain)}>
                {t('Stop')}
              </button>
            ) : (
              <button type="submit" className="primary" disabled={busy || !draft.trim()}>
                {t('Send')}
              </button>
            )}
          </div>
        </form>
      </main>

      {showFiles && current && (
        <aside className="workspace-files" aria-label={t('Files')}>
          <h2>{t('Files')}</h2>
          {files.length === 0 ? (
            <p>{t('No files yet.')}</p>
          ) : (
            <ul>
              {files.map((file) => (
                <li key={file.path}>
                  <a href={api.fileUrl(current.id, file.path)} download>
                    {file.path}
                  </a>
                </li>
              ))}
            </ul>
          )}
        </aside>
      )}
    </div>
  )
}
