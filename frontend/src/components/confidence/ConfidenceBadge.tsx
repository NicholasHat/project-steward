import { cn, confidenceBand, formatConfidence } from '@/lib/utils'

const dotColor: Record<'high' | 'medium' | 'low', string> = {
  high: 'bg-signal-600',
  medium: 'bg-drift-400',
  low: 'bg-zinc-300',
}

/** Compact confidence indicator for dense contexts (table rows, chips) —
 * same semantics as ConfidenceMeter, without the animated bar. */
export function ConfidenceBadge({ value, className }: { value: number; className?: string }) {
  const band = confidenceBand(value)
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 font-mono text-xs',
        band === 'low' ? 'text-zinc-400' : band === 'medium' ? 'text-drift-600' : 'text-signal-700',
        className,
      )}
    >
      <span className={cn('size-1.5 rounded-full', dotColor[band])} />
      {formatConfidence(value)}
    </span>
  )
}
