import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import type { SitePublicationCatalog, SitePublicationView } from '../api/sitePublications'
import type { Artifact } from '../api/types'
import SitePublicationsPanel from './SitePublicationsPanel'
import {
  getSitePublications, publishSitePublication, revokeSitePublication, stageSitePublication,
} from '../api/client'

vi.mock('../api/client', () => ({
  getSitePublications: vi.fn(),
  stageSitePublication: vi.fn(),
  publishSitePublication: vi.fn(),
  revokeSitePublication: vi.fn(),
  sitePublicationPreviewUrl: (projectId: string, siteId: string) =>
    `/api/projects/${projectId}/site-publications/${siteId}/preview`,
  errorMessage: (cause: unknown) => String(cause instanceof Error ? cause.message : cause),
}))

const artifact: Artifact = {
  id: 'artifact_' + 'a'.repeat(32), project_id: 'project_1',
  path: 'landing.html', name: 'landing.html', mime: 'text/html',
  version: 1, run_id: null, source_ids: [], checksum: 'b'.repeat(64),
  size_bytes: 200, download_url: '/api/artifacts/landing', content_storage: 'snapshot',
  created_at: '2026-09-24T00:00:00Z', updated_at: '2026-09-24T00:00:00Z',
}
const draft: SitePublicationView = {
  publication: {
    id: 'site_' + 'c'.repeat(32), project_id: 'project_1', artifact_id: artifact.id,
    artifact_name: artifact.name, artifact_checksum: artifact.checksum,
    size_bytes: artifact.size_bytes, origin: 'https://pages.example.com',
    state: 'draft', revision: 1, created_by: 'owner@example.com',
    created_at: '2026-09-24T00:00:00Z', published_by: null,
    published_at: null, revoked_by: null, revoked_at: null,
  },
  public_url: 'https://pages.example.com/published/project_1/site_' + 'c'.repeat(32),
}
const published: SitePublicationView = {
  ...draft,
  publication: { ...draft.publication, state: 'published', revision: 2,
    published_by: 'owner@example.com', published_at: '2026-09-24T00:01:00Z' },
}
const revoked: SitePublicationView = {
  ...published,
  publication: { ...published.publication, state: 'revoked', revision: 3,
    revoked_by: 'owner@example.com', revoked_at: '2026-09-24T00:02:00Z' },
}
const initial: SitePublicationCatalog = {
  public_origin: 'https://pages.example.com', can_stage: true,
  can_publish: true, publications: [],
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getSitePublications).mockResolvedValue(initial)
  vi.mocked(stageSitePublication).mockResolvedValue(draft)
  vi.mocked(publishSitePublication).mockResolvedValue(published)
  vi.mocked(revokeSitePublication).mockResolvedValue(revoked)
})

it('stages a pinned artifact, requires human review, then publishes and withdraws it', async () => {
  const { container } = render(<SitePublicationsPanel projectId="project_1" artifacts={[artifact]} />)
  fireEvent.click(screen.getByRole('button', { name: 'Published pages · 0' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Stage this HTML version' }))
  await waitFor(() => expect(stageSitePublication).toHaveBeenCalledWith('project_1', artifact.id))
  const approve = await screen.findByRole('button', { name: 'Approve and publish page' })
  expect(approve).toBeDisabled()
  expect(container.querySelector('iframe')).toHaveAttribute(
    'src', `/api/projects/project_1/site-publications/${draft.publication.id}/preview`,
  )
  expect(container.querySelector('iframe')).toHaveAttribute('sandbox', '')
  fireEvent.click(screen.getByRole('checkbox', {
    name: 'I reviewed this exact HTML version and approve making it public.',
  }))
  fireEvent.click(approve)
  await waitFor(() => expect(publishSitePublication).toHaveBeenCalledWith('project_1', draft))
  expect(await screen.findByRole('link', { name: 'Open public page' })).toHaveAttribute(
    'href', published.public_url,
  )
  fireEvent.click(screen.getByRole('button', { name: 'Withdraw page' }))
  expect(revokeSitePublication).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Confirm withdrawal' }))
  await waitFor(() => expect(revokeSitePublication).toHaveBeenCalledWith('project_1', published))
  expect(await screen.findByText('This page is no longer public.')).toBeInTheDocument()
})

it('does not offer staging when no public site origin is configured', async () => {
  vi.mocked(getSitePublications).mockResolvedValue({
    public_origin: null, can_stage: false, can_publish: false, publications: [],
  })
  render(<SitePublicationsPanel projectId="project_1" artifacts={[artifact]} />)
  fireEvent.click(screen.getByRole('button', { name: 'Published pages · 0' }))
  expect(await screen.findByText('Configure a separate public HTTPS site origin to publish pages.')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Stage this HTML version' })).not.toBeInTheDocument()
})
