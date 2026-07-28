import { motion } from 'framer-motion'
import { AlertTriangle, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { ApiError } from '@/lib/api/client'

/** Shared error-state affordance for read surfaces — a failed fetch must
 * never be indistinguishable from "no results" (an empty state renders very
 * differently and implies nothing is wrong). */
export function QueryErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof ApiError ? error.message : 'Something went wrong loading this data.'

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className="flex flex-col items-center justify-center rounded-2xl border border-red-100 bg-red-50/60 px-6 py-16 text-center"
    >
      <div className="mb-4 flex size-12 items-center justify-center rounded-2xl bg-red-100">
        <AlertTriangle className="size-6 text-red-600" />
      </div>
      <h2 className="text-base font-semibold text-zinc-900">Couldn't load this</h2>
      <p className="mt-1.5 max-w-sm text-sm text-zinc-500">{message}</p>
      {onRetry && (
        <Button variant="secondary" className="mt-5" onClick={onRetry}>
          <RefreshCw className="size-4" />
          Try again
        </Button>
      )}
    </motion.div>
  )
}
