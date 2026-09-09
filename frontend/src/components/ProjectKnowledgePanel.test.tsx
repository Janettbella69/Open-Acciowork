import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { KnowledgeSearch } from '../api/knowledge'
import { I18nContext } from '../i18n'
import ProjectKnowledgePanel from './ProjectKnowledgePanel'

vi.mock('../api/client', () => ({
  apiBaseUrl: '', getProjectKnowledge: vi.fn(),
  errorMessage: (error: unknown) => String(error instanceof Error ? error.message : error),
}))
import { getProjectKnowledge } from '../api/client'

function fixture(projectId = 'project_1'): KnowledgeSearch {
  return {
    project_id: projectId, query: 'quote', observed_at: '2026-09-09T10:00:00Z',
    documents: [{ artifact_id: 'artifact_1', name: 'quotes.csv', path: 'uploads/quotes.csv', mime: 'text/csv', version: 2, checksum: 'a'.repeat(64), run_id: 'run_1', source_ids: ['source_1'], created_at: '2026-09-09T09:00:00Z', status: 'indexed' }],
    matches: [{ artifact_id: 'artifact_1', excerpt: 'Supplier,price\nAcme,120', start_line: 2, end_line: 3, start_column: 1, excerpt_truncated: true }],
    total_documents: 1, indexed_documents: 1, omitted_documents: 0,
    matching_documents: 1, bytes_read: 25, reference_only: true,
  }
}

function open() { fireEvent.click(screen.getByRole('button', { name: 'Project knowledge' })) }
function search(value: string) {
  fireEvent.change(screen.getByRole('searchbox', { name: 'Search references' }), { target: { value } })
  fireEvent.click(screen.getByRole('button', { name: 'Search' }))
}

beforeEach(() => { vi.clearAllMocks(); vi.mocked(getProjectKnowledge).mockResolvedValue(fixture()) })

describe('Project knowledge', () => {
  it('loads on demand and displays line provenance and scoped downloads', async () => {
    render(<ProjectKnowledgePanel projectId="project_1" />)
    expect(getProjectKnowledge).not.toHaveBeenCalled()
    open()
    await screen.findByText('Supplier,price Acme,120')
    expect(screen.getByText('Lines 2–3 · column 1')).toBeInTheDocument()
    expect(screen.getByText('a'.repeat(64))).toBeInTheDocument()
    for (const link of screen.getAllByRole('link', { name: 'Download' })) {
      expect(link).toHaveAttribute('href', '/api/projects/project_1/knowledge/artifact_1/download')
    }
    search('quote')
    await waitFor(() => expect(getProjectKnowledge).toHaveBeenLastCalledWith('project_1', 'quote', expect.any(AbortSignal)))
  })

  it('shows coverage gaps without treating no matches as absence', async () => {
    const data = fixture()
    data.matches = []; data.matching_documents = 0; data.indexed_documents = 0
    data.omitted_documents = 2; data.total_documents = 3
    data.documents[0].status = 'changed'
    vi.mocked(getProjectKnowledge).mockResolvedValue(data)
    render(<ProjectKnowledgePanel projectId="project_1" />)
    open(); search('quote')
    await screen.findByText('File differs from registered version')
    expect(screen.getByText('2 older files are outside this search limit.')).toBeInTheDocument()
    expect(screen.getByText(/No matching passages in the searchable files/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Download' })).not.toBeInTheDocument()
  })

  it('drops late results from an older search', async () => {
    let resolveOld!: (data: KnowledgeSearch) => void
    vi.mocked(getProjectKnowledge).mockImplementationOnce(() => new Promise((resolve) => { resolveOld = resolve }))
    render(<ProjectKnowledgePanel projectId="project_1" />)
    open(); search('quote')
    await screen.findByText('Supplier,price Acme,120')
    const obsolete = fixture(); obsolete.matches[0].excerpt = 'obsolete-private-text'
    await act(async () => resolveOld(obsolete))
    expect(screen.queryByText('obsolete-private-text')).not.toBeInTheDocument()
  })

  it('unmounts private results when switching project and rejects wrong scope', async () => {
    const { rerender } = render(<ProjectKnowledgePanel projectId="project_1" />)
    open(); await screen.findByText('Supplier,price Acme,120')
    rerender(<ProjectKnowledgePanel projectId="project_2" />)
    expect(screen.queryByText('Supplier,price Acme,120')).not.toBeInTheDocument()
    open()
    await screen.findByRole('alert')
    expect(screen.getByRole('alert')).toHaveTextContent('Reference response did not match this project.')
  })

  it('refreshes after artifact revisions and recovers from failed requests', async () => {
    vi.mocked(getProjectKnowledge).mockRejectedValueOnce(new Error('temporarily unavailable'))
    const { rerender } = render(<ProjectKnowledgePanel projectId="project_1" revision="a" />)
    open(); await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh references' }))
    await screen.findByText('Supplier,price Acme,120')
    const refreshed = fixture(); refreshed.matches[0].excerpt = 'updated quote'
    vi.mocked(getProjectKnowledge).mockResolvedValue(refreshed)
    rerender(<ProjectKnowledgePanel projectId="project_1" revision="b" />)
    await screen.findByText('updated quote')
    expect(screen.queryByText('Supplier,price Acme,120')).not.toBeInTheDocument()
  })

  it('renders hostile content as text and ignores matches without current documents', async () => {
    const data = fixture(); data.matches[0].excerpt = '<script>alert(1)</script>'
    data.matches.push({ ...data.matches[0], artifact_id: 'foreign', excerpt: 'foreign result' })
    vi.mocked(getProjectKnowledge).mockResolvedValue(data)
    const { container } = render(<ProjectKnowledgePanel projectId="project_1" />)
    open(); await screen.findByText('<script>alert(1)</script>')
    expect(container.querySelector('script')).toBeNull()
    expect(screen.queryByText('foreign result')).not.toBeInTheDocument()
  })

  it('keeps extraction warnings visible beside a distant matching excerpt', async () => {
    const data = fixture(); data.documents[0].extraction_notice = 'OCR incomplete: missing page 2'
    vi.mocked(getProjectKnowledge).mockResolvedValue(data)
    render(<ProjectKnowledgePanel projectId="project_1" />)
    open(); await screen.findByText('OCR incomplete: missing page 2')
    expect(screen.getByText('Extraction notes — check before citing')).toBeInTheDocument()
  })

  it('provides Chinese labels and guidance for an empty project', async () => {
    const data = fixture(); data.documents = []; data.matches = []
    data.total_documents = 0; data.indexed_documents = 0; data.matching_documents = 0
    vi.mocked(getProjectKnowledge).mockResolvedValue(data)
    render(<I18nContext.Provider value="zh-CN"><ProjectKnowledgePanel projectId="project_1" /></I18nContext.Provider>)
    fireEvent.click(screen.getByRole('button', { name: '项目知识库' }))
    await screen.findByText('在项目消息中添加参考文件，即可在此检索。')
    expect(within(screen.getByRole('button', { name: '检索' })).queryByText('Search')).toBeNull()
  })
})
