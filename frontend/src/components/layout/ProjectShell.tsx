import { Link, NavLink, Outlet, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  Compass,
  FileText,
  FolderOpen,
  History,
  ListChecks,
  UploadCloud,
} from 'lucide-react'
import { Logo } from './Logo'
import { useProject, useProjectStatus } from '@/hooks/queries'
import { Skeleton } from '@/components/ui/skeleton'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'

const NAV_ITEMS = [
  { to: 'report', label: 'Report', icon: FileText },
  { to: 'artifacts', label: 'Artifacts', icon: FolderOpen },
  { to: 'timeline', label: 'Timeline', icon: History },
  { to: 'direction', label: 'Direction & Drift', icon: Compass },
  { to: 'gaps', label: 'Gaps', icon: ListChecks },
]

const stateBadge: Record<string, { label: string; variant: 'neutral' | 'signal' | 'danger' | 'drift' }> = {
  idle: { label: 'Not processed', variant: 'neutral' },
  running: { label: 'Processing…', variant: 'drift' },
  done: { label: 'Up to date', variant: 'signal' },
  error: { label: 'Run failed', variant: 'danger' },
}

export function ProjectShell() {
  const { projectId } = useParams<{ projectId: string }>()
  const { data: project, isLoading } = useProject(projectId)
  const { data: status } = useProjectStatus(projectId, { poll: true })

  const badge = status ? stateBadge[status.state] : null

  return (
    <div className="min-h-screen bg-zinc-50">
      <aside className="fixed inset-y-0 left-0 z-20 flex w-64 flex-col border-r border-zinc-200 bg-white">
        <div className="flex h-14 items-center border-b border-zinc-100 px-5">
          <Link to="/">
            <Logo />
          </Link>
        </div>

        <div className="border-b border-zinc-100 px-5 py-4">
          <Link
            to="/"
            className="mb-3 inline-flex items-center gap-1 text-xs font-medium text-zinc-400 transition-colors hover:text-zinc-600"
          >
            <ArrowLeft className="size-3" />
            All projects
          </Link>
          {isLoading ? (
            <Skeleton className="h-5 w-32" />
          ) : (
            <h1 className="truncate text-[15px] font-semibold text-zinc-900" title={project?.name}>
              {project?.name}
            </h1>
          )}
          {badge && (
            <Badge variant={badge.variant} className="mt-2">
              {badge.label}
            </Badge>
          )}
        </div>

        <nav className="flex-1 space-y-0.5 px-3 py-4">
          {NAV_ITEMS.map((item) => {
            const Icon = item.icon
            return (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  cn(
                    'flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                    isActive
                      ? 'bg-signal-50 text-signal-700'
                      : 'text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900',
                  )
                }
              >
                <Icon className="size-4" />
                {item.label}
              </NavLink>
            )
          })}
        </nav>

        <div className="border-t border-zinc-100 p-3">
          <NavLink
            to="pipeline"
            className={({ isActive }) =>
              cn(
                'flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                isActive
                  ? 'bg-signal-50 text-signal-700'
                  : 'text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900',
              )
            }
          >
            <UploadCloud className="size-4" />
            Upload & run
          </NavLink>
        </div>
      </aside>

      <div className="pl-64">
        <main className="mx-auto max-w-5xl px-8 py-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
