import { test, expect } from '@playwright/test'
import { randomBytes, createHash } from 'node:crypto'
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve, dirname } from 'node:path'

test('individual files pause and resume while another file is reconnecting', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Pairing code').fill('TEST0007')
  await page.getByRole('button', { name: 'Pair this device' }).click()
  const allowed = new Set<string>()
  await page.route('**/uploads/*/data', route => {
    const uid = route.request().url().split('/').at(-2)!
    return route.request().headers()['upload-offset'] === '0' || allowed.has(uid) ? route.continue() : route.abort()
  })
  await page.locator('input[type=file]').first().setInputFiles(['alpha.bin', 'beta.bin'].map(name => ({ name, mimeType: 'application/octet-stream', buffer: randomBytes(8 * 1024**2) })))
  await expect(page.locator('.file-row .state').filter({ hasText: /^reconnecting$/ })).toHaveCount(2, { timeout: 20000 })
  const jobs = await (await page.request.get('/api/v1/jobs')).json()
  const entries = await (await page.request.get(`/api/v1/jobs/${jobs[0].id}/entries`)).json()
  for (const path of ['alpha.bin', 'beta.bin']) {
    await page.getByRole('button', { name: `Pause ${path}`, exact: true }).click()
    const row = page.locator('.file-row').filter({ hasText: path })
    await expect(row.locator('.state')).toHaveText('paused')
    allowed.add(entries.find((entry: {path: string}) => entry.path === path).id)
    await page.getByRole('button', { name: `Retry ${path}`, exact: true }).click()
    await expect(row.locator('.state')).toHaveText('completed', { timeout: 30000 })
  }
  await expect(page.getByRole('alert')).toHaveCount(0)
})

test('folder picker preserves nested Unicode paths, hidden and empty files', async ({ page }) => {
  const directory = await mkdtemp(join(tmpdir(), 'lan-transfer-folder-'))
  expect(dirname(resolve(directory))).toBe(resolve(tmpdir()))
  try {
    await mkdir(join(directory, 'پوشه'))
    await writeFile(join(directory, 'پوشه', 'سلام.txt'), 'nested bytes')
    await writeFile(join(directory, '.hidden'), 'hidden bytes')
    await writeFile(join(directory, 'empty.txt'), '')
    await page.goto('/')
    await page.getByLabel('Pairing code').fill('TEST0008')
    await page.getByRole('button', { name: 'Pair this device' }).click()
    await page.locator('input[webkitdirectory]').setInputFiles(directory)
    await expect(page.locator('.batch-header')).toContainText('5/5 complete', { timeout: 30000 })
    const jobs = await (await page.request.get('/api/v1/jobs')).json()
    const entries = await (await page.request.get(`/api/v1/jobs/${jobs[0].id}/entries`)).json()
    expect(entries.filter((entry: {kind: string}) => entry.kind === 'directory')).toHaveLength(2)
    const nested = entries.find((entry: {path: string}) => entry.path.endsWith('/پوشه/سلام.txt'))
    expect(await (await page.request.get(`/api/v1/files/${nested.id}/content`)).text()).toBe('nested bytes')
  } finally {
    await rm(directory, { recursive: true, force: true })
  }
})

test('reload during verification completes without reselecting the source', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Pairing code').fill('TEST0004')
  await page.getByRole('button', { name: 'Pair this device' }).click()
  await page.locator('input[type=file]').first().setInputFiles({ name: 'slow-verify.bin', mimeType: 'application/octet-stream', buffer: randomBytes(1024) })
  await expect(page.locator('.file-row .state')).toHaveText('verifying')
  await page.reload()
  await expect(page.locator('.file-row .state')).toHaveText('completed', { timeout: 20000 })
})

