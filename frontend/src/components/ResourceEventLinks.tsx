import { artifactDownloadUrl } from '../api/client'
import { RESOURCE_EVENT, type OphiraEvent, type OpportunityProject } from '../api/types'
import { useT } from '../i18n'

export default function ResourceEventLinks({ event, project }: {
  event: OphiraEvent; project: OpportunityProject | null
}) {
  const t = useT()
  if (!project || event.project_id !== project.id) return null
  if (event.type === RESOURCE_EVENT.MEMORY_PROPOSED) return <p>
    {t('Memory proposals require your approval before future use.')} {' '}
    <a href="/app/settings#memory-review">{t('Review memories')}</a>
  </p>
  if (event.type !== RESOURCE_EVENT.ARTIFACT_REGISTERED && event.type !== RESOURCE_EVENT.ARTIFACT_UPDATED) return null
  const id = event.data.artifact_id ?? event.data.id
  // Resolve from the current authorized project, never from a URL or filename
  // supplied in an event. Each Artifact ID identifies one immutable version.
  const artifact = project.artifacts.find((item) => item.id === id
    && item.project_id === project.id && item.run_id === event.run_id)
  if (!artifact) return <p>{t('Refresh the project to view this saved artifact.')}</p>
  return <p><a href={artifactDownloadUrl(project.id, artifact.id)}>
    {t('Download {name} · version {version}', { name: artifact.name, version: artifact.version })}
  </a></p>
}
