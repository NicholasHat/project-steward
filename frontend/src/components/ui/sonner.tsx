import { Toaster as Sonner } from 'sonner'

type ToasterProps = React.ComponentProps<typeof Sonner>

function Toaster({ ...props }: ToasterProps) {
  return (
    <Sonner
      theme="light"
      className="toaster group"
      position="bottom-right"
      toastOptions={{
        classNames: {
          toast:
            'group toast bg-white border border-zinc-200 shadow-[var(--shadow-popover)] rounded-xl text-zinc-800 text-sm p-4',
          description: 'text-zinc-500',
          actionButton: 'bg-signal-600 text-white rounded-md',
          cancelButton: 'bg-zinc-100 text-zinc-600 rounded-md',
        },
      }}
      {...props}
    />
  )
}

export { Toaster }