test('lost manifest acknowledgement resumes registration after reload', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Pairing code').fill('TEST0005')
  await page.getByRole('button', { name: 'Pair this device' }).click()
  const files = ['first.txt', 'second.txt', 'third.txt'].map(name => ({ name, mimeType: 'text/plain', buffer: Buffer.from(name) }))
  await page.route('**/jobs/*/manifest', async route => { await route.fetch(); await route.abort() })
  await page.locator('input[type=file]').first().setInputFiles(files)
  await expect(page.getByRole('alert')).toBeVisible()
  const before = await (await page.request.get('/api/v1/jobs')).json()
  expect(before[0].state).toBe('registering')
  expect(before[0].count).toBe(3)
  await page.unroute('**/jobs/*/manifest')
  await page.reload()
  await page.locator('.notice').filter({ hasText: 'selection is waiting' }).getByRole('button', { name: 'Reselect files', exact: true }).click({ noWaitAfter: true })
  await page.locator('input[type=file]').first().setInputFiles(files)
  await expect(page.locator('.file-row .state').filter({ hasText: /^completed$/ })).toHaveCount(3, { timeout: 30000 })
  const after = await (await page.request.get('/api/v1/jobs')).json()
  expect(after).toHaveLength(1)
  expect(after[0].count).toBe(3)
})

test('cancel and discard releases only incomplete data', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Pairing code').fill('TEST0006')
  await page.getByRole('button', { name: 'Pair this device' }).click()
  await page.route('**/uploads/*/data', route => route.request().headers()['upload-offset'] === '0' ? route.continue() : route.abort())
  await page.locator('input[type=file]').first().setInputFiles({ name: 'cancel.bin', mimeType: 'application/octet-stream', buffer: randomBytes(8 * 1024**2) })
  await expect(page.locator('.file-row .state')).toHaveText('reconnecting', { timeout: 20000 })
  await page.getByRole('button', { name: 'Cancel batch', exact: true }).click()
  await expect(page.locator('.file-row .state')).toHaveText('canceled')
  await page.getByRole('button', { name: 'Discard incomplete data', exact: true }).click()
  await expect(page.getByText('Incomplete data discarded. Completed files remain available.')).toBeVisible()
})

test('filename conflicts can be renamed or explicitly skipped in the app', async ({ page }) => {
  await page.goto('/')
  await page.getByLabel('Pairing code').fill('TEST0003')
  await page.getByRole('button', { name: 'Pair this device' }).click()
  await page.locator('input[type=file]').first().setInputFiles([
    { name: 'Report.txt', mimeType: 'text/plain', buffer: Buffer.from('original') },
    { name: 'report.txt', mimeType: 'text/plain', buffer: Buffer.from('second') },
    { name: 'REPORT.txt', mimeType: 'text/plain', buffer: Buffer.from('skip this') },
  ])
  await expect(page.getByRole('heading', { name: 'Choose where these names should go' })).toBeVisible()
  await page.getByLabel('Destination for report.txt', { exact: true }).fill('kept-report.txt')
  await page.locator('.conflict-row').filter({ has: page.getByLabel('Destination for report.txt', { exact: true }) }).getByRole('button', { name: 'Use this name' }).click()
  await page.locator('.conflict-row').filter({ has: page.getByLabel('Destination for REPORT.txt', { exact: true }) }).getByRole('button', { name: 'Skip file', exact: true }).click()
  await page.getByRole('button', { name: 'Start resolved batch' }).click()
  await expect(page.locator('.file-row .state').filter({ hasText: /^completed$/ })).toHaveCount(2, { timeout: 30000 })
  await expect(page.locator('.file-row .state').filter({ hasText: /^skipped$/ })).toHaveCount(1)
  await expect(page.locator('.batch-header')).toContainText('1 explicitly skipped')
  const jobs = await (await page.request.get('/api/v1/jobs')).json()
  const entries = await (await page.request.get(`/api/v1/jobs/${jobs[0].id}/entries`)).json()
  for (const [source, bytes] of [['Report.txt', 'original'], ['report.txt', 'second']]) {
    const entry = entries.find((row: {source_path: string}) => row.source_path === source)
    const response = await page.request.get(`/api/v1/files/${entry.id}/content`)
    expect(await response.text()).toBe(bytes)
  }
  await page.reload()
  await expect(page.locator('.file-row').filter({ hasText: 'kept-report.txt' })).toBeVisible()
  await expect(page.locator('.file-row .state').filter({ hasText: /^skipped$/ })).toHaveCount(1)
})

