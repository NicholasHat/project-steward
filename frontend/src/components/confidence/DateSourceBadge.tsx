import { FileText, HardDrive, ScanText } from 'lucide-react'
import type { DateSignalSource } from '@/lib/api/types'
import { cn } from '@/lib/utils'

const config: Record<DateSignalSource, { label: string; icon: typeof ScanText; className: string }> = {
  content: {
    label: 'From content',
    icon: ScanText,
    className: 'border-signal-200 bg-signal-50 text-signal-700',
  },
  doc_meta: {
    label: 'Document metadata',
    icon: FileText,
    className: 'border-zinc-200 bg-zinc-100 text-zinc-600',
  },
  filesystem: {
    label: 'Filesystem only',
    icon: HardDrive,
    className: 'border-dashed border-zinc-300 bg-zinc-50 text-zinc-500',
  },
}

export function DateSourceBadge({ source, className }: { source: DateSignalSource; className?: string }) {
  const c = config[source]
  const Icon = c.icon
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium',
        c.className,
        className,
      )}
    >
      <Icon className="size-3" />
      {c.label}
    </span>
  )
}
