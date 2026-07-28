/** TanStack Query hooks, one group per resource, mirroring `lib/api/endpoints.ts`.
 * Query keys are centralized here so invalidation after a mutation (optimistic
 * updates + toasts for the human-action routes) stays consistent. */

import { useMutation, useQuery, useQueryClient, type QueryClient, type QueryKey } from '@tanstack/react-query'
import { toast } from 'sonner'
import {
  artifactsApi,
  directionApi,
  gapsApi,
  phasesApi,
  pipelineApi,
  projectsApi,
  reportApi,
  timelineApi,
} from '@/lib/api/endpoints'
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
  ProjectCreate,
} from '@/lib/api/types'

function isPage(value: unknown): value is Page<{ id: string }> {
  return !!value && typeof value === 'object' && Array.isArray((value as Page<unknown>).items)
}

/** Optimistically rewrites one item (matched by id) across every cached
 * `Page<T>` query whose key starts with `prefixKey` — human-action mutations
 * (confirm/override direction, dismiss/resolve gap, rename) show their
 * effect immediately rather than waiting on the round-trip. `prefixKey` may
 * also match non-`Page` queries (e.g. an artifact detail cached under the
 * same prefix as the artifact list); those are left untouched via `isPage`.
 * Returns a snapshot of every touched key for `onError` rollback. */
function optimisticPageItemUpdate<T extends { id: string }>(
  qc: QueryClient,
  prefixKey: QueryKey,
  itemId: string,
  updater: (item: T) => T,
) {
  const snapshot = qc.getQueriesData({ queryKey: prefixKey })
  qc.setQueriesData({ queryKey: prefixKey }, (data: unknown) => {
    if (!isPage(data)) return data
    return { ...data, items: data.items.map((item) => (item.id === itemId ? updater(item as T) : item)) }
  })
  return snapshot
}

function rollback(qc: QueryClient, snapshot: [QueryKey, unknown][]) {
  for (const [key, data] of snapshot) qc.setQueryData(key, data)
}

function isDirectionOverview(value: unknown): value is DirectionOverviewDTO {
  return !!value && typeof value === 'object' && isPage((value as DirectionOverviewDTO).labels)
}

/** Direction's cache shape nests its `Page` under `.labels`, so it needs its
 * own optimistic updater rather than the flat `optimisticPageItemUpdate`. */
function optimisticDirectionUpdate(
  qc: QueryClient,
  projectId: string,
  artifactId: string,
  updater: (label: DirectionLabelDTO) => DirectionLabelDTO,
) {
  const prefixKey: QueryKey = ['projects', projectId, 'direction']
  const snapshot = qc.getQueriesData({ queryKey: prefixKey })
  qc.setQueriesData({ queryKey: prefixKey }, (data: unknown) => {
    if (!isDirectionOverview(data)) return data
    return {
      ...data,
      labels: {
        ...data.labels,
        items: data.labels.items.map((item) => (item.artifact_id === artifactId ? updater(item) : item)),
      },
    }
  })
  return snapshot
}

export const qk = {
  projects: ['projects'] as const,
  project: (id: string) => ['projects', id] as const,
  status: (id: string) => ['projects', id, 'status'] as const,
  artifacts: (id: string, params: Record<string, unknown>) => ['projects', id, 'artifacts', params] as const,
  artifact: (id: string, artifactId: string) => ['projects', id, 'artifacts', artifactId] as const,
  timeline: (id: string, params: Record<string, unknown>) => ['projects', id, 'timeline', params] as const,
  direction: (id: string, params: Record<string, unknown>) => ['projects', id, 'direction', params] as const,
  gaps: (id: string, params: Record<string, unknown>) => ['projects', id, 'gaps', params] as const,
  phases: (id: string) => ['projects', id, 'phases'] as const,
  report: (id: string) => ['projects', id, 'report'] as const,
}

// --------------------------------------------------------------------------
// Projects
// --------------------------------------------------------------------------
export function useProjects() {
  return useQuery({ queryKey: qk.projects, queryFn: projectsApi.list })
}

export function useProject(projectId: string | undefined) {
  return useQuery({
    queryKey: qk.project(projectId ?? ''),
    queryFn: () => projectsApi.get(projectId!),
    enabled: !!projectId,
  })
}

export function useCreateProject() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: ProjectCreate) => projectsApi.create(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.projects })
    },
  })
}

export function useDeleteProject() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (projectId: string) => projectsApi.remove(projectId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.projects })
      toast.success('Project deleted')
    },
    onError: (err: Error) => {
      toast.error('Could not delete project', { description: err.message })
    },
  })
}

// --------------------------------------------------------------------------
// Pipeline
// --------------------------------------------------------------------------
export function useProjectStatus(projectId: string | undefined, opts: { poll?: boolean } = {}) {
  return useQuery({
    queryKey: qk.status(projectId ?? ''),
    queryFn: () => pipelineApi.status(projectId!),
    enabled: !!projectId,
    refetchInterval: (query) => {
      if (!opts.poll) return false
      const state = query.state.data?.state
      return state === 'running' ? 1500 : false
    },
  })
}

export function useUploadFiles(projectId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (files: File[]) => pipelineApi.uploadFiles(projectId, files),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.status(projectId) })
    },
  })
}

export function useRunPipeline(projectId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => pipelineApi.run(projectId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.status(projectId) })
    },
  })
}

// --------------------------------------------------------------------------
// Artifacts
// --------------------------------------------------------------------------
export function useArtifacts(
  projectId: string,
  params: { limit?: number; offset?: number; phase_id?: string; direction?: DirectionLabelValue },
) {
  return useQuery({
    queryKey: qk.artifacts(projectId, params),
    queryFn: () => artifactsApi.list(projectId, params),
  })
}

