import { AnimatePresence, motion } from 'framer-motion'
import {
  Boxes,
  Check,
  Compass,
  FileSearch,
  FileText,
  FolderTree,
  History,
  Inbox,
  Layers,
  ListChecks,
  Loader2,
  Share2,
  Tags,
  X,
} from 'lucide-react'
import type { Stage, StageProgressDTO } from '@/lib/api/types'
import { cn } from '@/lib/utils'

const STAGE_META: Record<Stage, { label: string; icon: typeof Inbox }> = {
  ingest: { label: 'Ingest', icon: Inbox },
  parse: { label: 'Parse', icon: FileSearch },
  extract: { label: 'Extract entities & dates', icon: Tags },
  embed: { label: 'Embed', icon: Boxes },
  timeline: { label: 'Build timeline', icon: History },
  phases: { label: 'Assign phases', icon: Layers },
  graph: { label: 'Build reference graph', icon: Share2 },
  direction: { label: 'Infer direction & drift', icon: Compass },
  gaps: { label: 'Detect gaps', icon: ListChecks },
  view: { label: 'Generate view / names', icon: FolderTree },
  report: { label: 'Synthesize report', icon: FileText },
}

function stageState(
  stage: StageProgressDTO,
  isCurrent: boolean,
  overallState: 'idle' | 'running' | 'done' | 'error',
): 'pending' | 'active' | 'done' | 'error' {
  if (stage.error > 0) return 'error'
  // Skipped artifacts (e.g. an unsupported format) are settled, not pending —
  // count them toward completion so the stage doesn't look stuck.
  if (stage.total > 0 && stage.done + stage.skipped >= stage.total) return 'done'
  if (overallState === 'done' && stage.total === 0) return 'done'
  if (isCurrent) return 'active'
  return 'pending'
}

export function StageProgressList({
  stages,
  currentStage,
  overallState,
}: {
  stages: StageProgressDTO[]
  currentStage: Stage | null
  overallState: 'idle' | 'running' | 'done' | 'error'
}) {
  return (
    <ol className="space-y-1">
      {stages.map((stage, i) => {
        const meta = STAGE_META[stage.stage]
        const Icon = meta.icon
        const status = stageState(stage, stage.stage === currentStage, overallState)
        const settled = stage.done + stage.skipped
        const pct = stage.total > 0 ? Math.round((settled / stage.total) * 100) : status === 'done' ? 100 : 0

        return (
          <motion.li
            key={stage.stage}
            initial={{ opacity: 0, x: -8 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.25, delay: i * 0.03, ease: [0.16, 1, 0.3, 1] }}
            className={cn(
              'flex items-center gap-3 rounded-lg border px-3 py-2.5 transition-colors',
              status === 'active' ? 'border-signal-200 bg-signal-50/60' : 'border-transparent',
            )}
          >
            <div
              className={cn(
                'relative flex size-8 shrink-0 items-center justify-center rounded-full border transition-colors',
                status === 'done' && 'border-signal-600 bg-signal-600 text-white',
                status === 'active' && 'border-signal-400 bg-white text-signal-600',
                status === 'pending' && 'border-zinc-200 bg-white text-zinc-300',
                status === 'error' && 'border-red-300 bg-red-50 text-red-600',
              )}
            >
              <AnimatePresence mode="wait" initial={false}>
                {status === 'done' ? (
                  <motion.span
                    key="check"
                    initial={{ scale: 0, opacity: 0 }}
                    animate={{ scale: 1, opacity: 1 }}
                    exit={{ scale: 0, opacity: 0 }}
                    transition={{ duration: 0.2 }}
                  >
                    <Check className="size-4" />
                  </motion.span>
                ) : status === 'error' ? (
                  <X className="size-4" />
                ) : status === 'active' ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <Icon className="size-4" />
                )}
              </AnimatePresence>
            </div>

            <div className="min-w-0 flex-1">
              <div className="flex items-center justify-between gap-2">
                <span
                  className={cn(
                    'text-sm font-medium',
                    status === 'pending' ? 'text-zinc-400' : 'text-zinc-800',
                  )}
                >
                  {meta.label}
                </span>
                {stage.total > 0 && (
                  <span className="shrink-0 font-mono text-xs text-zinc-400">
                    {settled}/{stage.total}
                    {stage.skipped > 0 && (
                      <span className="ml-1 text-amber-600">({stage.skipped} skipped)</span>
                    )}
                    {stage.error > 0 && <span className="ml-1 text-red-500">({stage.error} failed)</span>}
                  </span>
                )}
              </div>
              {stage.total > 0 && (
                <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-zinc-100">
                  <motion.div
                    className={cn('h-full rounded-full', status === 'error' ? 'bg-red-400' : 'bg-signal-600')}
                    initial={false}
                    animate={{ width: `${pct}%` }}
                    transition={{ duration: 0.4, ease: [0.16, 1, 0.3, 1] }}
                  />
                </div>
              )}
            </div>
          </motion.li>
        )
      })}
    </ol>
  )
}
