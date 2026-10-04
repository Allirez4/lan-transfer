import { api, API, ApiError, randomId, transient } from './api'
import type { Transfer } from './api'

interface HashResult { digest: string; bytes: ArrayBuffer }
interface Receipt { offset: number; length: number; sha256: string }

function aborted(signal: AbortSignal) { if (signal.aborted) throw new DOMException('Paused', 'AbortError') }

function delay(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    aborted(signal)
    const onAbort = () => { clearTimeout(timer); reject(new DOMException('Paused', 'AbortError')) }
    const timer = setTimeout(() => { signal.removeEventListener('abort', onAbort); resolve() }, ms)
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

class Hasher {
  worker = new Worker(new URL('./hash.worker.ts', import.meta.url), { type: 'module' })
  call(body: object): Promise<HashResult> {
    return new Promise((resolve, reject) => {
      this.worker.onmessage = ({ data }) => data.error ? reject(new Error(data.error)) : resolve(data)
      this.worker.onerror = () => reject(new Error('Hash worker failed. Reload and reselect your source.'))
      this.worker.postMessage({ ...body, id: randomId() })
    })
  }
  close() { this.worker.terminate() }
}

function sendChunk(row: Transfer, offset: number, bytes: ArrayBuffer, digest: string, rid: string, signal: AbortSignal, progress: (sent: number) => void) {
  return new Promise<{ offset: number }>((resolve, reject) => {
    aborted(signal)
    const xhr = new XMLHttpRequest()
    xhr.open('PUT', `${API}/uploads/${row.id}/data`)
    xhr.setRequestHeader('Content-Type', 'application/octet-stream')
    xhr.setRequestHeader('Upload-Offset', String(offset))
    xhr.setRequestHeader('Upload-Length', String(bytes.byteLength))
    xhr.setRequestHeader('Upload-SHA256', digest)
    xhr.setRequestHeader('Upload-Request-ID', rid)
    xhr.timeout = 150000
    const onAbort = () => xhr.abort()
    signal.addEventListener('abort', onAbort, { once: true })
    xhr.onloadend = () => signal.removeEventListener('abort', onAbort)
    let last = 0
    xhr.upload.onprogress = event => {
      if (performance.now() - last > 250 || event.loaded === bytes.byteLength) { progress(offset + event.loaded); last = performance.now() }
    }
    xhr.onload = () => {
      let body: { offset: number; detail?: string }
      try { body = JSON.parse(xhr.responseText) } catch { reject(new ApiError('Invalid server response', xhr.status)); return }
      if (xhr.status >= 200 && xhr.status < 300) resolve(body)
      else reject(new ApiError(body.detail || 'Chunk failed', xhr.status, Number(xhr.getResponseHeader('Retry-After') || 0)))
    }
    xhr.onerror = xhr.ontimeout = () => reject(new ApiError('Connection interrupted', 0))
    xhr.onabort = () => reject(new DOMException('Paused', 'AbortError'))
    xhr.send(bytes)
  })
}

async function reconnect<T>(path: string, signal: AbortSignal, update: (patch: Partial<Transfer>) => void, body?: unknown): Promise<T> {
  for (let attempt = 0; ; attempt++) {
    aborted(signal)
    try { return await api<T>(path, body, undefined, signal) }
    catch (error) {
      if (!transient(error) || attempt >= 8) throw error
      update({ state: 'reconnecting', speed: 0 })
      await delay(Math.max(error instanceof ApiError ? error.retryAfter * 1000 : 0, Math.min(30000, 700 * 2 ** attempt) * (0.75 + Math.random() / 2)), signal)
    }
  }
}

async function waitVerification(id: string, signal: AbortSignal, update: (patch: Partial<Transfer>) => void) {
  let interval = 50
  while (true) {
    const latest = await reconnect<Transfer>(`/uploads/${id}`, signal, update)
    update(latest)
    if (latest.state === 'completed') return
    if (['failed', 'repair_required', 'canceled', 'skipped', 'paused'].includes(latest.state) || latest.job_state === 'paused' || latest.job_state === 'canceled') throw new Error(latest.error || `Transfer is ${latest.job_state === 'paused' ? 'paused' : latest.state}`)
    await delay(interval, signal)
    interval = Math.min(500, interval * 2)
  }
}

export async function uploadFile(initial: Transfer, file: File | undefined, signal: AbortSignal, update: (patch: Partial<Transfer>) => void, waitForVerification = true) {
  const hash = new Hasher()
  try {
    let row = await reconnect<Transfer>(`/uploads/${initial.id}`, signal, update)
    if (['completed', 'skipped'].includes(row.state)) { update(row); return }
    if (['verifying', 'finalizing'].includes(row.state)) { update(row); if (waitForVerification) await waitVerification(row.id, signal, update); return }
    if (!file) throw new Error('Reselect the original source file to continue.')
    if (file.size !== row.size) throw new Error('Selected file has a different size. Cancel this file and start a new batch.')
    if (['paused', 'failed'].includes(row.state)) row = await reconnect(`/uploads/${row.id}/resume`, signal, update, {})
    await hash.call({ type: 'init', file })
    update({ state: 'checking_source', sent: row.offset })
    let checked = 0
    while (checked < row.offset) {
      const receipts = await reconnect<Receipt[]>(`/uploads/${row.id}/receipts?after=${checked}`, signal, update)
      update({ state: 'checking_source' })
      if (!receipts.length) throw new Error('Missing server receipts; cannot safely resume')
      for (const receipt of receipts) {
        aborted(signal)
        if (receipt.offset !== checked || checked + receipt.length > row.offset) throw new Error('Inconsistent server checkpoint')
        const result = await hash.call({ type: 'chunk', offset: checked, length: receipt.length, includeBytes: false })
        if (result.digest !== receipt.sha256) throw new Error('Selected source differs from committed bytes. Cancel and start a new batch.')
        checked += receipt.length
      }
    }
    let offset = row.offset
    let speed = 0
    let retries = initial.retries || 0
    const prepare = (start: number) => hash.call({ type: 'chunk', offset: start, length: Math.min(row.chunk_size, row.size - start), includeBytes: true })
      .then(value => ({ value, error: undefined }), error => ({ value: undefined, error }))
    let prepared = offset < row.size ? prepare(offset) : undefined
    while (offset < row.size) {
      aborted(signal)
      const length = Math.min(row.chunk_size, row.size - offset)
      const result = await prepared!
      if (result.error) throw result.error
      const { digest, bytes } = result.value!
      prepared = offset + length < row.size ? prepare(offset + length) : undefined
      const requestId = randomId()
      const started = performance.now()
      let done = false
      for (let retry = 0; !done; retry++) {
        aborted(signal)
        if (retry > 8) throw new Error('Connection could not recover. Reconnect, then Resume.')
        try {
          update({ state: 'uploading', error: undefined })
          const result = await sendChunk(row, offset, bytes, digest, requestId, signal, sent => update({ sent }))
          if (result.offset !== offset + length) throw new Error('Unexpected server offset; reselect to reconcile')
          done = true
        } catch (error) {
          if (!(error instanceof ApiError) || ![0, 408, 409, 429, 500, 502, 503, 504].includes(error.status)) throw error
          update({ state: 'reconnecting', sent: offset, retries: ++retries })
          await delay(Math.max(error.retryAfter * 1000, Math.min(30000, 700 * 2 ** retry) * (0.75 + Math.random() / 2)), signal)
          // A lost acknowledgement is resolved using both the offset and committed checksum.
          try {
            const current = await api<Transfer>(`/uploads/${row.id}`)
            if (current.offset === offset + length) {
              const receipts = await api<Receipt[]>(`/uploads/${row.id}/receipts?after=${offset}&limit=1`)
              if (receipts[0]?.offset !== offset || receipts[0]?.sha256 !== digest) throw new Error('Another client committed different source bytes')
              done = true
            } else if (current.offset !== offset) throw new Error('Server offset changed; reselect the source to reconcile')
            else if (['paused', 'canceled', 'repair_required'].includes(current.state) || ['paused', 'canceled'].includes((current as Transfer & {job_state?: string}).job_state || '')) throw new Error('Transfer was paused or canceled on the server')
          } catch (checkError) {
            if (transient(checkError)) continue
            throw checkError
          }
        }
      }
      offset += length
      const sample = length / ((performance.now() - started) / 1000)
      speed = speed ? speed * 0.7 + sample * 0.3 : sample
      update({ offset, sent: offset, speed })
    }
    aborted(signal)
    const { digest } = await hash.call({ type: 'digest' })
    update({ state: 'verifying', speed: 0 })
    await reconnect(`/uploads/${row.id}/complete`, signal, update, { sha256: digest })
    if (waitForVerification) await waitVerification(row.id, signal, update)
  } finally { hash.close() }
}
