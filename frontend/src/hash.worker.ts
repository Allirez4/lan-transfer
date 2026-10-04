import { createSHA256 } from 'hash-wasm'

let source: File
let whole: Awaited<ReturnType<typeof createSHA256>>
let position = 0

self.onmessage = async (event: MessageEvent) => {
  const { id, type, file, offset, length, includeBytes } = event.data
  try {
    if (type === 'init') {
      source = file
      whole = await createSHA256()
      whole.init()
      position = 0
      self.postMessage({ id })
    } else if (type === 'chunk') {
      if (offset !== position) throw new Error('Hash sequence mismatch. Reselect the file to resume.')
      const bytes = await source.slice(offset, offset + length).arrayBuffer()
      if (bytes.byteLength !== length) throw new Error('Source file changed or could not be read')
      const chunk = await createSHA256()
      chunk.init().update(new Uint8Array(bytes))
      whole.update(new Uint8Array(bytes))
      position += length
      const result = { id, digest: chunk.digest('hex'), bytes: includeBytes ? bytes : undefined }
      self.postMessage(result, { transfer: includeBytes ? [bytes] : [] })
    } else if (type === 'digest') {
      if (position !== source.size) throw new Error('Source hashing is incomplete')
      self.postMessage({ id, digest: whole.digest('hex') })
    }
  } catch (error) {
    self.postMessage({ id, error: error instanceof Error ? error.message : String(error) })
  }
}
