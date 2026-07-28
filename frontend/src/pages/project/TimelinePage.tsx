import * as React from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { motion } from 'framer-motion'
import { AlertOctagon, History, Loader2 } from 'lucide-react'
import { PageTransition } from '@/components/layout/PageTransition'
import { QueryErrorState } from '@/components/layout/QueryErrorState'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { ConfidenceBadge } from '@/components/confidence/ConfidenceBadge'
import { useGaps, useTimeline } from '@/hooks/queries'
import { cn, confidenceBand, formatDate } from '@/lib/utils'

const PAGE_SIZE = 30

const dotStyle: Record<'high' | 'medium' | 'low', string> = {
  high: 'bg-signal-600 border-signal-600',
  medium: 'bg-white border-drift-400',
  low: 'bg-white border-dashed border-zinc-300',
}

export function TimelinePage() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const [limit, setLimit] = React.useState(PAGE_SIZE)

  const { data, isLoading, isFetching, isError, error, refetch } = useTimeline(projectId!, { limit, offset: 0 })
  const { data: openGaps } = useGaps(projectId!, { status: 'open', limit: 5 })

  const items = data?.items ?? []
  const hasMore = (data?.total ?? 0) > items.length

  return (
    <PageTransition>
      <div className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">Timeline</h1>
          <p className="mt-1 text-sm text-zinc-500">
            The reconstructed chronology — confidence shown, never flattened into one certainty.
          </p>
        </div>
      </div>

      {!!openGaps?.items.length && (
        <motion.button
          type="button"
          initial={{ opacity: 0, y: -6 }}
          animate={{ opacity: 1, y: 0 }}
          onClick={() => navigate(`/projects/${projectId}/gaps`)}
          className="mb-6 flex w-full items-center gap-3 rounded-xl border border-drift-200 bg-drift-50 px-4 py-3 text-left transition-colors hover:bg-drift-100"
        >
          <AlertOctagon className="size-4 shrink-0 text-drift-600" />
          <p className="text-sm text-drift-700">
            <span className="font-semibold">{openGaps.total} open gap{openGaps.total === 1 ? '' : 's'}</span>{' '}
            flagged against this timeline — missing phases or unfulfilled promises. Review them.
          </p>
        </motion.button>
      )}

      {isError ? (
        <QueryErrorState error={error} onRetry={() => refetch()} />
      ) : isLoading ? (
        <div className="space-y-6">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="flex gap-4">
              <Skeleton className="size-3 shrink-0 rounded-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          ))}
        </div>
      ) : !items.length ? (
        <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-zinc-300 bg-white/60 px-6 py-20 text-center">
          <div className="mb-4 flex size-12 items-center justify-center rounded-2xl bg-zinc-100">
            <History className="size-6 text-zinc-400" />
          </div>
          <h2 className="text-base font-semibold text-zinc-900">No timeline events yet</h2>
          <p className="mt-1.5 max-w-sm text-sm text-zinc-500">
            Run the pipeline to reconstruct the project's chronology from its artifacts.
          </p>
        </div>
      ) : (
        <ol className="relative border-l border-zinc-200 pl-6">
          {items.map((event, i) => {
            const band = confidenceBand(event.confidence)
            return (
              <motion.li
                key={event.id}
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                transition={{ duration: 0.25, delay: Math.min(i, 15) * 0.03, ease: [0.16, 1, 0.3, 1] }}
                className={cn('relative mb-6 last:mb-0', band === 'low' && 'opacity-70')}
              >
                <span
                  className={cn(
                    'absolute -left-[29px] top-1.5 size-3 rounded-full border-2',
                    dotStyle[band],
                  )}
                />
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="font-mono text-xs text-zinc-400">{formatDate(event.event_date)}</span>
                  <ConfidenceBadge value={event.confidence} />
                  <Badge variant="outline" className="capitalize">
                    {event.source}
                  </Badge>
                </div>
                <p className="mt-1 text-sm text-zinc-700">{event.description}</p>
                {event.artifact_id && (
                  <button
                    type="button"
                    onClick={() => navigate(`/projects/${projectId}/artifacts/${event.artifact_id}`)}
                    className="mt-1 text-xs font-medium text-signal-600 hover:text-signal-700 hover:underline"
                  >
                    {event.artifact_name}
                  </button>
                )}
              </motion.li>
            )
          })}
        </ol>
      )}

      {hasMore && (
        <div className="mt-6 flex justify-center">
          <Button variant="secondary" onClick={() => setLimit((l) => l + PAGE_SIZE)} disabled={isFetching}>
            {isFetching && <Loader2 className="size-4 animate-spin" />}
            Load more
          </Button>
        </div>
      )}
    </PageTransition>
  )
}
