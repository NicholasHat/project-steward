import * as React from 'react'
import { Slot } from '@radix-ui/react-slot'
import { cva, type VariantProps } from 'class-variance-authority'
import { cn } from '@/lib/utils'

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-medium transition-colors duration-150 disabled:pointer-events-none disabled:opacity-40 [&_svg]:pointer-events-none [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        default: 'bg-signal-600 text-white shadow-sm hover:bg-signal-700 active:bg-signal-800',
        secondary:
          'bg-white text-zinc-700 border border-zinc-200 shadow-sm hover:bg-zinc-50 hover:border-zinc-300 active:bg-zinc-100',
        ghost: 'text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900',
        outline: 'border border-zinc-200 bg-transparent hover:bg-zinc-50 text-zinc-700',
        destructive: 'bg-red-600 text-white hover:bg-red-700 shadow-sm',
        link: 'text-signal-600 underline-offset-4 hover:underline',
      },
      size: {
        default: 'h-9 px-4 py-2 [&_svg]:size-4',
        sm: 'h-8 rounded-md px-3 text-[13px] [&_svg]:size-3.5',
        lg: 'h-11 rounded-lg px-6 text-base [&_svg]:size-4.5',
        icon: 'size-9 [&_svg]:size-4',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'default',
    },
  },
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : 'button'
    return (
      <Comp className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />
    )
  },
)
Button.displayName = 'Button'

export { Button, buttonVariants }
