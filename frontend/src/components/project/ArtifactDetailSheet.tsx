import * as React from 'react'
import {
  ArrowLeftRight,
  Calendar,
  Check,
  Loader2,
  Pencil,
  Tag,
  Users,
  X,
} from 'lucide-react'
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from '@/components/ui/sheet'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { Separator } from '@/components/ui/separator'
import { ConfidenceBadge } from '@/components/confidence/ConfidenceBadge'
import { ConfidenceMeter } from '@/components/confidence/ConfidenceMeter'
import { DirectionBadge } from '@/components/confidence/DirectionBadge'
import { DateSourceBadge } from '@/components/confidence/DateSourceBadge'
import { RationaleDisclosure } from '@/components/confidence/RationaleDisclosure'
import { useArtifact, useOverrideArtifactName } from '@/hooks/queries'
import { displayName } from '@/lib/artifactName'
import { formatBytes, formatDateTime } from '@/lib/utils'
import type { ArtifactDetailDTO } from '@/lib/api/types'

/** Owns the "editing" state for the rename affordance, keyed by artifact id
 * from the parent so navigating between two already-open artifacts resets
 * cleanly via remount instead of an effect syncing state to a prop change. */
function ArtifactNameHeader({ projectId, artifact }: { projectId: string; artifact: ArtifactDetailDTO }) {
  const overrideName = useOverrideArtifactName(projectId)
  const [editing, setEditing] = React.useState(false)
  const [nameDraft, setNameDraft] = React.useState(displayName(artifact))

  if (editing) {
    return (
      <form
        onSubmit={(e) => {
          e.preventDefault()
          overrideName.mutate(
            { artifactId: artifact.id, body: { suggested_name: nameDraft } },
            { onSuccess: () => setEditing(false) },
          )
        }}
        className="flex items-center gap-2"
      >
        <Input
          autoFocus
          value={nameDraft}
          onChange={(e) => setNameDraft(e.target.value)}
          className="h-8 text-sm"
        />
        <Button type="submit" size="icon" className="size-8 shrink-0" disabled={overrideName.isPending}>
          {overrideName.isPending ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="size-8 shrink-0"
          onClick={() => setEditing(false)}
        >
          <X className="size-3.5" />
        </Button>
      </form>
    )
  }

  return (
    <SheetTitle className="flex items-center gap-2">
      <span className="truncate">{displayName(artifact)}</span>
      <button
        type="button"
        onClick={() => {
          setNameDraft(displayName(artifact))
          setEditing(true)
        }}
        className="shrink-0 rounded-md p-1 text-zinc-400 hover:bg-zinc-100 hover:text-zinc-600"
      >
        <Pencil className="size-3.5" />
      </button>
    </SheetTitle>
  )
}

export function ArtifactDetailSheet({
  projectId,
  artifactId,
  onClose,
}: {
  projectId: string
  artifactId: string | null
  onClose: () => void
}) {
  const { data: artifact, isLoading } = useArtifact(projectId, artifactId ?? undefined)

  return (
    <Sheet open={!!artifactId} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="flex flex-col p-0">
        {isLoading || !artifact ? (
          <div className="space-y-3 p-6">
            <Skeleton className="h-6 w-2/3" />
            <Skeleton className="h-4 w-1/2" />
            <Skeleton className="h-24 w-full" />
          </div>
        ) : (
          <>
            <SheetHeader>
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <ArtifactNameHeader key={artifact.id} projectId={projectId} artifact={artifact} />
                  <SheetDescription className="mt-1 truncate font-mono text-[11px]">
                    {artifact.original_filename}
                  </SheetDescription>
                </div>
                {artifact.direction && <DirectionBadge label={artifact.direction.label} className="shrink-0" />}
              </div>
            </SheetHeader>

            <div className="flex-1 overflow-y-auto px-6 py-5">
              {/* Chosen date + confidence */}
              <section className="mb-6">
                <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-400">
                  <Calendar className="size-3.5" />
                  Placement
                </h3>
                {artifact.chosen_date ? (
                  <>
                    <p className="text-sm text-zinc-700">{formatDateTime(artifact.chosen_date)}</p>
                    <ConfidenceMeter value={artifact.chosen_date_confidence ?? 0} className="mt-2 max-w-xs" />
                  </>
                ) : (
                  <p className="text-sm text-zinc-400 italic">No confident date resolved</p>
                )}
              </section>

              {/* Direction rationale */}
              {artifact.direction && (
                <section className="mb-6">
                  <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-400">
                    Direction
                  </h3>
                  <div className="flex items-center gap-2">
                    <DirectionBadge label={artifact.direction.label} />
                    <ConfidenceBadge value={artifact.direction.confidence} />
                    {artifact.direction.confirmed_by_user && (
                      <Badge variant="signal">Confirmed by user</Badge>
                    )}
                  </div>
                  <RationaleDisclosure rationale={artifact.direction.rationale} className="mt-2" />
                </section>
              )}

              {/* Phases */}
              {artifact.phases.length > 0 && (
                <section className="mb-6">
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-400">Phases</h3>
                  <div className="space-y-2">
                    {artifact.phases.map((p) => (
                      <div key={p.id} className="flex items-center justify-between gap-3 rounded-lg bg-zinc-50 px-3 py-2">
                        <span className="text-sm text-zinc-700">{p.phase_name}</span>
                        <ConfidenceBadge value={p.confidence} />
                      </div>
                    ))}
                  </div>
                </section>
              )}

              <Separator className="my-5" />

              {/* File info */}
              <section className="mb-6">
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-400">File</h3>
                <dl className="grid grid-cols-2 gap-y-1.5 text-[13px]">
                  <dt className="text-zinc-400">Type</dt>
                  <dd className="text-zinc-700">{artifact.file_type}</dd>
                  <dt className="text-zinc-400">Size</dt>
                  <dd className="text-zinc-700">{formatBytes(artifact.size_bytes)}</dd>
                  <dt className="text-zinc-400">Ingested</dt>
                  <dd className="text-zinc-700">{formatDateTime(artifact.ingested_at)}</dd>
                  <dt className="text-zinc-400">Processing state</dt>
                  <dd className="text-zinc-700 capitalize">{artifact.processing_state}</dd>
                </dl>
                {artifact.processing_note && (
                  <p className="mt-3 rounded-md bg-amber-50 px-3 py-2 text-[13px] text-amber-700">
                    {artifact.processing_note}
                  </p>
                )}
              </section>

              {/* Resolved dates (all signals, not just chosen) */}
              {artifact.resolved_dates.length > 0 && (
                <section className="mb-6">
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-400">
                    Date candidates
                  </h3>
                  <ul className="space-y-2">
                    {artifact.resolved_dates.map((rd) => (
                      <li
                        key={rd.id}
                        className={`flex items-center justify-between gap-3 rounded-lg border px-3 py-2 ${
                          rd.is_chosen ? 'border-signal-200 bg-signal-50/60' : 'border-zinc-100'
                        }`}
                      >
                        <div className="min-w-0">
                          <p className="text-[13px] text-zinc-700">{formatDateTime(rd.candidate_date)}</p>
                          {rd.evidence_text && (
                            <p className="mt-0.5 truncate text-xs italic text-zinc-400">"{rd.evidence_text}"</p>
                          )}
                        </div>
                        <div className="flex shrink-0 items-center gap-2">
                          <DateSourceBadge source={rd.signal_source} />
                          <ConfidenceBadge value={rd.confidence} />
                        </div>
                      </li>
                    ))}
                  </ul>
                </section>
              )}

              {/* Entities */}
              {artifact.entities.length > 0 && (
                <section className="mb-6">
                  <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-400">
                    <Users className="size-3.5" />
                    Entities
                  </h3>
                  <div className="flex flex-wrap gap-1.5">
                    {artifact.entities.map((e) => (
                      <span
                        key={`${e.entity_id}-${e.value}`}
                        className="inline-flex items-center gap-1 rounded-full border border-zinc-200 bg-white px-2 py-0.5 text-xs text-zinc-600"
                        title={`${e.type} · ${e.context ?? ''}`}
                      >
                        <Tag className="size-2.5 text-zinc-400" />
                        {e.value}
                      </span>
                    ))}
                  </div>
                </section>
              )}

              {/* Relationships */}
              {artifact.edges.length > 0 && (
                <section>
                  <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-zinc-400">
                    <ArrowLeftRight className="size-3.5" />
                    Related artifacts
                  </h3>
                  <ul className="space-y-1.5">
                    {artifact.edges.map((edge) => (
                      <li key={edge.id} className="flex items-center gap-2 rounded-lg bg-zinc-50 px-3 py-2 text-[13px]">
                        <Badge variant="outline" className="shrink-0 capitalize">
                          {edge.direction === 'outgoing' ? '→' : '←'} {edge.type.replace('_', ' ')}
                        </Badge>
                        <span className="truncate text-zinc-700">{edge.other_artifact_name}</span>
                        <ConfidenceBadge value={edge.confidence} className="ml-auto shrink-0" />
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  )
}
