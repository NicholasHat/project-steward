/**
 * Typed fetch client: injects the Bearer token, serializes JSON/form/
 * multipart bodies, and normalizes FastAPI error responses into `ApiError`.
 * On a 401 it clears the stored token and hands off to `onUnauthorized`
 * (wired by `AuthProvider` to a router redirect) instead of hard-reloading.
 */

const API_BASE_URL: string = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

const TOKEN_KEY = 'truth_engine_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

let unauthorizedHandler: (() => void) | null = null
export function onUnauthorized(handler: () => void): void {
  unauthorizedHandler = handler
}

export class ApiError extends Error {
  status: number
  detail: unknown

  constructor(status: number, message: string, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

function extractDetail(body: unknown): string | null {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) {
      return detail
        .map((d) => (typeof d === 'object' && d && 'msg' in d ? String((d as { msg: unknown }).msg) : String(d)))
        .join('; ')
    }
  }
  return null
}

interface RequestOptions {
  method?: string
  body?: unknown
  form?: Record<string, string>
  files?: File[]
  query?: Record<string, string | number | boolean | undefined | null>
  skipAuth?: boolean
}

function buildQuery(query?: RequestOptions['query']): string {
  if (!query) return ''
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `?${qs}` : ''
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, form, files, query, skipAuth } = options
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token && !skipAuth) headers.Authorization = `Bearer ${token}`

  let fetchBody: BodyInit | undefined

  if (files) {
    const fd = new FormData()
    for (const file of files) fd.append('files', file)
    fetchBody = fd
  } else if (form) {
    headers['Content-Type'] = 'application/x-www-form-urlencoded'
    fetchBody = new URLSearchParams(form)
  } else if (body !== undefined) {
    headers['Content-Type'] = 'application/json'
    fetchBody = JSON.stringify(body)
  }

  const res = await fetch(`${API_BASE_URL}${path}${buildQuery(query)}`, {
    method,
    headers,
    body: fetchBody,
  })

  if (res.status === 401 && !skipAuth) {
    clearToken()
    unauthorizedHandler?.()
    throw new ApiError(401, 'Session expired. Please log in again.')
  }

  if (res.status === 204) return undefined as T

  const text = await res.text()
  const parsed: unknown = text ? JSON.parse(text) : undefined

  if (!res.ok) {
    const detail = extractDetail(parsed)
    throw new ApiError(res.status, detail ?? res.statusText, parsed)
  }

  return parsed as T
}

export { API_BASE_URL }
