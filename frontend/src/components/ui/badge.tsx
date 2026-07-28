import * as React from 'react'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const badgeVariants = cva(
  'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium transition-colors',
  {
    variants: {
      variant: {
        neutral: 'border-zinc-200 bg-zinc-100 text-zinc-600',
        outline: 'border-zinc-200 bg-transparent text-zinc-600',
        signal: 'border-signal-200 bg-signal-50 text-signal-700',
        drift: 'border-drift-200 bg-drift-50 text-drift-700',
        unclear: 'border-dashed border-zinc-300 bg-zinc-50 text-zinc-500',
        success: 'border-emerald-200 bg-emerald-50 text-emerald-700',
        danger: 'border-red-200 bg-red-50 text-red-700',
      },
    },
    defaultVariants: { variant: 'neutral' },
  },
)

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { Badge, badgeVariants }
