import { Layers, MessageSquareWarning } from 'lucide-react'
import type { GapType } from '@/lib/api/types'
import { cn } from '@/lib/utils'

const config: Record<GapType, { label: string; icon: typeof Layers; className: string }> = {
  structural: {
    label: 'Structural',
    icon: Layers,
    className: 'border-zinc-300 bg-zinc-100 text-zinc-700',
  },
  promised_unfulfilled: {
    label: 'Promised, unfulfilled',
    icon: MessageSquareWarning,
    className: 'border-drift-200 bg-drift-50 text-drift-700',
  },
}

export function GapTypeBadge({ type, className }: { type: GapType; className?: string }) {
  const c = config[type]
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
