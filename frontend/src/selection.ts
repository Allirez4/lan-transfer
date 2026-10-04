import { openDB } from 'idb'

export interface ManifestEntry { key: string; path: string; size: number; modified: number; kind: 'file' | 'directory' }
export interface PendingBatch { key: string; name: string; entries: ManifestEntry[]; job?: string }

export function* manifestPages(entries: ManifestEntry[]) {
  let page: ManifestEntry[] = [], bytes = 0
  const encoder = new TextEncoder()
  for (const entry of entries) {
    const length = encoder.encode(JSON.stringify(entry)).length + 1
    if (page.length && (page.length === 500 || bytes + length > 800 * 1024)) {
      yield page; page = []; bytes = 0
    }
    page.push(entry); bytes += length
  }
  if (page.length) yield page
}

const fallback = new Map<string, unknown>()
const database = openDB('lan-transfer', 1, { upgrade(db) { db.createObjectStore('metadata') } }).catch(() => null)

// Private browsing or a denied storage permission must not prevent a live transfer.
export const metadata = {
  async get<T>(key: string): Promise<T | undefined> {
    try { return await (await database)?.get('metadata', `${location.origin}:${key}`) ?? fallback.get(key) } catch { return fallback.get(key) as T | undefined }
  },
  async put(key: string, value: unknown) {
    fallback.set(key, value)
    try { await (await database)?.put('metadata', value, `${location.origin}:${key}`) } catch { /* Server retains authoritative job metadata. */ }
  },
  async delete(key: string) {
    fallback.delete(key)
    try { await (await database)?.delete('metadata', `${location.origin}:${key}`) } catch { /* Best-effort local selection hint. */ }
  },
}

export function selection(files: File[]) {
  const entries: ManifestEntry[] = []
  const sources = new Map<string, File>()
  const directories = new Set<string>()
  const occurrences = new Map<string, number>()
  for (const file of files) {
    const path = file.webkitRelativePath || file.name
    const parts = path.split('/')
    for (let count = 1; count < parts.length; count++) {
      const prefix = parts.slice(0, count).join('/')
      if (!directories.has(prefix)) {
        directories.add(prefix)
        entries.push({ key: `directory:${prefix}`, path: prefix, kind: 'directory', size: 0, modified: 0 })
      }
    }
    const occurrence = occurrences.get(path) || 0
    occurrences.set(path, occurrence + 1)
    const key = `file:${occurrence}:${path}`
    entries.push({ key, path, kind: 'file', size: file.size, modified: file.lastModified })
    sources.set(key, file)
  }
  return { entries, sources }
}

export function requireSameManifest(original: ManifestEntry[], selected: ManifestEntry[]) {
  const entries = new Map(selected.map(entry => [entry.key, entry]))
  if (original.length !== selected.length || original.some(entry => {
    const current = entries.get(entry.key)
    return !current || current.path !== entry.path || current.size !== entry.size || current.kind !== entry.kind
  })) throw new Error('This batch was only partly registered. Reselect the complete original files/folder with matching paths and sizes.')
}
