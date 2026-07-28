import * as React from 'react'
import { useParams } from 'react-router-dom'
import { motion } from 'framer-motion'
import { Check, Compass, Loader2, Sparkles } from 'lucide-react'
import { PageTransition } from '@/components/layout/PageTransition'
import { QueryErrorState } from '@/components/layout/QueryErrorState'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ConfidenceMeter } from '@/components/confidence/ConfidenceMeter'
import { DirectionBadge } from '@/components/confidence/DirectionBadge'
import { RationaleDisclosure } from '@/components/confidence/RationaleDisclosure'
import { useDirection, usePatchDirectionLabel } from '@/hooks/queries'
import { formatDateTime } from '@/lib/utils'
import type { DirectionLabelValue } from '@/lib/api/types'

const PAGE_SIZE = 50

const SIGNAL_LABEL: Record<'signal_a_score' | 'signal_b_score', string> = {
  signal_a_score: 'Cluster drift (Signal A)',
  signal_b_score: 'Reference graph (Signal B)',
}

export function DirectionPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const [limit, setLimit] = React.useState(PAGE_SIZE)
  const [filter, setFilter] = React.useState<'all' | DirectionLabelValue>('all')

  const { data, isLoading, isFetching, isError, error, refetch } = useDirection(projectId!, {
    limit,
    offset: 0,
  })
  const patchLabel = usePatchDirectionLabel(projectId!)

  const items = data?.labels.items ?? []
  const filtered = filter === 'all' ? items : items.filter((l) => l.label === filter)
  const hasMore = (data?.labels.total ?? 0) > items.length

  return (
    <PageTransition>
      <div className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">Direction &amp; Drift</h1>
        <p className="mt-1 text-sm text-zinc-500">
          What the project is actually pointed at now — not just its earliest artifacts.
        </p>
      </div>

      {isError ? (
        <QueryErrorState error={error} onRetry={() => refetch()} />
      ) : (
      <>
      {isLoading ? (
        <Card className="mb-6">
          <CardContent className="space-y-2 p-6">
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-2/3" />
          </CardContent>
        </Card>
      ) : data?.snapshot ? (
        <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}>
          <Card className="mb-6 border-signal-200 bg-signal-50/40">
            <CardContent className="p-6">
              <div className="mb-2 flex items-center gap-2">
                <Sparkles className="size-4 text-signal-600" />
                <h2 className="text-sm font-semibold text-signal-800">Current direction</h2>
                <span className="ml-auto text-xs text-zinc-400">
                  computed {formatDateTime(data.snapshot.computed_at)}
                </span>
              </div>
              <p className="text-[15px] leading-relaxed text-zinc-700">
                {data.snapshot.inferred_direction_summary}
              </p>
            </CardContent>
          </Card>
        </motion.div>
      ) : (
        <Card className="mb-6">
          <CardContent className="flex items-center gap-3 p-6 text-sm text-zinc-500">
            <Compass className="size-4 text-zinc-400" />
            No direction snapshot yet — run the pipeline to infer it.
          </CardContent>
        </Card>
      )}

      <div className="mb-4">
        <Tabs value={filter} onValueChange={(v) => setFilter(v as typeof filter)}>
          <TabsList>
            <TabsTrigger value="all">All ({items.length})</TabsTrigger>
            <TabsTrigger value="current">Current</TabsTrigger>
            <TabsTrigger value="superseded">Superseded</TabsTrigger>
            <TabsTrigger value="unclear">Unclear</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>

      {isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-24 w-full rounded-xl" />
          ))}
        </div>
      ) : !filtered.length ? (
        <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-zinc-300 bg-white/60 px-6 py-16 text-center">
          <p className="text-sm text-zinc-500">No artifacts with this label.</p>
        </div>
      ) : (
        <div className="space-y-3">
          {filtered.map((label, i) => (
            <motion.div
              key={label.id}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.25, delay: Math.min(i, 15) * 0.03, ease: [0.16, 1, 0.3, 1] }}
            >
              <Card className="p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="truncate font-medium text-zinc-800">{label.artifact_name}</p>
                      <DirectionBadge label={label.label} />
                      {label.confirmed_by_user && (
                        <Badge variant="signal">
                          <Check className="size-2.5" />
                          Confirmed
                        </Badge>
                      )}
                    </div>
                    <div className="mt-2 grid max-w-md grid-cols-1 gap-1.5 sm:grid-cols-2">
                      {(['signal_a_score', 'signal_b_score'] as const).map((key) =>
                        label[key] !== null ? (
                          <div key={key}>
                            <p className="mb-0.5 text-[11px] text-zinc-400">{SIGNAL_LABEL[key]}</p>
                            <ConfidenceMeter value={label[key] as number} size="sm" />
                          </div>
                        ) : null,
                      )}
                    </div>
                    <div className="mt-2 flex items-center gap-2">
                      <span className="text-xs text-zinc-400">Combined confidence</span>
                      <ConfidenceMeter value={label.confidence} size="sm" className="max-w-[120px]" />
                    </div>
                    <RationaleDisclosure rationale={label.rationale} className="mt-2" />
                  </div>

                  <div className="flex shrink-0 items-center gap-2">
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={patchLabel.isPending}
                      onClick={() => patchLabel.mutate({ artifactId: label.artifact_id, body: {} })}
                    >
                      {patchLabel.isPending ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
                      Confirm
                    </Button>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" size="sm">
                          Override
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        {(['current', 'superseded', 'unclear'] as const).map((value) => (
                          <DropdownMenuItem
                            key={value}
                            disabled={value === label.label}
                            onClick={() =>
                              patchLabel.mutate({ artifactId: label.artifact_id, body: { label: value } })
                            }
                          >
                            <DirectionBadge label={value} />
                          </DropdownMenuItem>
                        ))}
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </div>
                </div>
              </Card>
            </motion.div>
          ))}
        </div>
      )}

      {hasMore && (
        <div className="mt-6 flex justify-center">
          <Button variant="secondary" onClick={() => setLimit((l) => l + PAGE_SIZE)} disabled={isFetching}>
            {isFetching && <Loader2 className="size-4 animate-spin" />}
            Load more
          </Button>
        </div>
      )}
      </>
      )}
    </PageTransition>
  )
}
