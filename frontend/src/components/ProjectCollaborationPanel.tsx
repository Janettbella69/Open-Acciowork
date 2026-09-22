import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { getCollaborationCapabilities } from '../api/client'
import { requireCollaborationScope, type CollaborationCapabilities } from '../api/collaboration'
import type { OpportunityProject, OphiraEvent, RunRecord } from '../api/types'
import { useT } from '../i18n'
import ProjectComments from './ProjectComments'
import ProjectMembers from './ProjectMembers'
import { useCollaborationRequest } from './useCollaborationRequest'
import './project-collaboration.css'

type PanelProps = { project: OpportunityProject; currentRun: RunRecord | null; events: OphiraEvent[] }

function Collaboration({ project, currentRun, events }: PanelProps) {
  const t = useT()
  const [capabilities, setCapabilities] = useState<CollaborationCapabilities | null>(null)
  const [tab, setTab] = useState<'comments' | 'members'>('comments')
  const [unavailable, setUnavailable] = useState(false)
  const blocked = useRef(false)
  const generation = useRef(0)
  const onUnavailable = useCallback(() => {
    generation.current += 1
    blocked.current = true; setCapabilities(null); setUnavailable(true); setTab('comments')
  }, [])
  const { run, busy, error } = useCollaborationRequest(onUnavailable)
  const refresh = useCallback(() => {
    const observed = generation.current
    return run(async (signal) => {
      const value = await getCollaborationCapabilities(project.id, signal)
      requireCollaborationScope(project.id, value)
      return value
    }, (value) => {
      if (observed === generation.current && !blocked.current) { setCapabilities(value); setUnavailable(false) }
    })
  }, [project.id, run])
  useEffect(() => {
    void refresh()
    const check = () => { if (!document.hidden && !blocked.current) void refresh() }
    const timer = window.setInterval(check, 15_000)
    window.addEventListener('focus', check)
    return () => { window.clearInterval(timer); window.removeEventListener('focus', check) }
  }, [refresh])
  return <div className="project-collaboration-body">
    {error && <p role="alert">{t(error)}</p>}
    {unavailable && <p role="alert">{t('Project collaboration access changed. Refresh after signing in with your current account and role.')}</p>}
    {!capabilities && <button type="button" disabled={busy} onClick={() => { blocked.current = false; void refresh() }}>{t(busy ? 'Loading collaboration…' : 'Retry collaboration')}</button>}
    {capabilities && <div key={`${capabilities.actor_id}:${capabilities.role}:${capabilities.can_comment}`}>
      <p><strong>{capabilities.identity_mode === 'local_only' ? t('Local operator') : capabilities.actor_id}</strong>{capabilities.role && ` · ${t(capabilities.role)}`}</p>
      {capabilities.identity_mode === 'local_only' && <p>{t('Comments for this local project stay on this computer. Shared team projects require organization sign-in.')}</p>}
      <div className="collaboration-actions" aria-label={t('Collaboration views')}>
        <button type="button" aria-pressed={tab === 'comments'} onClick={() => setTab('comments')}>{t('Project discussions')}</button>
        {capabilities.can_manage_members && <button type="button" aria-pressed={tab === 'members'} onClick={() => setTab('members')}>{t('Project members')}</button>}
      </div>
      {tab === 'members' && capabilities.can_manage_members
        ? <ProjectMembers capabilities={capabilities} onUnavailable={onUnavailable} />
        : <ProjectComments project={project} currentRun={currentRun} events={events} capabilities={capabilities} onUnavailable={onUnavailable} />}
    </div>}
  </div>
}

function ScopedPanel(props: PanelProps) {
  const t = useT()
  const id = useId()
  const [open, setOpen] = useState(false)
  return <section className="project-collaboration">
    <button type="button" className="project-collaboration-toggle" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
      <span>{t('Team and comments')}</span><span aria-hidden="true">{open ? '−' : '+'}</span>
    </button>
    {open && <div id={id}><Collaboration {...props} /></div>}
  </section>
}
export default function ProjectCollaborationPanel(props: PanelProps) {
  return <ScopedPanel key={props.project.id} {...props} />
}
