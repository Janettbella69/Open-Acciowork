import type { Artifact } from './types'

export type ArtifactTextView = {
  artifact: Artifact
  content: string
  latest_artifact_id: string
  editable: boolean
  edit_notice: string | null
}

export type ArtifactTextUpdate = {
  content: string
  expected_checksum: string
  request_id: string
}
