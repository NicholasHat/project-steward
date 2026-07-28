/**
 * TypeScript mirror of `truth_engine/api/schemas.py` + the enums it imports
 * from `db/models.py`. Field names/nullability/enum string values are kept
 * in lockstep with the Python source — this is the contract layer, so when
 * the backend DTO shape changes, this file (and only this file) should need
 * to change before anything else does.
 */

// --------------------------------------------------------------------------
// Enums (db/models.py)
// --------------------------------------------------------------------------
export type ProcessingState =
  | 'pending'
  | 'parsed'
  | 'extracted'
  | 'embedded'
  | 'analyzed'
  | 'unsupported'
  | 'error'
export type DateSignalSource = 'content' | 'doc_meta' | 'filesystem'
export type EntityType = 'person' | 'group' | 'tool' | 'cost' | 'experiment' | 'hypothesis' | 'citation'
export type AssignmentSource = 'auto' | 'human'
export type DirectionLabelValue = 'current' | 'superseded' | 'unclear'
export type GapType = 'structural' | 'promised_unfulfilled'
export type GapStatus = 'open' | 'confirmed' | 'dismissed' | 'resolved'
export type Stage =
  | 'ingest'
  | 'parse'
  | 'extract'
  | 'embed'
  | 'timeline'
  | 'phases'
  | 'graph'
  | 'direction'
  | 'gaps'
  | 'view'
  | 'report'
export type PipelineRunStatus = 'idle' | 'running' | 'done' | 'error'

// --------------------------------------------------------------------------
// Shared envelope
// --------------------------------------------------------------------------
export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

// --------------------------------------------------------------------------
// Projects
// --------------------------------------------------------------------------
export interface ProjectCreate {
  name: string
  root_path: string
}

export interface ProjectDTO {
  id: string
  name: string
  root_path: string
  created_at: string
}

// --------------------------------------------------------------------------
// Shared sub-DTOs
// --------------------------------------------------------------------------
export interface ViewProjectionDTO {
  id: string
  suggested_name: string | null
  suggested_category: string | null
  virtual_path: string | null
  version: number
  source: AssignmentSource
  created_at: string
}

export interface PhaseAssignmentDTO {
  id: string
  artifact_id: string
  phase_id: string
  phase_name: string
  confidence: number
  rationale: string | null
  source: AssignmentSource
}

export interface DirectionLabelDTO {
  id: string
  artifact_id: string
  artifact_name: string
  label: DirectionLabelValue
  rationale: string | null
  signal_a_score: number | null
  signal_b_score: number | null
  confidence: number
  confirmed_by_user: boolean
  created_at: string
}

export interface ResolvedDateDTO {
  id: string
  candidate_date: string
  signal_source: DateSignalSource
  confidence: number
  evidence_text: string | null
  extractor: string
  is_chosen: boolean
}

// --------------------------------------------------------------------------
// Artifacts
// --------------------------------------------------------------------------
export interface ArtifactSummaryDTO {
  id: string
  original_filename: string
  file_type: string
  processing_state: ProcessingState
  chosen_date: string | null
  chosen_date_confidence: number | null
  view: ViewProjectionDTO | null
  direction: DirectionLabelDTO | null
  phases: PhaseAssignmentDTO[]
}

export interface EntityMentionDTO {
  entity_id: string
  type: EntityType
  value: string
  context: string | null
  confidence: number
  extractor: string
}

export interface RelationshipEdgeDTO {
  id: string
  direction: 'incoming' | 'outgoing'
  other_artifact_id: string
  other_artifact_name: string
  type: string
  confidence: number
  evidence: string | null
}

export interface ArtifactDetailDTO extends ArtifactSummaryDTO {
  current_path: string
  size_bytes: number
  fs_created: string | null
  fs_modified: string | null
  ingested_at: string
  parser_name: string | null
  parser_version: string | null
  structure: Record<string, unknown> | null
  embedded_metadata: Record<string, unknown> | null
  entities: EntityMentionDTO[]
  resolved_dates: ResolvedDateDTO[]
  edges: RelationshipEdgeDTO[]
}

export interface ArtifactNamePutRequest {
  suggested_name: string
}

// --------------------------------------------------------------------------
// Timeline
// --------------------------------------------------------------------------
export interface TimelineEventDTO {
  id: string
  artifact_id: string | null
  artifact_name: string | null
  event_date: string
  description: string
  confidence: number
  source: string
}

// --------------------------------------------------------------------------
// Direction
// --------------------------------------------------------------------------
export interface DirectionSnapshotDTO {
  id: string
  inferred_direction_summary: string
  computed_at: string
}

export interface DirectionOverviewDTO {
  snapshot: DirectionSnapshotDTO | null
  labels: Page<DirectionLabelDTO>
}

export interface DirectionPatchRequest {
  label?: DirectionLabelValue | null
}

// --------------------------------------------------------------------------
// Gaps
// --------------------------------------------------------------------------
export interface GapDTO {
  id: string
  type: GapType
  phase_id: string | null
  phase_name: string | null
  description: string
  evidence: string | null
  confidence: number
  status: GapStatus
}

export interface GapPatchRequest {
  status: GapStatus
}

// --------------------------------------------------------------------------
// Phases / domain
// --------------------------------------------------------------------------
export interface DomainClassificationDTO {
  id: string
  domain: string
  confidence: number
  model: string | null
  confirmed_by_user: boolean
  created_at: string
}

export interface PhaseTemplateCoverageDTO {
  id: string
  phase_name: string
  ordinal: number
  description: string | null
  artifact_count: number
}

export interface PhasesOverviewDTO {
  domain_classification: DomainClassificationDTO | null
  template_domain: string | null
  phases: PhaseTemplateCoverageDTO[]
  assignments: Page<PhaseAssignmentDTO>
}

export interface DomainPatchRequest {
  domain?: string | null
}

// --------------------------------------------------------------------------
// Pipeline: upload / run / status
// --------------------------------------------------------------------------
export interface UploadedFileDTO {
  filename: string
  size_bytes: number
}

export interface UploadResponse {
  root_path: string
  files: UploadedFileDTO[]
}

export interface RunResponse {
  run_id: string
  status: PipelineRunStatus
}

export interface StageProgressDTO {
  stage: Stage
  total: number
  done: number
  error: number
  pending: number
  skipped: number
}

export interface ProjectStatusDTO {
  state: PipelineRunStatus
  run_id: string | null
  current_stage: Stage | null
  error: string | null
  started_at: string | null
  finished_at: string | null
  stages: StageProgressDTO[]
}

// --------------------------------------------------------------------------
// Report
// --------------------------------------------------------------------------
export interface ReportDTO {
  id: string
  version: number
  content: string
  sections: Record<string, unknown>
  generated_at: string
}

export interface ReportResponse {
  report: ReportDTO | null
}

// --------------------------------------------------------------------------
// Auth (fastapi-users)
// --------------------------------------------------------------------------
export interface UserRead {
  id: string
  email: string
  is_active: boolean
  is_superuser: boolean
  is_verified: boolean
}

export interface UserCreate {
  email: string
  password: string
}

export interface TokenResponse {
  access_token: string
  token_type: string
}
