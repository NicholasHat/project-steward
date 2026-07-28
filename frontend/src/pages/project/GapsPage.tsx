import * as React from 'react'
import { useParams } from 'react-router-dom'
import { motion } from 'framer-motion'
import { Check, ListChecks, Loader2, X } from 'lucide-react'
import { PageTransition } from '@/components/layout/PageTransition'
import { QueryErrorState } from '@/components/layout/QueryErrorState'
import { Card } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { ConfidenceMeter } from '@/components/confidence/ConfidenceMeter'
import { GapTypeBadge } from '@/components/confidence/GapTypeBadge'
import { RationaleDisclosure } from '@/components/confidence/RationaleDisclosure'
import { useGaps, usePatchGap } from '@/hooks/queries'
import type { GapStatus } from '@/lib/api/types'

const STATUS_TABS: { value: GapStatus | 'all'; label: string }[] = [
  { value: 'open', label: 'Open' },
  { value: 'confirmed', label: 'Confirmed' },
  { value: 'resolved', label: 'Resolved' },
  { value: 'dismissed', label: 'Dismissed' },
  { value: 'all', label: 'All' },
]

const statusBadgeVariant: Record<GapStatus, 'neutral' | 'drift' | 'signal' | 'outline'> = {
  open: 'drift',
  confirmed: 'neutral',
  resolved: 'signal',
  dismissed: 'outline',
}

export function GapsPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const [status, setStatus] = React.useState<GapStatus | 'all'>('open')

  const { data, isLoading, isError, error, refetch } = useGaps(projectId!, {
    limit: 100,
    status: status === 'all' ? undefined : status,
  })
  const patchGap = usePatchGap(projectId!)

  const items = data?.items ?? []

  return (
    <PageTransition>
      <div className="mb-6 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">Gaps</h1>
          <p className="mt-1 text-sm text-zinc-500">
            What's missing — structurally uncovered phases, and promises never fulfilled.
          </p>
        </div>
      </div>

      <div className="mb-4">
        <Tabs value={status} onValueChange={(v) => setStatus(v as GapStatus | 'all')}>
          <TabsList>
            {STATUS_TABS.map((t) => (
              <TabsTrigger key={t.value} value={t.value}>
                {t.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </Tabs>
      </div>

      {isError ? (
        <QueryErrorState error={error} onRetry={() => refetch()} />
      ) : isLoading ? (
        <div className="space-y-3">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-20 w-full rounded-xl" />
          ))}
        </div>
      ) : !items.length ? (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-zinc-300 bg-white/60 px-6 py-20 text-center"
        >
          <div className="mb-4 flex size-12 items-center justify-center rounded-2xl bg-zinc-100">
            <ListChecks className="size-6 text-zinc-400" />
          </div>
          <h2 className="text-base font-semibold text-zinc-900">
            {status === 'open' ? 'No open gaps' : 'Nothing here'}
          </h2>
          <p className="mt-1.5 max-w-sm text-sm text-zinc-500">
            {status === 'open'
              ? 'Every phase has coverage and no unfulfilled promises were detected — or the pipeline has not run yet.'
              : 'No gaps currently have this status.'}
          </p>
        </motion.div>
      ) : (
        <div className="space-y-3">
          {items.map((gap, i) => (
            <motion.div
              key={gap.id}
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.25, delay: Math.min(i, 15) * 0.03, ease: [0.16, 1, 0.3, 1] }}
            >
              <Card className="p-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <GapTypeBadge type={gap.type} />
                      {gap.phase_name && <Badge variant="outline">{gap.phase_name}</Badge>}
                      <Badge variant={statusBadgeVariant[gap.status]} className="capitalize">
                        {gap.status}
                      </Badge>
                    </div>
                    <p className="mt-2 text-sm text-zinc-700">{gap.description}</p>
                    <div className="mt-2 flex items-center gap-2">
                      <span className="text-xs text-zinc-400">Confidence</span>
                      <ConfidenceMeter value={gap.confidence} size="sm" className="max-w-[120px]" />
                    </div>
                    <RationaleDisclosure rationale={gap.evidence} triggerLabel="Evidence" className="mt-2" />
                  </div>

                  {gap.status === 'open' && (
                    <div className="flex shrink-0 items-center gap-2">
                      <Button
                        variant="secondary"
                        size="sm"
                        disabled={patchGap.isPending}
                        onClick={() => patchGap.mutate({ gapId: gap.id, body: { status: 'confirmed' } })}
                      >
                        <Check className="size-3.5" />
                        Confirm
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        disabled={patchGap.isPending}
                        onClick={() => patchGap.mutate({ gapId: gap.id, body: { status: 'dismissed' } })}
                      >
                        <X className="size-3.5" />
                        Dismiss
                      </Button>
                    </div>
                  )}
                  {gap.status === 'confirmed' && (
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={patchGap.isPending}
                      onClick={() => patchGap.mutate({ gapId: gap.id, body: { status: 'resolved' } })}
                    >
                      {patchGap.isPending && <Loader2 className="size-3.5 animate-spin" />}
                      Mark resolved
                    </Button>
                  )}
                </div>
              </Card>
            </motion.div>
          ))}
        </div>
      )}
    </PageTransition>
  )
}
