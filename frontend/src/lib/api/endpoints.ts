/** One function per backend route, thinly wrapping `apiRequest` — mirrors
 * `api/routers/*.py` 1:1 so a route rename here should map obviously back
 * to its Python source. */

import { apiRequest } from './client'
import type {
  ArtifactDetailDTO,
  ArtifactNamePutRequest,
  ArtifactSummaryDTO,
  DirectionLabelDTO,
  DirectionLabelValue,
  DirectionOverviewDTO,
  DirectionPatchRequest,
  GapDTO,
  GapPatchRequest,
  GapStatus,
  GapType,
  Page,
  PhasesOverviewDTO,
  ProjectCreate,
  ProjectDTO,
  ProjectStatusDTO,
  ReportResponse,
  RunResponse,
  TimelineEventDTO,
  TokenResponse,
  UploadResponse,
  UserCreate,
  UserRead,
  ViewProjectionDTO,
} from './types'

// --------------------------------------------------------------------------
// Auth
// --------------------------------------------------------------------------
export const authApi = {
  register: (body: UserCreate) =>
    apiRequest<UserRead>('/auth/register', { method: 'POST', body, skipAuth: true }),

  login: (email: string, password: string) =>
    // fastapi-users' JWT login route expects OAuth2 form-encoded body
    // (`username` + `password`, per the OAuth2PasswordRequestForm spec).
    apiRequest<TokenResponse>('/auth/jwt/login', {
      method: 'POST',
      form: { username: email, password },
      skipAuth: true,
    }),

  me: () => apiRequest<{ id: string; email: string }>('/me'),
}

// --------------------------------------------------------------------------
// Projects
// --------------------------------------------------------------------------
export const projectsApi = {
  list: () => apiRequest<ProjectDTO[]>('/projects'),
  create: (body: ProjectCreate) => apiRequest<ProjectDTO>('/projects', { method: 'POST', body }),
  get: (projectId: string) => apiRequest<ProjectDTO>(`/projects/${projectId}`),
  remove: (projectId: string) => apiRequest<void>(`/projects/${projectId}`, { method: 'DELETE' }),
}

// --------------------------------------------------------------------------
// Pipeline: upload / run / status
// --------------------------------------------------------------------------
export const pipelineApi = {
  uploadFiles: (projectId: string, files: File[]) =>
    apiRequest<UploadResponse>(`/projects/${projectId}/files`, { method: 'POST', files }),
  run: (projectId: string) =>
    apiRequest<RunResponse>(`/projects/${projectId}/run`, { method: 'POST' }),
  status: (projectId: string) => apiRequest<ProjectStatusDTO>(`/projects/${projectId}/status`),
}

// --------------------------------------------------------------------------
// Artifacts
// --------------------------------------------------------------------------
export const artifactsApi = {
  list: (
    projectId: string,
    params: { limit?: number; offset?: number; phase_id?: string; direction?: DirectionLabelValue } = {},
  ) =>
    apiRequest<Page<ArtifactSummaryDTO>>(`/projects/${projectId}/artifacts`, { query: params }),
  get: (projectId: string, artifactId: string) =>
    apiRequest<ArtifactDetailDTO>(`/projects/${projectId}/artifacts/${artifactId}`),
  overrideName: (projectId: string, artifactId: string, body: ArtifactNamePutRequest) =>
    apiRequest<ViewProjectionDTO>(`/projects/${projectId}/artifacts/${artifactId}/name`, {
      method: 'PUT',
      body,
    }),
}

// --------------------------------------------------------------------------
// Timeline
// --------------------------------------------------------------------------
export const timelineApi = {
  list: (projectId: string, params: { limit?: number; offset?: number } = {}) =>
    apiRequest<Page<TimelineEventDTO>>(`/projects/${projectId}/timeline`, { query: params }),
}

// --------------------------------------------------------------------------
// Direction
// --------------------------------------------------------------------------
export const directionApi = {
  get: (projectId: string, params: { limit?: number; offset?: number } = {}) =>
    apiRequest<DirectionOverviewDTO>(`/projects/${projectId}/direction`, { query: params }),
  patch: (projectId: string, artifactId: string, body: DirectionPatchRequest) =>
    apiRequest<DirectionLabelDTO>(`/projects/${projectId}/direction/${artifactId}`, {
      method: 'PATCH',
      body,
    }),
}

// --------------------------------------------------------------------------
// Gaps
// --------------------------------------------------------------------------
export const gapsApi = {
  list: (
    projectId: string,
    params: { limit?: number; offset?: number; status?: GapStatus; type?: GapType } = {},
  ) => apiRequest<Page<GapDTO>>(`/projects/${projectId}/gaps`, { query: params }),
  patch: (projectId: string, gapId: string, body: GapPatchRequest) =>
    apiRequest<GapDTO>(`/projects/${projectId}/gaps/${gapId}`, { method: 'PATCH', body }),
}

// --------------------------------------------------------------------------
// Phases / domain
// --------------------------------------------------------------------------
export const phasesApi = {
  get: (projectId: string, params: { limit?: number; offset?: number } = {}) =>
    apiRequest<PhasesOverviewDTO>(`/projects/${projectId}/phases`, { query: params }),
}

// --------------------------------------------------------------------------
// Report
// --------------------------------------------------------------------------
export const reportApi = {
  get: (projectId: string) => apiRequest<ReportResponse>(`/projects/${projectId}/report`),
}
