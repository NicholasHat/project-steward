import * as React from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { AnimatePresence, motion } from 'framer-motion'
import { AlertTriangle, CheckCircle2, Loader2, PlayCircle, UploadCloud } from 'lucide-react'
import { toast } from 'sonner'
import { PageTransition } from '@/components/layout/PageTransition'
import { Dropzone } from '@/components/project/Dropzone'
import { StageProgressList } from '@/components/project/StageProgressList'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { useProjectStatus, useRunPipeline, useUploadFiles } from '@/hooks/queries'
import { ApiError } from '@/lib/api/client'

export function PipelinePage() {
  const { projectId } = useParams<{ projectId: string }>()
  const navigate = useNavigate()
  const [files, setFiles] = React.useState<File[]>([])
  const [redirecting, setRedirecting] = React.useState(false)
  const prevStateRef = React.useRef<string | undefined>(undefined)

  const { data: status, isLoading: statusLoading } = useProjectStatus(projectId, { poll: true })
  const uploadFiles = useUploadFiles(projectId!)
  const runPipeline = useRunPipeline(projectId!)

  const isRunning = status?.state === 'running'
  const hasArtifacts = (status?.stages[0]?.total ?? 0) > 0

  // Auto-advance to the dashboard only when a run we were watching *finishes
  // now* (running -> done). Keying off `state === 'done'` alone re-fired on
  // every revisit of an already-completed project, yanking the user off the
  // upload page each time they came back to add more files.
  React.useEffect(() => {
    const prev = prevStateRef.current
    prevStateRef.current = status?.state
    if (prev === 'running' && status?.state === 'done') {
      setRedirecting(true)
      const t = setTimeout(() => navigate(`/projects/${projectId}/report`), 1400)
      return () => clearTimeout(t)
    }
  }, [status?.state, navigate, projectId])

  async function handleUpload() {
    if (!files.length) return
    try {
      const res = await uploadFiles.mutateAsync(files)
      toast.success(`${res.files.length} file${res.files.length === 1 ? '' : 's'} uploaded`)
      setFiles([])
    } catch (err) {
      toast.error('Upload failed', { description: err instanceof ApiError ? err.message : undefined })
    }
  }

  async function handleRun() {
    try {
      await runPipeline.mutateAsync()
      toast.success('Pipeline started')
    } catch (err) {
      toast.error('Could not start pipeline', {
        description: err instanceof ApiError ? err.message : undefined,
      })
    }
  }

  return (
    <PageTransition>
      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">Upload &amp; run</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Add the project's raw artifacts, then run the pipeline to reconstruct its timeline and
          direction.
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <UploadCloud className="size-4 text-zinc-400" />
              Add artifacts
            </CardTitle>
            <CardDescription>
              Originals are stored exactly as uploaded and never modified or moved.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Dropzone files={files} onFilesChange={setFiles} disabled={uploadFiles.isPending} />
            <Button
              className="mt-4 w-full"
              onClick={handleUpload}
              disabled={!files.length || uploadFiles.isPending}
            >
              {uploadFiles.isPending && <Loader2 className="size-4 animate-spin" />}
              Upload {files.length > 0 ? `${files.length} file${files.length === 1 ? '' : 's'}` : ''}
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-base">
              <PlayCircle className="size-4 text-zinc-400" />
              Run the pipeline
            </CardTitle>
            <CardDescription>
              Ingest → parse → extract → embed → timeline → phases → direction → gaps → view → report.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {statusLoading ? (
              <div className="space-y-2">
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
              </div>
            ) : (
              <AnimatePresence mode="wait" initial={false}>
                {isRunning || (status && status.stages.some((s) => s.done > 0 || s.error > 0)) ? (
                  <motion.div
                    key="progress"
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    exit={{ opacity: 0 }}
                  >
                    <StageProgressList
                      stages={status!.stages}
                      currentStage={status!.current_stage}
                      overallState={status!.state}
                    />
                    {status?.state === 'done' && (
                      <motion.div
                        initial={{ opacity: 0, y: 6 }}
                        animate={{ opacity: 1, y: 0 }}
                        className="mt-4 flex items-center gap-2 rounded-lg bg-signal-50 px-3 py-2.5 text-sm font-medium text-signal-700"
                      >
                        <CheckCircle2 className="size-4" />
                        {redirecting ? 'Done — opening the dashboard…' : 'Done.'}
                      </motion.div>
                    )}
                    {status?.state === 'error' && (
                      <motion.div
                        initial={{ opacity: 0, y: 6 }}
                        animate={{ opacity: 1, y: 0 }}
                        className="mt-4 flex items-start gap-2 rounded-lg bg-red-50 px-3 py-2.5 text-sm text-red-700"
                      >
                        <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                        <span>{status.error ?? 'The pipeline failed.'}</span>
                      </motion.div>
                    )}
                    {status?.state !== 'running' && !redirecting && (
                      <Button className="mt-4 w-full" variant="secondary" onClick={handleRun} disabled={runPipeline.isPending}>
                        {runPipeline.isPending && <Loader2 className="size-4 animate-spin" />}
                        Run again
                      </Button>
                    )}
                  </motion.div>
                ) : (
                  <motion.div key="idle" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                    <p className="mb-4 text-sm text-zinc-500">
                      {hasArtifacts
                        ? 'Artifacts are ready. Run the pipeline to (re)build the dashboard.'
                        : 'Upload at least one file before running the pipeline.'}
                    </p>
                    <Button className="w-full" onClick={handleRun} disabled={runPipeline.isPending}>
                      {runPipeline.isPending && <Loader2 className="size-4 animate-spin" />}
                      Run pipeline
                    </Button>
                  </motion.div>
                )}
              </AnimatePresence>
            )}
          </CardContent>
        </Card>
      </div>
    </PageTransition>
  )
}
