import { render, screen } from '@testing-library/react'
import { I18nContext } from '../i18n'
import type { Artifact, OphiraEvent, OpportunityProject } from '../api/types'
import ResourceEventLinks from './ResourceEventLinks'

const artifact = {
  id: 'art_original', project_id: 'project_1', run_id: 'run_1', name: 'report.md',
  path: 'report.md', version: 1,
} as Artifact
const project = {
  id: 'project_1', artifacts: [artifact, { ...artifact, id: 'art_latest', version: 2 }],
} as OpportunityProject
const event = {
  id: 'run_1:1', type: 'artifact.updated', project_id: 'project_1', run_id: 'run_1',
  data: { artifact_id: artifact.id, name: 'untrusted-name', download_url: 'https://untrusted.test' },
} as unknown as OphiraEvent

it('downloads the recorded immutable version using authorized project data', () => {
  render(<ResourceEventLinks event={event} project={project} />)
  expect(screen.getByRole('link', { name: 'Download report.md · version 1' }))
    .toHaveAttribute('href', '/api/projects/project_1/artifacts/art_original/download')
  expect(screen.queryByText('untrusted-name')).not.toBeInTheDocument()
})

it.each([
  { ...event, project_id: 'foreign' },
  { ...event, run_id: 'foreign' },
  { ...event, data: { artifact_id: 'missing' } },
])('never links artifacts outside this project or source Run', (value) => {
  render(<ResourceEventLinks event={value} project={project} />)
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
})

it('does not display memory text or a stale pending status from a replayed event', () => {
  render(<ResourceEventLinks event={{ ...event, type: 'memory.proposed', data: {
    memory_id: 'mem_1', text: 'Private preference', status: 'approved',
  } }} project={project} />)
  expect(screen.getByRole('link', { name: 'Review memories' }))
    .toHaveAttribute('href', '/app/settings#memory-review')
  expect(screen.getByText(/require your approval before future use/)).toBeVisible()
  expect(screen.queryByText('Private preference')).not.toBeInTheDocument()
  expect(screen.queryByText('approved')).not.toBeInTheDocument()
})

it('removes all resource links after project access becomes unavailable', () => {
  const view = render(<ResourceEventLinks event={event} project={project} />)
  expect(screen.getByRole('link')).toBeInTheDocument()
  view.rerender(<ResourceEventLinks event={event} project={null} />)
  expect(screen.queryByRole('link')).not.toBeInTheDocument()
})

it('translates saved version and memory review links', () => {
  const view = render(<I18nContext.Provider value="zh-CN">
    <ResourceEventLinks event={event} project={project} />
  </I18nContext.Provider>)
  expect(screen.getByRole('link', { name: '下载 report.md · 版本 1' })).toBeVisible()
  view.rerender(<I18nContext.Provider value="zh-CN">
    <ResourceEventLinks event={{ ...event, type: 'memory.proposed' }} project={project} />
  </I18nContext.Provider>)
  expect(screen.getByRole('link', { name: '审核记忆' })).toBeVisible()
})
