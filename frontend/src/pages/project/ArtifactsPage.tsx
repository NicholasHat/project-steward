import * as React from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { motion } from 'framer-motion'
import { ChevronLeft, ChevronRight, FolderOpen } from 'lucide-react'
import { PageTransition } from '@/components/layout/PageTransition'
import { QueryErrorState } from '@/components/layout/QueryErrorState'
import { ArtifactDetailSheet } from '@/components/project/ArtifactDetailSheet'
import { Card } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ConfidenceMeter } from '@/components/confidence/ConfidenceMeter'
import { DirectionBadge } from '@/components/confidence/DirectionBadge'
import { useArtifacts, usePhases } from '@/hooks/queries'
import { displayName } from '@/lib/artifactName'
import { formatDate } from '@/lib/utils'
import type { DirectionLabelValue } from '@/lib/api/types'

const PAGE_SIZE = 20

export function ArtifactsPage() {
  const { projectId, artifactId } = useParams<{ projectId: string; artifactId?: string }>()
  const navigate = useNavigate()

  const [offset, setOffset] = React.useState(0)
  const [phaseId, setPhaseId] = React.useState<string | undefined>(undefined)
  const [direction, setDirection] = React.useState<DirectionLabelValue | undefined>(undefined)

  const { data: phasesData } = usePhases(projectId!)
  const { data, isLoading, isError, error, refetch } = useArtifacts(projectId!, {
    limit: PAGE_SIZE,
    offset,
    phase_id: phaseId,
    direction,
  })

  const total = data?.total ?? 0
  const page = Math.floor(offset / PAGE_SIZE) + 1
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE))

  return (
    <PageTransition>
      <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">Artifacts</h1>
          <p className="mt-1 text-sm text-zinc-500">
            Every file, organized — clean names and structure, raw files untouched.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Select
            value={direction ?? 'all'}
            onValueChange={(v) => {
              setDirection(v === 'all' ? undefined : (v as DirectionLabelValue))
              setOffset(0)
            }}
          >
            <SelectTrigger className="w-40">
              <SelectValue placeholder="All directions" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All directions</SelectItem>
              <SelectItem value="current">Current</SelectItem>
              <SelectItem value="superseded">Superseded</SelectItem>
              <SelectItem value="unclear">Unclear</SelectItem>
            </SelectContent>
          </Select>
          {!!phasesData?.phases.length && (
            <Select
              value={phaseId ?? 'all'}
              onValueChange={(v) => {
                setPhaseId(v === 'all' ? undefined : v)
                setOffset(0)
              }}
            >
              <SelectTrigger className="w-44">
                <SelectValue placeholder="All phases" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">All phases</SelectItem>
                {phasesData.phases.map((p) => (
                  <SelectItem key={p.id} value={p.id}>
                    {p.phase_name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </div>
      </div>

      {isError ? (
        <QueryErrorState error={error} onRetry={() => refetch()} />
      ) : (
      <Card className="overflow-hidden p-0">
        {isLoading ? (
          <div className="space-y-3 p-5">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-10 w-full" />
            ))}
          </div>
        ) : !data?.items.length ? (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="flex flex-col items-center justify-center px-6 py-20 text-center"
          >
            <div className="mb-4 flex size-12 items-center justify-center rounded-2xl bg-zinc-100">
              <FolderOpen className="size-6 text-zinc-400" />
            </div>
            <h2 className="text-base font-semibold text-zinc-900">No artifacts match these filters</h2>
            <p className="mt-1.5 max-w-sm text-sm text-zinc-500">
              Try clearing the direction or phase filter, or upload &amp; run the pipeline first.
            </p>
          </motion.div>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Name</TableHead>
                <TableHead>Category</TableHead>
                <TableHead>Date</TableHead>
                <TableHead>Direction</TableHead>
                <TableHead>Phases</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.items.map((artifact, i) => (
                <motion.tr
                  key={artifact.id}
                  initial={{ opacity: 0 }}
                  animate={{ opacity: 1 }}
                  transition={{ duration: 0.2, delay: Math.min(i, 12) * 0.02 }}
                  onClick={() => navigate(`/projects/${projectId}/artifacts/${artifact.id}`)}
                  className={`cursor-pointer border-b border-zinc-100 transition-colors last:border-0 hover:bg-zinc-50/80 ${
                    (artifact.chosen_date_confidence ?? 1) < 0.4 ? 'opacity-70' : ''
                  }`}
                >
                  <TableCell className="max-w-xs">
                    <p className="truncate font-medium text-zinc-800">{displayName(artifact)}</p>
                    <p className="truncate font-mono text-[11px] text-zinc-400">{artifact.original_filename}</p>
                  </TableCell>
                  <TableCell>
                    <span className="text-sm text-zinc-500">{artifact.view?.suggested_category ?? '—'}</span>
                  </TableCell>
                  <TableCell className="min-w-[160px]">
                    {artifact.chosen_date ? (
                      <div>
                        <p className="text-sm text-zinc-700">{formatDate(artifact.chosen_date)}</p>
                        <ConfidenceMeter
                          value={artifact.chosen_date_confidence ?? 0}
                          size="sm"
                          className="mt-1 max-w-[140px]"
                        />
                      </div>
                    ) : (
                      <span className="text-sm text-zinc-400 italic">Undated</span>
                    )}
                  </TableCell>
                  <TableCell>
                    {artifact.direction ? (
                      <DirectionBadge label={artifact.direction.label} />
                    ) : (
                      <span className="text-sm text-zinc-300">—</span>
                    )}
                  </TableCell>
                  <TableCell>
                    <div className="flex flex-wrap gap-1">
                      {artifact.phases.slice(0, 2).map((p) => (
                        <Badge key={p.id} variant="outline">
                          {p.phase_name}
                        </Badge>
                      ))}
                      {artifact.phases.length > 2 && (
                        <Badge variant="outline">+{artifact.phases.length - 2}</Badge>
                      )}
                    </div>
                  </TableCell>
                </motion.tr>
              ))}
            </TableBody>
          </Table>
        )}
      </Card>
      )}

      {!isError && total > PAGE_SIZE && (
        <div className="mt-4 flex items-center justify-between">
          <p className="text-xs text-zinc-400">
            {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
          </p>
          <div className="flex items-center gap-2">
            <Button
              variant="secondary"
              size="sm"
              disabled={offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              <ChevronLeft className="size-3.5" />
              Prev
            </Button>
            <span className="text-xs text-zinc-400">
              Page {page} of {totalPages}
            </span>
            <Button
              variant="secondary"
              size="sm"
              disabled={offset + PAGE_SIZE >= total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
              <ChevronRight className="size-3.5" />
            </Button>
          </div>
        </div>
      )}

      <ArtifactDetailSheet
        projectId={projectId!}
        artifactId={artifactId ?? null}
        onClose={() => navigate(`/projects/${projectId}/artifacts`)}
      />
    </PageTransition>
  )
}
