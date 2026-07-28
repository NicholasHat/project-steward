import * as React from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronDown } from 'lucide-react'
import { cn } from '@/lib/utils'

/** Expandable "why" panel — every label/gap in this product carries a
 * rationale (PROJECTSPECS.md §3.4: "not just a black-box tag"), and this is
 * the one shared affordance for revealing it on demand instead of dumping
 * it in every row. */
export function RationaleDisclosure({
  rationale,
  triggerLabel = 'Why?',
  className,
}: {
  rationale: string | null | undefined
  triggerLabel?: string
  className?: string
}) {
  const [open, setOpen] = React.useState(false)
  if (!rationale) {
    return <span className="text-xs text-zinc-400 italic">No rationale recorded</span>
  }

  return (
    <div className={className}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="inline-flex items-center gap-1 text-xs font-medium text-zinc-500 transition-colors hover:text-signal-700"
      >
        {triggerLabel}
        <motion.span animate={{ rotate: open ? 180 : 0 }} transition={{ duration: 0.15 }}>
          <ChevronDown className="size-3.5" />
        </motion.span>
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            key="content"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
            className="overflow-hidden"
          >
            <p className={cn('mt-2 rounded-lg bg-zinc-50 p-3 text-[13px] leading-relaxed text-zinc-600')}>
              {rationale}
            </p>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
