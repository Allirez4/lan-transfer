# Validation and performance report

## Executed in this workspace

Windows, Python 3.12, FastAPI 0.141.1, Starlette 1.6.0, Uvicorn 0.52.4, httpx 0.28.1; Node 22.17, React 19.2.8, Vite 8.3.0. Exact Python and JavaScript dependency resolutions are in the lockfiles.

- Python suite covers server/client protocol, Unicode/empty-directory tree round-trip, checkpoint resume, source changes before/during downloads, disk/fsync/metadata failures, 1,500-entry pagination, path/symlink confinement, authentication/origin checks, and real subprocess kills mid-chunk and after commit. See STATUS.md for the final test count.
- Eight Edge tests cover real upload/download hashes, nested Unicode folders, conflict rename/skip, cancellation cleanup, per-file pause/retry during another file's reconnection, reload during verification, interrupted manifest replay and changed-source rejection. The desktop transfer test checks every request stays on the local origin. Mobile viewport emulation is not a physical device test.
- Production frontend build and lint pass.
- Installed Starlette behavior was checked for authenticated GET/HEAD, 206/416, Content-Range, Content-Length, matching/mismatching If-Range, ETag and attachment filenames. No replacement range implementation was needed. [Starlette FileResponse](https://starlette.dev/responses/#fileresponse)

All measured transfers below used loopback on this Windows laptop, with source/server/download on the same volume. Other development tests and builds ran concurrently. These are real materialized storage transfers, **not Wi-Fi or isolated bottleneck measurements**. The 80 GB tree, physical router/devices, browser memory and independent disk/network limits remain unmeasured.

### Real 5 GB and 10 GB round trips (2026-09-15)

16 MiB chunks, one active CLI file, server limit two. GB/MB are decimal. Both directions include whole-file disk verification; a separate independent destination re-read also matched the original random-data hash and exact length.

| File | Upload including verification | Server verification wait¹ | Download including verification | Result |
|---|---:|---:|---:|---|
| 5,000,000,000 bytes | 203.23 s · 24.60 MB/s | 7.10 s | 235.47 s · 21.23 MB/s | Passed |
| 10,000,000,000 bytes | 371.27 s · 26.93 MB/s | 17.78 s | 447.63 s · 22.34 MB/s | Passed |

¹ Includes polling delay. Generation and the independent final recheck are recorded separately. Generated fixture copies were removed after validation; checksums and measurements are retained in the [5 GB result](benchmarks/2026-09-15-5GB-loopback.json) and [10 GB result](benchmarks/2026-09-15-10GB-loopback.json).

### Memory finding and completed fix

The large runs exposed request-buffer retention in the CLI: peak client RSS reached 494/605 MiB before the fix. The client now submits a bounded, exhaustible request iterator, allowing each chunk to be released promptly instead of remaining reachable through HTTP response cycles. A separate post-fix test uploaded **two real 256 MiB files simultaneously**, using two CLI workers and a fresh server: **71.61 MiB peak server process-tree RSS**, **109.18 MiB peak combined client RSS**, 14.26 s including verification. The server met the starting 256 MiB target. [Recorded memory test](benchmarks/2026-09-15-two-client-memory.json)

The first large-run sampler measured the Windows venv launcher rather than its child server, so those full-run server RSS/CPU values are explicitly marked unavailable in the reports. The corrected sampler includes the child. An observation during the latter part of the 10 GB run saw 72.51 MiB server RSS; it is not presented as a full-run peak. No browser process-memory claim is made.

The verifier now wakes when completion is queued, and the CLI uses short initial verification polls with a bounded backoff. This avoids imposing a fixed half-second wait on every tiny file while preserving full hashing and fsync durability.

### Mixed tree with 2,000 files

A 64 MiB materialized tree (one 32 MiB file, 1,999 smaller files across 16 directories, plus an empty nested directory) passed upload and download. All 2,000 destination sizes/hashes were independently re-read and matched. Upload including verification took **244.42 s**; download including verification took **178.20 s**. Peak server process-tree RSS was **75.85 MiB**, client **83.17 MiB**. Small-file metadata, filesystem durability and per-file request overhead dominate this result; it is not a bulk-network throughput benchmark. [Full manifest and recorded result](benchmarks/2026-09-15-2000-files-loopback.json)

### Materialized localhost smoke run (2026-09-15)

One 67,108,864-byte random file, one active CLI file, 4 MiB chunks, Windows 11/Python 3.12.10; source, server and destination on this machine. Upload including verification: **3.776 s (17.77 MB/s)**. Server verification wait including polling: **0.505 s**. Download including destination disk verification: **3.456 s (19.42 MB/s)**. SHA-256 and byte counts matched. Generation took 2.869 s, separately excluded from transfer totals. [Raw recorded result](benchmarks/2026-09-15-loopback.json)

This validates the runnable benchmark pipeline with materialized bytes. It does not measure sustained Wi-Fi, independent disk/network limits, or two-file memory. The serving process used disposable test storage; its share ID is historical, not a current downloadable share.

## September 25 performance update

The same 64 MiB / 2,000-file generated-tree procedure passed with independent size/SHA-256 checks for every destination. Upload including verification took **86.39 s**, and download including disk verification took **73.54 s**. The September 15 recorded times were 244.42 s and 178.20 s (approximately 2.8x and 2.4x faster respectively). The newer client allows two simultaneous uploads, batches manifest writes and streams downloads with 16 MiB checkpoints. Server peak RSS was **76.30 MiB**, client **99.55 MiB**. [Full result](benchmarks/2026-09-25-2000-files-loopback.json)

A separate **256 MiB** materialized random file passed upload, download and an independent destination recheck: **3.00 s / 89.33 MB/s** upload, **1.09 s / 246.81 MB/s** download. Peak RSS was 72.06 MiB server / 99.26 MiB client. This smaller cached loopback test is not directly comparable to the older 5/10 GB runs. [Full result](benchmarks/2026-09-25-256MiB-loopback.json)

Other local development/test work may run concurrently. These comparisons are against retained historical runs, not a controlled same-day A/B test; hardware LAN throughput remains unmeasured. Checksums, durability ordering and final disk verification remain enabled.

Two simultaneous 256 MiB uploads also passed the memory check: peak server process-tree RSS **72.17 MiB**, combined client RSS **125.68 MiB**; elapsed time including verification **2.29 s**. The backend remains below the 256 MiB target in this bounded loopback test. [Memory result](benchmarks/2026-09-25-two-client-memory.json)

With concurrent uploads and deferred completion polling, `sum_verification_observation_latency_seconds` sums overlapping per-file intervals from requesting verification to observing completion. It can exceed total elapsed time and must not be interpreted as time blocked waiting.

## Runnable materialized benchmark

To reproduce the isolated loopback checks, without pairing or touching existing storage:

```powershell
.\backend\.venv\Scripts\python.exe scripts/validate_large.py --bytes 5000000000 10000000000 --output D:/lan-validation --confirm-large
.\backend\.venv\Scripts\python.exe scripts/validate_large.py --bytes 67108864 --files 2000 --output D:/mixed-validation
.\backend\.venv\Scripts\python.exe scripts/validate_memory.py
```

The first tool requires **3× the largest dataset plus 2 GiB free**, runs requested sizes sequentially, and deletes only its generated fixture directories after each run. Logs/hashes/results remain. For multi-file validation, half the bytes go to one larger file and the rest to small files across 16 directories, plus an empty nested directory. The memory test uses about 1 GiB of temporary source/server data. Neither tool allocates an 80 GB dataset by default.

Pair the companion first. Tests above 1 GiB require an explicit flag. `--bytes` is decimal bytes; MiB denotes binary units.

```powershell
# Small reproducible baseline; keeps all artifacts and both transfer copies.
.\backend\.venv\Scripts\python.exe scripts/benchmark.py --workdir D:/lan-bench --bytes 67108864

# Real individual-file gates (requires source + server + download space).
.\backend\.venv\Scripts\python.exe scripts/benchmark.py --workdir D:/lan-bench --bytes 5000000000 --confirm-large
.\backend\.venv\Scripts\python.exe scripts/benchmark.py --workdir D:/lan-bench --bytes 10000000000 --confirm-large

# Real materialized 80 GB nested tree, 10,000 files and empty directories.
.\backend\.venv\Scripts\python.exe scripts/benchmark.py --workdir D:/lan-bench --bytes 80000000000 --files 10000 --confirm-large
```

Client source + download require roughly **2× dataset bytes**, plus server storage of **1× dataset bytes**. On one volume allow roughly 3× plus safety margins. The 80 GB local test needs about 240 GB free, not 80 GB. The tool checks client and server admission separately; when both use the same disk, independently ensure the combined requirement. It generates fresh `os.urandom` bytes, writes/fsyncs them, and records a manifest of sizes/hashes. It does not create sparse/all-zero throughput datasets or delete files.

`results.json` records generation time, upload including server verification, server verification wait including polling delay, download including destination disk verification, end-to-end decimal MB/s and client CPU time. All files, logs and local state remain in the printed benchmark folder. Server shares remain until local maintenance. RSS/network-only fields remain null until independently measured.

## Hardware and tuning matrix

1. Record laptop/client OS and CPU, filesystem, free space, storage media, router/adapter models, Wi-Fi band/signal, link rates, power mode and software versions. Keep sources unchanged and the laptop awake.
2. Record disk read/write rates using materialized files larger than cache where space allows. Separately time SHA-256. Record warm/cold cache conditions; do not silently drop caches or change durability settings.
3. Use `iperf3 -s` on one host and `iperf3 -c <host> -t 60`, plus `-R`, from the other to measure network-only throughput. On Windows record the actual binary source and version; ESnet does not provide an official Windows binary. Use a trusted packaged build or a supported Linux host, noting topology differences. [ESnet iperf3](https://software.es.net/iperf/)
4. Run the materialized benchmark over the actual all-Wi-Fi arrangement. Repeat with laptop Ethernet if available. Keep WAN disconnected for a separate offline acceptance pass; do not disable LAN connectivity. Confirm browser developer tools show no external/CDN requests.
5. Repeat with server `chunk_size` 4194304/8388608/16777216/33554432 and `active_files` 1/2/4. Restart the server after each configuration change; start **new jobs** so chunk size is fixed. The CLI benchmark now uses up to two simultaneous upload files; use `--workers 1` with the companion for a serial baseline. Keep one active file for an HDD baseline.
6. Sample the actual server/CLI process RSS and CPU every second (include the Python child of a Windows venv launcher). Separate process working set from OS file cache; monitor disk/network throughput. The starting backend RSS goal below 256 MiB passed the two-client loopback check above; repeat on the real deployment.
7. Report transfer-only rates, verification time, total wall time, per-file errors and retries, peak memory/CPU, and path/count/size/hash agreement. Investigate large-file throughput below roughly 70% of the measured network/storage/hash bottleneck; this is a diagnostic threshold, not an acceptance promise.
8. Repeat disconnect/kill/restart at randomized positions, before/after finalization, and at a download checkpoint. Verify finished files are not retransmitted and that an unexpected 200 response is never appended to partial data. Exercise disk-full and denied permissions on a disposable test volume.

Do not call an 80 GB test complete until actual paths/counts/sizes/hashes match, all registered entries finish, and there are no hidden failures. Manual remaining gates are tracked in STATUS.md.
