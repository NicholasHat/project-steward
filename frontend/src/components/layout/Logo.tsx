import { Radar } from 'lucide-react'
import { cn } from '@/lib/utils'

export function Logo({ className, iconOnly }: { className?: string; iconOnly?: boolean }) {
  return (
    <div className={cn('flex items-center gap-2', className)}>
      <span className="flex size-7 items-center justify-center rounded-lg bg-signal-600 text-white shadow-sm">
        <Radar className="size-4" strokeWidth={2.25} />
      </span>
      {!iconOnly && (
        <span className="text-[15px] font-semibold tracking-tight text-zinc-900">
          Truth Engine
        </span>
      )}
    </div>
  )
}