export function useArtifact(projectId: string, artifactId: string | undefined) {
  return useQuery({
    queryKey: qk.artifact(projectId, artifactId ?? ''),
    queryFn: () => artifactsApi.get(projectId, artifactId!),
    enabled: !!artifactId,
  })
}

export function useOverrideArtifactName(projectId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ artifactId, body }: { artifactId: string; body: ArtifactNamePutRequest }) =>
      artifactsApi.overrideName(projectId, artifactId, body),
    onMutate: async ({ artifactId, body }) => {
      const listPrefix: QueryKey = ['projects', projectId, 'artifacts']
      await qc.cancelQueries({ queryKey: listPrefix })
      const listSnapshot = optimisticPageItemUpdate<ArtifactSummaryDTO>(qc, listPrefix, artifactId, (item) =>
        item.view ? { ...item, view: { ...item.view, suggested_name: body.suggested_name } } : item,
      )
      const detailKey = qk.artifact(projectId, artifactId)
      const previousDetail = qc.getQueryData<ArtifactDetailDTO>(detailKey)
      if (previousDetail?.view) {
        qc.setQueryData<ArtifactDetailDTO>(detailKey, {
          ...previousDetail,
          view: { ...previousDetail.view, suggested_name: body.suggested_name },
        })
      }
      return { listSnapshot, detailKey, previousDetail }
    },
    onError: (err: Error, _vars, context) => {
      if (context) {
        rollback(qc, context.listSnapshot)
        qc.setQueryData(context.detailKey, context.previousDetail)
      }
      toast.error('Could not update name', { description: err.message })
    },
    onSuccess: () => {
      toast.success('Name updated', { description: 'The suggested name has been overridden.' })
    },
    onSettled: (_data, _err, { artifactId }) => {
      qc.invalidateQueries({ queryKey: ['projects', projectId, 'artifacts'] })
      qc.invalidateQueries({ queryKey: qk.artifact(projectId, artifactId) })
    },
  })
}

// --------------------------------------------------------------------------
// Timeline
// --------------------------------------------------------------------------
export function useTimeline(projectId: string, params: { limit?: number; offset?: number }) {
  return useQuery({
    queryKey: qk.timeline(projectId, params),
    queryFn: () => timelineApi.list(projectId, params),
  })
}

// --------------------------------------------------------------------------
// Direction
// --------------------------------------------------------------------------
export function useDirection(projectId: string, params: { limit?: number; offset?: number } = {}) {
  return useQuery({
    queryKey: qk.direction(projectId, params),
    queryFn: () => directionApi.get(projectId, params),
  })
}

export function usePatchDirectionLabel(projectId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ artifactId, body }: { artifactId: string; body: DirectionPatchRequest }) =>
      directionApi.patch(projectId, artifactId, body),
    onMutate: async ({ artifactId, body }) => {
      const prefixKey: QueryKey = ['projects', projectId, 'direction']
      await qc.cancelQueries({ queryKey: prefixKey })
      const snapshot = optimisticDirectionUpdate(qc, projectId, artifactId, (label) => ({
        ...label,
        label: body.label ?? label.label,
        confirmed_by_user: true,
      }))
      return { snapshot }
    },
    onError: (err: Error, _vars, context) => {
      if (context) rollback(qc, context.snapshot)
      toast.error('Could not update direction', { description: err.message })
    },
    onSuccess: (data) => {
      toast.success(data.label ? `Marked as ${data.label}` : 'Direction confirmed', {
        description: data.artifact_name,
      })
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['projects', projectId, 'direction'] })
      qc.invalidateQueries({ queryKey: ['projects', projectId, 'artifacts'] })
    },
  })
}

// --------------------------------------------------------------------------
// Gaps
// --------------------------------------------------------------------------
export function useGaps(
  projectId: string,
  params: { limit?: number; offset?: number; status?: GapStatus; type?: GapType } = {},
) {
  return useQuery({
    queryKey: qk.gaps(projectId, params),
    queryFn: () => gapsApi.list(projectId, params),
  })
}

export function usePatchGap(projectId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ gapId, body }: { gapId: string; body: GapPatchRequest }) =>
      gapsApi.patch(projectId, gapId, body),
    onMutate: async ({ gapId, body }) => {
      const prefixKey: QueryKey = ['projects', projectId, 'gaps']
      await qc.cancelQueries({ queryKey: prefixKey })
      const snapshot = optimisticPageItemUpdate<GapDTO>(qc, prefixKey, gapId, (gap) => ({
        ...gap,
        status: body.status,
      }))
      return { snapshot }
    },
    onError: (err: Error, _vars, context) => {
      if (context) rollback(qc, context.snapshot)
      toast.error('Could not update gap', { description: err.message })
    },
    onSuccess: (data) => {
      toast.success(`Gap ${data.status}`, { description: data.description.slice(0, 80) })
    },
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ['projects', projectId, 'gaps'] })
    },
  })
}

// --------------------------------------------------------------------------
// Phases (used for the artifact-browser phase filter)
// --------------------------------------------------------------------------
export function usePhases(projectId: string) {
  return useQuery({
    queryKey: qk.phases(projectId),
    queryFn: () => phasesApi.get(projectId, { limit: 1 }),
  })
}

// --------------------------------------------------------------------------
// Report
// --------------------------------------------------------------------------
export function useReport(projectId: string) {
  return useQuery({ queryKey: qk.report(projectId), queryFn: () => reportApi.get(projectId) })
}
