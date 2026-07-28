import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** Motion durations/easings shared across every Framer Motion usage — the
 * one place the "motion language" lives so surfaces stay consistent. */
export const motionTokens = {
  duration: {
    instant: 0.1,
    fast: 0.15,
    base: 0.25,
    slow: 0.4,
    slower: 0.6,
  },
  ease: {
    out: [0.16, 1, 0.3, 1] as const,
    inOut: [0.4, 0, 0.2, 1] as const,
  },
  stagger: 0.05,
}

/** Formats a 0..1 confidence score as a whole-number percentage string. */
export function formatConfidence(value: number): string {
  return `${Math.round(value * 100)}%`
}

/** Confidence -> qualitative band, used consistently for copy + styling. */
export function confidenceBand(value: number): 'high' | 'medium' | 'low' {
  if (value >= 0.7) return 'high'
  if (value >= 0.4) return 'medium'
  return 'low'
}

export function formatDate(iso: string | null | undefined, opts?: Intl.DateTimeFormatOptions): string {
  if (!iso) return 'Unknown'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return 'Unknown'
  return d.toLocaleDateString('en-US', opts ?? { year: 'numeric', month: 'short', day: 'numeric' })
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return 'Unknown'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return 'Unknown'
  return d.toLocaleString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i++
  }
  return `${value.toFixed(1)} ${units[i]}`
}
