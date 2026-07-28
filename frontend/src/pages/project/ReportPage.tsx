import { useNavigate, useParams } from 'react-router-dom'
import Markdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { motion } from 'framer-motion'
import { FileText, RefreshCw, Sparkles } from 'lucide-react'
import { PageTransition } from '@/components/layout/PageTransition'
import { QueryErrorState } from '@/components/layout/QueryErrorState'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { useReport } from '@/hooks/queries'
import { formatDateTime } from '@/lib/utils'

export function ReportPage() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const { data, isLoading, isError, error, refetch } = useReport(projectId!)
  const report = data?.report

  return (
    <PageTransition>
      <div className="mb-8 flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">Report</h1>
          <p className="mt-1 text-sm text-zinc-500">
            The self-updating summary: current direction, recent activity, and open gaps.
          </p>
        </div>
        {report && (
          <Badge variant="neutral" className="mt-1 shrink-0">
            v{report.version} · generated {formatDateTime(report.generated_at)}
          </Badge>
        )}
      </div>

      {isError ? (
        <QueryErrorState error={error} onRetry={() => refetch()} />
      ) : isLoading ? (
        <Card>
          <CardContent className="space-y-3 p-8">
            <Skeleton className="h-6 w-1/3" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-2/3" />
            <div className="pt-4" />
            <Skeleton className="h-6 w-1/4" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
          </CardContent>
        </Card>
      ) : !report ? (
        <motion.div
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-zinc-300 bg-white/60 px-6 py-20 text-center"
        >
          <div className="mb-4 flex size-12 items-center justify-center rounded-2xl bg-signal-50">
            <Sparkles className="size-6 text-signal-600" />
          </div>
          <h2 className="text-base font-semibold text-zinc-900">No report yet</h2>
          <p className="mt-1.5 max-w-sm text-sm text-zinc-500">
            The report is generated once the pipeline has run at least once. Upload artifacts and
            run the pipeline to produce it.
          </p>
          <Button className="mt-5" variant="secondary" onClick={() => navigate(`/projects/${projectId}/pipeline`)}>
            <RefreshCw className="size-4" />
            Go to upload &amp; run
          </Button>
        </motion.div>
      ) : (
        <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.3 }}>
          <Card>
            <CardContent className="p-8">
              <article
                className="prose prose-zinc prose-sm max-w-none
                  prose-headings:font-semibold prose-headings:tracking-tight
                  prose-h1:text-xl prose-h2:text-lg prose-h2:mt-8 prose-h3:text-base
                  prose-a:text-signal-600 prose-a:no-underline hover:prose-a:underline
                  prose-strong:text-zinc-800
                  prose-code:before:content-none prose-code:after:content-none prose-code:bg-zinc-100 prose-code:rounded prose-code:px-1 prose-code:py-0.5
                  prose-blockquote:border-signal-200 prose-blockquote:text-zinc-500
                  prose-hr:border-zinc-100
                  prose-table:text-sm prose-th:text-zinc-500"
              >
                <Markdown remarkPlugins={[remarkGfm]}>{report.content}</Markdown>
              </article>
            </CardContent>
          </Card>
          <p className="mt-3 flex items-center gap-1.5 text-xs text-zinc-400">
            <FileText className="size-3" />
            Regenerated incrementally as new artifacts are processed — this is version {report.version}.
          </p>
        </motion.div>
      )}
    </PageTransition>
  )
}
