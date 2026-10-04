import { test, expect } from '@playwright/test'
import { patchQueue, queueSlice, queueView } from '../src/queue'
import type { Transfer } from '../src/api'

test('large queue updates retain immutable pages and correct aggregate totals', () => {
  const rows = Array.from({ length: 10000 }, (_, index): Transfer => ({
    id: String(index), seq: index, job: 'batch', path: `file-${index}`, kind: 'file',
    size: 100, offset: 0, modified: 0, chunk_size: 100, state: 'queued',
  }))
  const first = queueView(rows)
  const completed = patchQueue(first, 260, { ...rows[260], offset: 100, state: 'completed' })
  expect(completed.committed).toBe(100)
  expect(completed.completed).toBe(1)
  expect(completed.total).toBe(1000000)
  expect(first.committed).toBe(0)
  expect(first.pages[1][4].state).toBe('queued')
  expect(completed.pages[0]).toBe(first.pages[0])
  expect(completed.pages[2]).toBe(first.pages[2])
  const skipped = patchQueue(completed, 500, { ...rows[500], state: 'skipped' })
  expect(skipped.skipped).toBe(1)
  expect(skipped.total).toBe(999900)
  expect(queueSlice(skipped, 254, 262).map(row => row.id)).toEqual(['254', '255', '256', '257', '258', '259', '260', '261'])
})