test('desktop: pair, upload real bytes in a worker, verify and download', async ({ page, request }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  const outside: string[] = []
  page.on('request', req => { if (!req.url().startsWith('http://127.0.0.1:8765')) outside.push(req.url()) })
  await page.setViewportSize({ width: 1440, height: 1100 })
  await page.goto('/')
  await page.getByLabel('Pairing code').fill('TEST0000')
  await page.getByRole('button', { name: 'Pair this device' }).click()
  await expect(page.getByRole('heading', { name: 'Make yourself at home.' })).toBeVisible()
  const bytes = randomBytes(5 * 1024 * 1024 + 17)
  await page.locator('input[type=file]').first().setInputFiles({ name: 'سلام-recording.bin', mimeType: 'application/octet-stream', buffer: bytes })
  await expect(page.locator('.file-row .state')).toHaveText('completed', { timeout: 30000 })
  await expect(page.locator('.batch-header')).toContainText('1/1 complete')
  await page.getByRole('button', { name: /سلام-recording.bin.*published entries/ }).click()
  const href = await page.getByRole('link', { name: 'Download سلام-recording.bin' }).getAttribute('href')
  const response = await page.request.get(href!)
  const downloaded = await response.body()
  expect(downloaded.length).toBe(bytes.length)
  expect(createHash('sha256').update(downloaded).digest('hex')).toBe(createHash('sha256').update(bytes).digest('hex'))
  expect(response.headers()['content-disposition']).toContain('attachment')
  expect(errors).toEqual([])
  expect(outside).toEqual([])
  await page.screenshot({ path: '../artifacts/browser-desktop.png', fullPage: true })
  await page.reload()
  await expect(page.locator('.file-row .state')).toHaveText('completed')
  expect((await request.get('/api/v1/shares')).status()).toBe(401)
})

test('mobile: interruption, reload and changed-source rejection', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/')
  await page.getByLabel('Pairing code').fill('TEST0001')
  await page.getByRole('button', { name: 'Pair this device' }).click()
  const bytes = randomBytes(8 * 1024 * 1024 + 17)
  await page.route('**/uploads/*/data', route => {
    if (route.request().headers()['upload-offset'] !== '0') return route.abort()
    return route.continue()
  })
  await page.locator('input[type=file]').first().setInputFiles({ name: 'resume.bin', mimeType: 'application/octet-stream', buffer: bytes })
  await expect(page.locator('.file-row .state')).toHaveText('reconnecting', { timeout: 20000 })
  await page.getByRole('button', { name: 'Pause batch', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Reselect files', exact: true })).toBeVisible()
  await page.reload()
  await page.unroute('**/uploads/*/data')
  const corrupted = Buffer.from(bytes)
  corrupted[0] ^= 255
  await page.getByRole('button', { name: 'Reselect files', exact: true }).click({ noWaitAfter: true })
  await page.locator('input[type=file]').first().setInputFiles({ name: 'resume.bin', mimeType: 'application/octet-stream', buffer: corrupted })
  await expect(page.locator('.file-row')).toContainText('Selected source differs from committed bytes', { timeout: 20000 })
  await page.getByRole('button', { name: 'Reselect files', exact: true }).click({ noWaitAfter: true })
  await page.locator('input[type=file]').first().setInputFiles({ name: 'resume.bin', mimeType: 'application/octet-stream', buffer: bytes })
  await expect(page.locator('.file-row .state')).toHaveText('completed', { timeout: 30000 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: '../artifacts/browser-mobile.png', fullPage: true })
})
