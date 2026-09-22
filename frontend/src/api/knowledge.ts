export type ReferenceStatus = 'indexed' | 'unsupported' | 'unavailable' | 'changed' | 'too_large' | 'invalid_text' | 'invalid_document' | 'budget_exceeded'

export interface ReferenceDocument {
  artifact_id: string
  name: string
  path: string
  mime: string
  version: number
  checksum: string
  run_id: string | null
  source_ids: string[]
  created_at: string
  status: ReferenceStatus
  extraction_notice?: string | null
}

export interface ReferenceMatch {
  artifact_id: string
  excerpt: string
  start_line: number
  end_line: number
  start_column: number
  excerpt_truncated: boolean
  source_location?: {
    kind: 'paragraph' | 'cell' | 'slide' | 'slide_notes'
    paragraph?: number | null
    sheet?: string | null
    cell?: string | null
    slide?: number | null
  } | null
}

export interface KnowledgeSearch {
  project_id: string
  query: string
  observed_at: string
  documents: ReferenceDocument[]
  matches: ReferenceMatch[]
  total_documents: number
  indexed_documents: number
  omitted_documents: number
  matching_documents: number
  bytes_read: number
  office_bytes_reserved?: number
  reference_only: boolean
}
