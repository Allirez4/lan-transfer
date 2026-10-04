import type { Transfer } from './api'

const PAGE = 256
export interface QueueView {
  pages: Transfer[][]
  length: number
  committed: number
  total: number
  completed: number
  skipped: number
  conflicts: Transfer[]
}

export function queueView(rows: Transfer[]): QueueView {
  const pages: Transfer[][] = []
  for (let index = 0; index < rows.length; index += PAGE) pages.push(rows.slice(index, index + PAGE))
  return {
    pages, length: rows.length,
    committed: rows.reduce((sum, row) => sum + row.offset, 0),
    total: rows.reduce((sum, row) => sum + (row.state === 'skipped' ? 0 : row.size), 0),
    completed: rows.filter(row => row.state === 'completed').length,
    skipped: rows.filter(row => row.state === 'skipped').length,
    conflicts: rows.filter(row => row.state === 'conflict'),
  }
}

export function patchQueue(view: QueueView, index: number, row: Transfer): QueueView {
  const page = Math.floor(index / PAGE), position = index % PAGE
  const old = view.pages[page][position]
  const pages = view.pages.slice()
  pages[page] = pages[page].slice()
  pages[page][position] = row
  let conflicts = view.conflicts
  if (old.state === 'conflict' || row.state === 'conflict') {
    conflicts = conflicts.filter(item => item.id !== row.id)
    if (row.state === 'conflict') conflicts = [...conflicts, row]
  }
  return {
    ...view, pages, conflicts,
    committed: view.committed + row.offset - old.offset,
    total: view.total + (row.state === 'skipped' ? 0 : row.size) - (old.state === 'skipped' ? 0 : old.size),
    completed: view.completed + Number(row.state === 'completed') - Number(old.state === 'completed'),
    skipped: view.skipped + Number(row.state === 'skipped') - Number(old.state === 'skipped'),
  }
}

export function queueSlice(view: QueueView, start: number, end: number): Transfer[] {
  const rows = []
  for (let index = start; index < Math.min(end, view.length); index++) rows.push(view.pages[Math.floor(index / PAGE)][index % PAGE])
  return rows
}
