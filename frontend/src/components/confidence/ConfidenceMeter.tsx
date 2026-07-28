import * as React from 'react'
import { motion, useMotionValue, useSpring, useTransform } from 'framer-motion'
import { cn, confidenceBand } from '@/lib/utils'

const bandStyles: Record<'high' | 'medium' | 'low', { bar: string; text: string; label: string }> = {
  high: { bar: 'bg-signal-600', text: 'text-signal-700', label: 'High confidence' },
  medium: { bar: 'bg-drift-400', text: 'text-drift-600', label: 'Medium confidence' },
  low: { bar: 'bg-zinc-300', text: 'text-zinc-500', label: 'Low confidence' },
}

interface ConfidenceMeterProps {
  value: number // 0..1
  label?: string
  className?: string
  size?: 'sm' | 'default'
}

/** An animated confidence bar that counts up on mount/change — the primary
 * "show, don't hide, uncertainty" affordance used across timeline, direction
 * and gap surfaces (PROJECTSPECS.md §3.2/§3.4). Low confidence is rendered
 * with a visibly lighter fill + muted number, never omitted. */
export function ConfidenceMeter({ value, label, className, size = 'default' }: ConfidenceMeterProps) {
  const band = confidenceBand(value)
  const styles = bandStyles[band]
  const pct = Math.round(value * 100)

  const motionValue = useMotionValue(0)
  const spring = useSpring(motionValue, { stiffness: 90, damping: 20 })
  const width = useTransform(spring, (v) => `${v}%`)
  const [display, setDisplay] = React.useState(0)

  React.useEffect(() => {
    motionValue.set(pct)
  }, [pct, motionValue])

  React.useEffect(() => {
    const unsub = spring.on('change', (v) => setDisplay(Math.round(v)))
    return unsub
  }, [spring])

  const height = size === 'sm' ? 'h-1' : 'h-1.5'

  return (
    <div className={cn('flex items-center gap-2', className)}>
      <div className={cn('relative w-full overflow-hidden rounded-full bg-zinc-100', height)}>
        <motion.div className={cn('h-full rounded-full', styles.bar)} style={{ width }} />
      </div>
      <span
        className={cn('font-mono font-tabular text-xs shrink-0 w-9 text-right', styles.text)}
        title={label ?? styles.label}
      >
        {display}%
      </span>
    </div>
  )
}
