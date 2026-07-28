import type { ArtifactDetailDTO, ArtifactSummaryDTO } from './api/types'

/** Mirrors `api/assemble.clean_name`: prefer the current suggested name,
 * fall back to the original filename — never touch the raw file itself. */
export function displayName(artifact: ArtifactSummaryDTO | ArtifactDetailDTO): string {
  return artifact.view?.suggested_name ?? artifact.original_filename
}
