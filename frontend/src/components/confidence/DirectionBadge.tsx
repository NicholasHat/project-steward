import { Compass, GitBranch, HelpCircle } from 'lucide-react'
import type { DirectionLabelValue } from '@/lib/api/types'
import { cn } from '@/lib/utils'

const config: Record<
  DirectionLabelValue,
  { label: string; icon: typeof Compass; className: string }
> = {
  current: {
    label: 'Current',
    icon: Compass,
    className: 'border-signal-200 bg-signal-50 text-signal-700',
  },
  superseded: {
    label: 'Superseded',
    icon: GitBranch,
    className: 'border-drift-200 bg-drift-50 text-drift-700',
  },
  unclear: {
    label: 'Unclear',
    icon: HelpCircle,
    className: 'border-dashed border-zinc-300 bg-zinc-50 text-zinc-500',
  },
}

export function DirectionBadge({ label, className }: { label: DirectionLabelValue; className?: string }) {
  const c = config[label]
  const Icon = c.icon
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium',
        c.className,
        className,
      )}
    >
      <Icon className="size-3" />
      {c.label}
    </span>
  )
}
