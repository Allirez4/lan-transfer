export const API = '/api/v1'

export class ApiError extends Error {
  status: number
  retryAfter: number
  constructor(message: string, status: number, retryAfter = 0) {
    super(message)
    this.status = status
    this.retryAfter = retryAfter
  }
}

export async function api<T>(path: string, body?: unknown, method?: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(API + path, {
    method: method ?? (body === undefined ? 'GET' : 'POST'),
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: 'same-origin',
    signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(30000)]) : AbortSignal.timeout(30000),
  })
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}))
    throw new ApiError(typeof detail.detail === 'string' ? detail.detail : `Request failed (${response.status})`, response.status, Number(response.headers.get('Retry-After') || 0))
  }
  return response.json()
}

// getRandomValues is available on trusted-LAN HTTP; randomUUID is secure-context-only.
export function randomId() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, '0')).join('')
}

export interface Transfer {
  id: string; seq: number; job: string; path: string; kind: string; size: number; modified: number
  offset: number; chunk_size: number; state: string; error?: string
  sent?: number; speed?: number; retries?: number
  source_key?: string; source_path?: string; resolution?: string; suggested_path?: string; job_state?: string
}
export interface Job { id: string; key: string; name: string; state: string; total: number; committed: number; count: number; created: number; expected_entries?: number; skipped?: number; discarded?: number }
export interface Share { id: string; name: string; count: number; size: number; created: number }
export interface SharedFile { id: string; seq: number; path: string; kind: string; size: number }
export interface Capabilities { chunk_size: number; active_files: number; secure: boolean; identity: string; disk: { free: number; reserved: number; available: number; filesystem?: string } }

export async function allEntries(job: string): Promise<Transfer[]> {
  const rows: Transfer[] = []
  while (true) {
    const page = await api<Transfer[]>(`/jobs/${job}/entries?after=${rows.at(-1)?.seq ?? 0}`)
    rows.push(...page)
    if (page.length < 500) return rows
  }
}

export async function listing<T extends { created: number; id: string }>(path: string): Promise<T[]> {
  const rows: T[] = []
  while (true) {
    const last = rows.at(-1)
    const page = await api<T[]>(`${path}?limit=500&after=${last?.created || 0}&after_id=${last?.id || ''}`)
    rows.push(...page)
    if (page.length < 500) return rows
  }
}

export function transient(error: unknown) {
  return error instanceof TypeError || (error instanceof DOMException && error.name === 'TimeoutError') || (error instanceof ApiError && [0, 408, 429, 500, 502, 503, 504].includes(error.status))
}
