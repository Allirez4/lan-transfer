import { useEffect, useRef, useState } from 'react'
import { ArrowDownToLine, ArrowUpRight, Check, ChevronRight, File, Folder, FolderUp, HardDrive, Link, LoaderCircle, Pause, Play, Plus, Radio, RefreshCw, Shield, X } from 'lucide-react'
import { api, API, allEntries, ApiError, randomId, listing } from './api'
import type { Capabilities, Job, Share, SharedFile, Transfer } from './api'
import { uploadFile } from './transfer'
import { metadata, selection, requireSameManifest, manifestPages } from './selection'
import type { PendingBatch } from './selection'
import { patchQueue, queueSlice, queueView } from './queue'

const size = (n = 0) => n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : n >= 1e3 ? `${(n / 1e3).toFixed(1)} KB` : `${n} B`
const label = (state: string) => ({ checking_source: 'Checking selected source', repair_required: 'Needs repair', uploading: 'Transferring', finalizing: 'Verifying' }[state] || state.replaceAll('_', ' '))
const message = (e: unknown) => e instanceof Error ? e.message : String(e)

export default function App() {
  const [caps, setCaps] = useState<Capabilities | null>(null)
  const [connecting, setConnecting] = useState(true)
  const [online, setOnline] = useState(true)
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [jobs, setJobs] = useState<Job[]>([])
  const [shares, setShares] = useState<Share[]>([])
  const [selected, setSelected] = useState<string>('')
  const [view, setView] = useState(() => queueView([]))
  const viewRef = useRef(view)
  const rowIndex = useRef(new Map<string, number>())
  const [scrollTop, setScrollTop] = useState(0)
  const [busy, setBusy] = useState(false)
  const [running, setRunning] = useState(false)
  const [selectedShare, setSelectedShare] = useState('')
  const [sharedFiles, setSharedFiles] = useState<SharedFile[]>([])
  const [shareState, setShareState] = useState('')
  const [shareMore, setShareMore] = useState(false)
  const [shareCursors, setShareCursors] = useState<number[]>([0])
  const [dragging, setDragging] = useState(false)
  const [pending, setPending] = useState<PendingBatch | null>(null)
  const [conflictPaths, setConflictPaths] = useState<Record<string, string>>({})
  const refreshing = useRef<Promise<void> | null>(null)
  const sessionEpoch = useRef(0)
  const input = useRef<HTMLInputElement>(null)
  const folder = useRef<HTMLInputElement>(null)
  const reselectJob = useRef('')
  const rowRef = useRef<Transfer[]>([])
  const sources = useRef(new Map<string, File>())
  const controls = useRef(new Map<string, AbortController>())
  const stop = useRef(false)
  const runGuard = useRef(false)
  const currentJob = useRef('')
  const retryQueue = useRef<Transfer[]>([])
  const settled = useRef(new Map<string, Promise<void>>())
  const idleRunners = useRef(new Set<() => void>())
  function wakeRunners() { idleRunners.current.forEach(wake => wake()); idleRunners.current.clear() }

  function replaceRows(next: Transfer[]) {
    rowRef.current = next
    rowIndex.current = new Map(next.map((row, index) => [row.id, index]))
    viewRef.current = queueView(next)
    setView(viewRef.current)
  }
  function update(id: string, patch: Partial<Transfer>) {
    const index = rowIndex.current.get(id)
    if (index === undefined) return
    const row = { ...rowRef.current[index], ...patch }
    rowRef.current[index] = row
    viewRef.current = patchQueue(viewRef.current, index, row)
    setView(viewRef.current)
  }
  async function refresh() {
    if (refreshing.current) return refreshing.current
    const epoch = sessionEpoch.current
    refreshing.current = (async () => {
    try {
      const [capabilities, newJobs, newShares] = await Promise.all([
        api<Capabilities>('/capabilities'), listing<Job>('/jobs'), listing<Share>('/shares'),
      ])
      if (epoch === sessionEpoch.current) { setCaps(capabilities); setJobs(newJobs); setShares(newShares); setOnline(true) }
    } catch (e) {
      if (epoch !== sessionEpoch.current) return
      setOnline(false)
      if (e instanceof ApiError && e.status === 401) { controls.current.forEach(c => c.abort()); setCaps(null) }
      else setError(message(e))
    } finally { setConnecting(false); refreshing.current = null }
    })()
    return refreshing.current
  }
  useEffect(() => {
    queueMicrotask(() => { void refresh(); void metadata.get<PendingBatch>('pending').then(saved => setPending(saved || null)) })
    const activeControls = controls.current
    const timer = setInterval(() => { void refresh() }, 2500)
    return () => { clearInterval(timer); activeControls.forEach(c => c.abort()) }
  }, [])
  useEffect(() => {
    if (folder.current) folder.current.setAttribute('webkitdirectory', '')
  }, [caps])

  useEffect(() => {
    let polling = false
    const timer = setInterval(() => {
      if (polling || !currentJob.current) return
      const jid = currentJob.current
      const waiting = rowRef.current.find(row => !controls.current.has(row.id) && ['verifying', 'finalizing', 'reconnecting'].includes(row.state))
      if (!waiting) return
      polling = true
      void api<Transfer[]>(`/jobs/${jid}/entries?after=${waiting.seq - 1}&limit=500`).then(latest => {
        if (jid !== currentJob.current) return
        for (const row of latest) {
          const index = rowIndex.current.get(row.id)
          const old = index === undefined ? undefined : rowRef.current[index]
          if (old && !controls.current.has(row.id) && ['verifying', 'finalizing', 'reconnecting'].includes(old.state) && (old.state !== row.state || old.offset !== row.offset)) update(row.id, row)
        }
      }).catch(() => { /* The next refresh reconciles connection/authentication errors. */ }).finally(() => { polling = false })
    }, 500)
    return () => clearInterval(timer)
  }, [])

  async function pairDevice(event: React.FormEvent) {
    event.preventDefault(); setBusy(true); setError('')
    try {
      await api('/pair', { code, name: navigator.platform || 'Browser', client: 'browser' })
      sessionEpoch.current++; setCode('')
      await refreshing.current; await refresh()
    }
    catch (e) { setError(message(e)) } finally { setBusy(false) }
  }
  async function chooseJob(id: string) {
    if (runGuard.current && id !== currentJob.current) { setError('Pause the current batch before opening another.'); return }
    setSelected(id); currentJob.current = id; setScrollTop(0)
    try { const next = await allEntries(id); if (currentJob.current !== id) return; replaceRows(next); await metadata.put('selected', { id }) }
    catch (e) { setError(message(e)) }
  }
  useEffect(() => {
    if (caps && !selected) void metadata.get<{id: string}>('selected').then(saved => {
      if (saved && jobs.some(j => j.id === saved.id)) void chooseJob(saved.id)
    })
  }, [caps, selected, jobs])

  function sourceFor(row: Transfer) {
    return sources.current.get(`${row.job}:${row.source_key || row.path}`)
  }

  async function runBatch(jid: string, entries: Transfer[]) {
    if (runGuard.current || currentJob.current !== jid) return
    runGuard.current = true; stop.current = false; setRunning(true)
    const queue = entries.filter(r => r.kind === 'file' && !['completed', 'skipped', 'canceled', 'repair_required', 'conflict'].includes(r.state))
    let position = 0
    async function runner() {
      while (!stop.current) {
        if (position >= queue.length && !retryQueue.current.length) {
          if (!controls.current.size) return
          await new Promise<void>(resolve => { idleRunners.current.add(resolve) })
          continue
        }
        const row = position < queue.length ? queue[position++] : retryQueue.current.shift()!
        const latest = rowRef.current.find(r => r.id === row.id)
        if (latest?.state === 'canceled' || (latest?.state === 'paused' && row.state !== 'paused')) continue
        const file = sourceFor(row)
        if (!file && !['verifying', 'finalizing'].includes(row.state)) { update(row.id, { error: 'Reselect the original files or folder to resume.' }); continue }
        const controller = new AbortController()
        controls.current.set(row.id, controller)
        let finished = () => {}
        settled.current.set(row.id, new Promise<void>(resolve => { finished = resolve }))
        try { await uploadFile(row, file, controller.signal, patch => update(row.id, patch), false) }
        catch (e) {
          if (controller.signal.aborted) {
            // Aborting a request is not proof of a paused server state; reconcile its committed result.
            try { update(row.id, await api<Transfer>(`/uploads/${row.id}`)) }
            catch { update(row.id, { state: 'reconnecting', speed: 0 }) }
          } else {
            update(row.id, { state: 'failed', error: message(e), sent: rowRef.current.find(r => r.id === row.id)?.offset, speed: 0 })
            if (e instanceof ApiError && [401, 403, 507].includes(e.status)) {
              stop.current = true; controls.current.forEach(c => c.abort()); setError(message(e))
            }
          }
        } finally { controls.current.delete(row.id); settled.current.delete(row.id); finished(); wakeRunners() }
      }
    }
    try { await Promise.all(Array.from({ length: caps?.active_files || 1 }, runner)) }
    finally {
      runGuard.current = false; setRunning(false)
      if (!stop.current && retryQueue.current.length) void runBatch(jid, retryQueue.current.splice(0))
      else retryQueue.current = []
      await refresh()
    }
  }

  async function startRegistered(jid: string, entries: Transfer[]) {
    const saved = await metadata.get<PendingBatch>(`manifest:${jid}`)
    const job = await api<Job>(`/jobs/${jid}`)
    if (job.state === 'registering') {
      await api(`/jobs/${jid}/seal`, { expected_entries: saved?.entries.length ?? job.expected_entries ?? entries.length })
    } else if (['paused', 'failed'].includes(job.state)) await api(`/jobs/${jid}/resume`, {})
    replaceRows(await allEntries(jid))
    const waiting = await metadata.get<PendingBatch>('pending')
    if (waiting?.job === jid) { await metadata.delete('pending'); setPending(null) }
    await refresh()
    void runBatch(jid, rowRef.current)
  }

  async function acceptFiles(files: File[]) {
    if (!files.length) return
    if (runGuard.current) { setError('Pause the current batch before selecting more files.'); return }
    setBusy(true); setError('')
    let jid = reselectJob.current
    reselectJob.current = ''
    try {
      const chosen = selection(files)
      let batch: PendingBatch | undefined
      if (jid === 'pending') {
        batch = await metadata.get<PendingBatch>('pending')
        if (!batch) throw new Error('The interrupted selection is no longer available. Select a batch or start a new one.')
        requireSameManifest(batch.entries, chosen.entries)
        jid = batch.job || ''
      }
      if (!jid) {
        batch ??= { key: randomId(), name: files[0].webkitRelativePath?.split('/')[0] || (files.length === 1 ? files[0].name : `${files.length} files`), entries: chosen.entries }
        await metadata.put('pending', batch); setPending(batch)
        const job = await api<Job>('/jobs', { key: batch.key, name: batch.name, expected_entries: batch.entries.length })
        jid = job.id; batch = { ...batch, job: jid }
        await metadata.put(`manifest:${jid}`, batch)
        await metadata.put('pending', batch); setPending({ ...batch })
      }
      const known = await api<Job>(`/jobs/${jid}`)
      if (known.state === 'registering') {
        batch ??= await metadata.get<PendingBatch>(`manifest:${jid}`)
        if (batch) requireSameManifest(batch.entries, chosen.entries)
        else if (known.expected_entries !== null && known.expected_entries !== undefined && known.expected_entries !== chosen.entries.length) throw new Error('Reselect the complete original files/folder before finishing registration.')
        const entries = batch?.entries || chosen.entries
        for (const page of manifestPages(entries)) {
          await api(`/jobs/${jid}/manifest`, { allow_conflicts: true, entries: page })
        }
      }
      const entries = await allEntries(jid)
      for (const row of entries) {
        if (row.kind !== 'file') continue
        const file = chosen.sources.get(row.source_key || '') || files.find(f => (f.webkitRelativePath || f.name) === (row.source_path || row.path) && f.size === row.size)
        if (file) sources.current.set(`${jid}:${row.source_key || row.path}`, file)
      }
      currentJob.current = jid; setSelected(jid); replaceRows(entries)
      await metadata.put('selected', { id: jid })
      await refresh()
      if (!entries.some(r => r.state === 'conflict')) await startRegistered(jid, entries)
    } catch (e) { setError(message(e)); await refresh() }
    finally { setBusy(false); if (input.current) input.current.value = ''; if (folder.current) folder.current.value = '' }
  }

  async function resolveName(row: Transfer, action: 'rename' | 'skip') {
    setBusy(true); setError('')
    try {
      await api(`/uploads/${row.id}/resolve`, { action, path: action === 'rename' ? conflictPaths[row.id] || row.suggested_path : undefined })
      replaceRows(await allEntries(row.job))
      await refresh()
    } catch (e) { setError(message(e)) } finally { setBusy(false) }
  }

  async function jobAction(action: 'pause' | 'resume' | 'cancel') {
    if (!selected) return
    setError('')
    try {
      if (action !== 'resume') { stop.current = true; retryQueue.current = []; controls.current.forEach(c => c.abort()); wakeRunners() }
      await api(`/jobs/${selected}/${action}`, {})
      if (action === 'resume') {
        const entries = await allEntries(selected); replaceRows(entries)
        if (!entries.some(r => sourceFor(r) || ['verifying', 'finalizing'].includes(r.state))) { setError('Use Reselect files or Reselect folder. Committed bytes will be checked before resuming.'); return }
        void runBatch(selected, entries)
      } else if (action === 'cancel') {
        replaceRows(await allEntries(selected))
        const waiting = await metadata.get<PendingBatch>('pending')
        if (waiting?.job === selected) { await metadata.delete('pending'); setPending(null) }
      }
      await refresh()
    } catch (e) { setError(message(e)) }
  }
  async function fileAction(row: Transfer, action: 'pause' | 'resume' | 'cancel') {
    try {
      if (action !== 'resume') controls.current.get(row.id)?.abort()
      if (action === 'resume') {
        await settled.current.get(row.id)
        const job = await api<Job>(`/jobs/${row.job}`)
        if (job.state === 'paused') await api(`/jobs/${row.job}/resume`, {})
      }
      const result = await api<Transfer>(`/uploads/${row.id}/${action}`, {})
      update(row.id, result)
      if (action === 'resume') {
        if (runGuard.current) {
          if (!retryQueue.current.some(item => item.id === row.id)) retryQueue.current.push(result)
          wakeRunners()
        } else void runBatch(row.job, [result])
      }
    } catch (e) { setError(message(e)) }
  }
  async function discardIncomplete() {
    setBusy(true); setError('')
    try { await api(`/jobs/${selected}/incomplete`, undefined, 'DELETE'); await refresh() }
    catch (e) { setError(message(e)) } finally { setBusy(false) }
  }
  async function unpair() {
    try {
      stop.current = true; retryQueue.current = []; controls.current.forEach(c => c.abort()); wakeRunners()
      await api('/sessions/current', undefined, 'DELETE')
      sessionEpoch.current++
      sources.current.clear(); currentJob.current = ''; setSelected(''); replaceRows([])
      setJobs([]); setShares([]); setSharedFiles([]); setSelectedShare(''); setPending(null); setCaps(null)
      await metadata.delete('selected'); await metadata.delete('pending')
    } catch (e) { setError(message(e)) }
  }
  async function openShare(id: string, cursors = [0]) {
    try {
      const result = await api<{ state: string; entries: SharedFile[] }>(`/shares/${id}/manifest?after=${cursors.at(-1) || 0}`)
      setSelectedShare(id); setShareState(result.state); setShareMore(result.entries.length === 500)
      setSharedFiles(result.entries); setShareCursors(cursors)
    } catch (e) { setError(message(e)) }
  }

  const current = jobs.find(j => j.id === selected)
  const { committed, total, conflicts } = view
  const visibleStart = Math.max(0, Math.floor(scrollTop / 94) - 2)
  const visible = queueSlice(view, visibleStart, visibleStart + 12)
  const completeCount = view.completed
  const skippedCount = view.skipped

  if (!caps) return <div className="pair-shell"><div className="pair-aside"><div className="brand"><span className="brand-mark"><ArrowUpRight /></span>LAN Transfer</div><div><span className="eyebrow">YOUR NETWORK. YOUR FILES.</span><h1>A shorter trip<br />for your files.</h1><p>Move files between your devices and laptop.<br />All through your local network.</p></div><span className="aside-foot"><Radio size={16} /> Keep your laptop awake and devices on the same network.</span></div><main className="pair-main"><div className="pair-card"><span className="step">01 / CONNECT</span><h2>Meet your laptop.</h2><p>Enter the single-use pairing code shown in the laptop’s terminal. Codes expire after five minutes.</p><form onSubmit={pairDevice}><label htmlFor="pair-code">Pairing code</label><input id="pair-code" autoComplete="off" autoFocus maxLength={16} placeholder="AB12CD34" value={code} onChange={e => setCode(e.target.value.toUpperCase())} /><button className="primary" disabled={busy || !code}>{busy ? <LoaderCircle className="spin" size={18} /> : <Link size={18} />} Pair this device <ArrowUpRight size={18} /></button></form>{error && <div className="error" role="alert">{error}</div>}<div className="trust"><Shield size={20} /><p>{location.protocol === 'https:' ? 'Your connection uses HTTPS.' : 'Trusted-LAN HTTP: files and pairing credentials are not encrypted. Use HTTPS for sensitive transfers.'}</p></div>{connecting && <p className="muted">Checking connection…</p>}</div></main></div>

  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><span className="brand-mark"><ArrowUpRight /></span>LAN Transfer</div><span className="nav-label">WORKSPACE</span><a className="nav-item active" href="#send"><ArrowUpRight size={19} /> Transfers <span>{jobs.filter(j => !['completed', 'canceled'].includes(j.state)).length}</span></a><a className="nav-item" href="#shares"><Folder size={19} /> Shared on laptop</a><div className="side-note"><Radio size={22} /><strong>Close by. Connected.</strong><p>Files stay on your local network. Your laptop stores each received batch.</p></div><div className="disk"><HardDrive size={19} /><span><strong>{size(caps.disk.available)}</strong><small>available for new transfers</small></span></div><button className="text-button" onClick={() => { void unpair() }}>Unpair this device</button></aside>
    <div className="workspace"><header className="topbar"><span>Personal workspace <ChevronRight size={14} /> Transfers</span><span className="connection"><i /> {online ? (caps.secure ? 'Connected over HTTPS' : 'Connected over local HTTP') : 'Reconnecting…'}</span></header>
      <main className="main-content"><section className="heading"><div><span className="eyebrow">A LITTLE LESS DISTANCE</span><h1>Make yourself at home.</h1><p>Your files, moving between your devices.</p></div><span className="local-badge"><Radio size={16} /> LOCAL NETWORK</span></section>
      {error && <div className="error global" role="alert"><span>{error}</span><button aria-label="Dismiss error" onClick={() => setError('')}><X size={18} /></button></div>}
      {!caps.secure && <div className="notice"><Shield size={16} /> Trusted-LAN HTTP · Files and credentials are not encrypted. Use HTTPS for sensitive transfers.</div>}
      <section id="send" className={`send-zone ${dragging ? 'dragging' : ''}`} onDragOver={e => { e.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={e => { e.preventDefault(); setDragging(false); if (Array.from(e.dataTransfer.items).some(item => item.webkitGetAsEntry?.()?.isDirectory)) { setError('Use Send folder to preserve a directory tree.'); return } void acceptFiles(Array.from(e.dataTransfer.files)) }}>
        <div className="send-icon"><ArrowUpRight size={32} /></div><div className="send-copy"><h2>Send something over.</h2><p>Drop files here, or choose what to send to your laptop.</p><small>Chunked transfers · SHA-256 verified · Resume after interruptions</small></div><div className="send-actions"><button className="primary" disabled={busy || running} onClick={() => { reselectJob.current = ''; input.current?.click() }}><Plus size={17} /> Send files</button><button className="secondary" disabled={busy || running} onClick={() => { reselectJob.current = ''; folder.current?.click() }}><FolderUp size={17} /> Send folder</button></div>
        <input ref={input} className="hidden" type="file" multiple onChange={e => { void acceptFiles(Array.from(e.target.files || [])) }} /><input ref={folder} className="hidden" type="file" multiple onChange={e => { void acceptFiles(Array.from(e.target.files || [])) }} />
      </section>
      <div className="support-note">{caps.disk.filesystem === 'FAT32' && <strong>FAT32 storage cannot accept individual files of 4 GiB or larger. </strong>}Folder selection preserves file paths. Empty folders need the desktop companion. On mobile, keep this page open and the screen awake.</div>
      {pending && !running && <div className="notice"><span>A selection is waiting to finish registration: {pending.name}.</span><button disabled={busy} onClick={() => { reselectJob.current = pending.job || 'pending'; input.current?.click() }}>Reselect files</button><button disabled={busy} onClick={() => { reselectJob.current = pending.job || 'pending'; folder.current?.click() }}>Reselect folder</button></div>}
      <section className="panel queue"><div className="section-title"><div><span className="section-number">01</span><h2>Transfer queue</h2><span className="count">{jobs.length}</span></div><span className="muted">{busy ? 'Scanning & registering…' : running ? 'Transfer in progress' : 'Ready when you are'}</span></div>
        <div className="queue-body"><div className="batch-list">{jobs.length === 0 ? <div className="empty-small">Your next transfer starts here.</div> : jobs.map(job => <button key={job.id} className={`batch ${selected === job.id ? 'selected' : ''}`} onClick={() => { void chooseJob(job.id) }}><Folder size={18} /><span><strong>{job.name}</strong><small>{job.count} entries · {size(job.total)}</small></span><i className={job.state === 'completed' ? 'done-dot' : 'pending-dot'} /></button>)}</div>
        <div className="queue-detail">{!selected ? <div className="empty-state"><div><ArrowUpRight size={30} /></div><h3>A clear path from A to B.</h3><p>Send a file or select a batch to see its progress.</p></div> : <><div className="batch-header"><div><h3>{current?.name || 'Selected batch'}</h3><p>{size(committed)} committed of {size(total)} · {completeCount}/{view.length} complete{skippedCount ? ` · ${skippedCount} explicitly skipped` : ''}</p></div><div className="button-row"><button title="Pause batch" aria-label="Pause batch" onClick={() => { void jobAction('pause') }} disabled={['completed', 'canceled', 'registering'].includes(current?.state || '')}><Pause size={16} /></button><button title="Resume batch" aria-label="Resume batch" onClick={() => { void jobAction('resume') }} disabled={running || ['completed', 'canceled', 'registering'].includes(current?.state || '')}><Play size={16} /></button><button title="Cancel batch" aria-label="Cancel batch" onClick={() => { void jobAction('cancel') }} disabled={['completed', 'canceled'].includes(current?.state || '')}><X size={16} /></button></div></div><div className="batch-progress"><span style={{ width: `${total ? committed / total * 100 : completeCount === view.length ? 100 : 0}%` }} /></div>
          {current?.state === 'canceled' && <div className="reselect"><span>{current.discarded ? 'Incomplete data discarded. Completed files remain available.' : 'Canceled. Incomplete data is retained until you discard it.'}</span>{!current.discarded && <button disabled={busy} onClick={() => { void discardIncomplete() }}>Discard incomplete data</button>}</div>}
          {current?.state === 'registering' && <div className="conflicts"><h3>{conflicts.length ? 'Choose where these names should go' : 'This batch is ready to start'}</h3><p>Renaming changes the destination only. Skips are recorded with the batch.</p>{conflicts.slice(0, 50).map(row => <div className="conflict-row" key={row.id}><strong>{row.source_path || row.path}</strong><small>{row.error}</small><label>Destination path<input aria-label={`Destination for ${row.source_path || row.path}`} value={conflictPaths[row.id] ?? row.suggested_path ?? row.path} onChange={e => setConflictPaths(old => ({ ...old, [row.id]: e.target.value }))} /></label><div><button disabled={busy} onClick={() => { void resolveName(row, 'rename') }}>Use this name</button><button disabled={busy} onClick={() => { void resolveName(row, 'skip') }}>Skip {row.kind === 'directory' ? 'folder' : 'file'}</button></div></div>)}{conflicts.length > 50 && <p>Resolve these to see the next conflicts ({conflicts.length} remaining).</p>}<button className="primary" disabled={busy || conflicts.length > 0 || !view.length || (current.expected_entries !== null && current.expected_entries !== undefined && current.expected_entries !== view.length)} onClick={() => { setBusy(true); void startRegistered(selected, rowRef.current).catch(e => setError(message(e))).finally(() => setBusy(false)) }}>Start resolved batch</button></div>}
          {!running && !['completed', 'canceled'].includes(current?.state || '') && <div className="reselect"><span>After a reload, reselect your originals.</span><button onClick={() => { reselectJob.current = selected; input.current?.click() }}>Reselect files</button><button onClick={() => { reselectJob.current = selected; folder.current?.click() }}>Reselect folder</button></div>}
          <div className="file-viewport" onScroll={e => setScrollTop(e.currentTarget.scrollTop)}><div style={{ height: view.length * 94, position: 'relative' }}>{visible.map((row, i) => <div className="file-row" key={row.id} style={{ position: 'absolute', top: (visibleStart + i) * 94, left: 0, right: 0, height: 94 }}><div className={`file-icon ${row.state === 'completed' ? 'complete' : ''}`}>{row.state === 'completed' ? <Check size={18} /> : row.kind === 'directory' ? <Folder size={18} /> : <File size={18} />}</div><div className="file-info"><div className="file-top"><strong title={row.source_path && row.source_path !== row.path ? `${row.source_path} → ${row.path}` : row.path}>{row.path}</strong><span className={`state ${row.state}`}>{label(row.state)}</span></div><div className="file-progress"><span className="sent" style={{ width: `${row.size ? (row.sent || row.offset) / row.size * 100 : 0}%` }} /><span className="committed" style={{ width: `${row.size ? row.offset / row.size * 100 : row.state === 'completed' ? 100 : 0}%` }} /></div><div className="file-meta"><span title={row.error}>{row.error || `${size(row.offset)} committed · ${size(row.sent || row.offset)} sent · ${size(row.size)} total`}</span><span>{row.speed ? `${size(row.speed)}/s · ${Math.ceil((row.size - row.offset) / row.speed)}s left` : row.retries ? `${row.retries} retries` : ''}</span></div></div>{current?.state !== 'registering' && !['completed', 'skipped', 'conflict', 'canceled', 'verifying', 'finalizing'].includes(row.state) && <div className="file-controls"><button disabled={row.state === 'repair_required' || row.state === 'paused'} aria-label={`Pause ${row.path}`} onClick={() => { void fileAction(row, 'pause') }}><Pause size={13} /></button><button disabled={row.state === 'repair_required' || ['uploading', 'checking_source', 'reconnecting'].includes(row.state)} aria-label={`Retry ${row.path}`} onClick={() => { void fileAction(row, 'resume') }}><Play size={13} /></button><button aria-label={`Cancel ${row.path}`} onClick={() => { void fileAction(row, 'cancel') }}><X size={13} /></button></div>}</div>)}</div></div></>}</div></div>
      </section>
      <section id="shares" className="panel shares"><div className="section-title"><div><span className="section-number">02</span><h2>Shared on your laptop</h2></div><button className="text-button" onClick={() => { void refresh(); if (selectedShare) void openShare(selectedShare) }}><RefreshCw size={14} /> Refresh</button></div><div className="shares-layout"><div className="share-cards">{shares.length ? shares.map(share => <button className={`share-card ${selectedShare === share.id ? 'selected' : ''}`} key={share.id} onClick={() => { void openShare(share.id) }}><Folder size={25} /><strong>{share.name}</strong><span>{share.count} published entries · {size(share.size)}</span><ArrowUpRight size={17} /></button>) : <p className="muted">Received files appear here after verification. Add existing laptop folders with the local share command.</p>}</div>{selectedShare && <div className="shared-files"><div className="shared-heading"><strong>Available files</strong><span>{shareState === 'completed' ? 'Verified / registered share' : 'Batch still incomplete'}</span></div>{sharedFiles.map(file => <div key={file.id}><span title={file.path}>{file.kind === 'directory' ? <Folder size={15} /> : <File size={15} />}{file.path}</span>{file.kind === 'file' && <a href={`${API}/files/${file.id}/content`} aria-label={`Download ${file.path}`}><ArrowDownToLine size={16} /></a>}</div>)}<div className="button-row">{shareCursors.length > 1 && <button onClick={() => { void openShare(selectedShare, shareCursors.slice(0, -1)) }}>Previous files</button>}{shareMore && <button onClick={() => { void openShare(selectedShare, [...shareCursors, sharedFiles.at(-1)?.seq || 0]) }}>Next files</button>}</div></div>}</div><div className="companion"><div><strong>A whole folder? Bring the companion.</strong><p>The desktop client resumes partial downloads and verifies every file.</p></div><code>lan-transfer download {selectedShare || '<share-id>'} --to ./Downloads</code></div></section>
      <footer><span><Shield size={14} /> No cloud account. No external relay.</span><span>Browser download resume depends on your browser. <span className="footer-dot">•</span> LAN Transfer / v1</span></footer>
      </main></div></div>
}
