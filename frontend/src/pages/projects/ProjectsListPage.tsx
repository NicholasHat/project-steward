import * as React from 'react'
import { useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import { FolderPlus, FolderOpen, MoreVertical, Plus, Radar, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { PageTransition } from '@/components/layout/PageTransition'
import { QueryErrorState } from '@/components/layout/QueryErrorState'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useCreateProject, useDeleteProject, useProjects } from '@/hooks/queries'
import { formatDate } from '@/lib/utils'
import { ApiError } from '@/lib/api/client'

/** Shared create-project dialog body: name -> create -> land on the
 * project's upload screen. `root_path` is a placeholder until the first
 * `POST .../files` call sets the real one (api/routers/pipeline.py). */
function CreateProjectDialog({ trigger }: { trigger: React.ReactNode }) {
  const [open, setOpen] = React.useState(false)
  const [name, setName] = React.useState('')
  const [error, setError] = React.useState<string | null>(null)
  const navigate = useNavigate()
  const createProject = useCreateProject()

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError(null)
    try {
      const project = await createProject.mutateAsync({ name, root_path: `pending/${Date.now()}` })
      toast.success('Project created', { description: `"${project.name}" is ready for upload.` })
      setOpen(false)
      setName('')
      navigate(`/projects/${project.id}/pipeline`)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'Could not create project.')
    }
  }

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>{trigger}</DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New project</DialogTitle>
          <DialogDescription>
            Name the project, then upload its artifacts on the next screen.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={onSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="project-name">Project name</Label>
            <Input
              id="project-name"
              autoFocus
              required
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="e.g. Catalyst Screening — Group B"
            />
          </div>
          {error && <p className="rounded-md bg-red-50 px-3 py-2 text-[13px] text-red-700">{error}</p>}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={createProject.isPending || !name.trim()}>
              Create project
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function EmptyState() {
  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.98 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
      className="flex flex-col items-center justify-center rounded-2xl border border-dashed border-zinc-300 bg-white/60 px-6 py-24 text-center"
    >
      <div className="mb-5 flex size-14 items-center justify-center rounded-2xl bg-signal-50">
        <Radar className="size-7 text-signal-600" />
      </div>
      <h2 className="text-lg font-semibold text-zinc-900">No projects yet</h2>
      <p className="mt-1.5 max-w-sm text-sm text-zinc-500">
        Upload a messy folder of research artifacts and Truth Engine reconstructs the timeline,
        current direction, and what's missing.
      </p>
      <div className="mt-6">
        <CreateProjectDialog
          trigger={
            <Button>
              <Plus className="size-4" />
              New project
            </Button>
          }
        />
      </div>
    </motion.div>
  )
}

export function ProjectsListPage() {
  const { data: projects, isLoading, isError, error, refetch } = useProjects()
  const deleteProject = useDeleteProject()
  const navigate = useNavigate()

  return (
    <PageTransition>
      <div className="mb-8 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900">Projects</h1>
          <p className="mt-1 text-sm text-zinc-500">Every project's true history, reconstructed.</p>
        </div>
        {!!projects?.length && (
          <CreateProjectDialog
            trigger={
              <Button>
                <Plus className="size-4" />
                New project
              </Button>
            }
          />
        )}
      </div>

      {isError ? (
        <QueryErrorState error={error} onRetry={() => refetch()} />
      ) : isLoading ? (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <Card key={i} className="p-5">
              <Skeleton className="mb-3 h-5 w-2/3" />
              <Skeleton className="h-4 w-1/2" />
            </Card>
          ))}
        </div>
      ) : !projects?.length ? (
        <EmptyState />
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {projects.map((project, i) => (
            <motion.div
              key={project.id}
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.3, delay: i * 0.05, ease: [0.16, 1, 0.3, 1] }}
            >
              <Card
                onClick={() => navigate(`/projects/${project.id}/report`)}
                className="group relative cursor-pointer p-5 transition-shadow hover:shadow-[var(--shadow-card-hover)]"
              >
                <div className="flex items-start justify-between">
                  <div className="flex size-9 items-center justify-center rounded-lg bg-zinc-100 text-zinc-500 group-hover:bg-signal-50 group-hover:text-signal-600">
                    <FolderOpen className="size-4.5" />
                  </div>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="size-7 opacity-0 group-hover:opacity-100"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <MoreVertical className="size-4" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="end" onClick={(e) => e.stopPropagation()}>
                      <DropdownMenuItem
                        className="text-red-600 focus:bg-red-50 focus:text-red-700"
                        onClick={() => deleteProject.mutate(project.id)}
                      >
                        <Trash2 className="size-3.5" />
                        Delete
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                </div>
                <h3 className="mt-3 truncate font-semibold text-zinc-900">{project.name}</h3>
                <p className="mt-1 text-xs text-zinc-400">Created {formatDate(project.created_at)}</p>
              </Card>
            </motion.div>
          ))}
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.3, delay: projects.length * 0.05, ease: [0.16, 1, 0.3, 1] }}
          >
            <CreateProjectDialog
              trigger={
                <button
                  type="button"
                  className="flex h-full min-h-[124px] w-full flex-col items-center justify-center gap-2 rounded-xl border border-dashed border-zinc-300 text-zinc-400 transition-colors hover:border-signal-300 hover:bg-signal-50/40 hover:text-signal-600"
                >
                  <FolderPlus className="size-5" />
                  <span className="text-sm font-medium">New project</span>
                </button>
              }
            />
          </motion.div>
        </div>
      )}
    </PageTransition>
  )
}
