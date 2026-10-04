# Implementation status

The required v1 software workflows are implemented. The remaining acceptance work requires larger storage and physical network/client devices; it is listed separately below.

## September 25 update

- Native Tk laptop interface and double-click launcher: server lifecycle, QR/pairing codes, file/folder pickers, refresh/unshare, devices, incoming jobs, settings, and companion pairing/upload/download/pause/resume.
- Resilient verifier retries and truthful degraded health; job-summary reconciliation after failed completion commits; cancellation of disconnected downloads and send idle timeouts.
- Session expiry, orphan cancellation/local cleanup, private-network admission, per-device job/manifest quotas, and live enforcement of unsharing/configured roots.
- Concurrent companion upload workers with one-chunk prefetch, batched manifest transactions, streamed downloads, cheap live retries, independent lazy hashing, quicker browser verification polling and immutable paged queue updates.
- **74 Python tests and nine Edge browser tests pass**, including native widget callbacks, a real desktop-controller/companion round trip, and interrupted-transfer recovery. Production frontend build, lint and Python dependency compatibility checks pass. Two upstream TestClient deprecation warnings remain.
- A repeated 2,000-file 64 MiB loopback tree passed every independent destination hash: 86.39 s upload / 73.54 s download, versus the September 15 record of 244.42 s / 178.20 s. A 256 MiB file passed at 89.33 MB/s upload and 246.81 MB/s download. These are local-storage/cache measurements, not Wi-Fi promises.

## Completed behavior

- One-worker FastAPI service, local React production build, configurable storage, exact host/origin checks, single-use pairing, hashed/revocable sessions, Windows setup/start scripts and LAN URL/QR output.
- Bounded sequential chunks, chunk SHA-256, fsync-before-SQLite checkpoints, idempotent receipts, source-prefix checks, serialized final verification, no-replace publication and startup recovery. A failed file cannot prevent healthy siblings from transferring.
- Paged manifests with expected-count checks and recovery from interrupted registration. Stable rename/skip decisions preserve original source paths and carry folder mappings through descendants and later pages. Skips are visible and included in completion accounting.
- Nested Unicode paths, hidden/zero-byte files, explicit empty directories through the CLI, path confinement, free-space reservations and Windows FAT32 detection. Existing laptop shares need no upload or copy.
- Browser batch/file controls, retry while another file reconnects, verification polling after reload, virtualized transfer rows, all-page job/share loading and bounded shared-file pages. Cancellation retains partial bytes until explicit discard.
- Installable pair/upload/download/resume/jobs companion, durable conflict policies, disk-backed manifests, pinned-version ranges, checksum checkpoints, final destination verification and publication recovery. CLI request buffers are released after each chunk.
- Pinned dependencies, protocol/OpenAPI snapshot, recovery/browser/HTTPS guides, benchmark tools and retained measurements. Caddy HTTPS was exercised with an isolated trusted CA and no system trust changes.

## Earlier validation on Windows 11 (September 15)

- **59 Python tests passed** in 21.00 s, including real process-tree kills mid-chunk and after commit, finalization recovery, file changes, disk/fsync/database failures, duplicate writers, conflicts, pagination, auth/origin checks and real TLS sockets. Two upstream TestClient deprecation warnings remain.
- **Eight Microsoft Edge browser tests passed** in 54.5 s, covering actual bytes, folder paths, reload/reselection, verification recovery, manifest replay, per-file controls, explicit conflicts and cancellation cleanup. Desktop and 390 px mobile layouts were inspected; the latter is viewport emulation.
- Windows setup script, production frontend build, lint, installed CLI commands and Python dependency compatibility checks pass.
- Real **5 GB and 10 GB** materialized upload/download round trips passed with matching lengths and SHA-256. Timings include verification and are loopback measurements.
- Two simultaneous **256 MiB** uploads passed after the client memory fix: peak server process-tree RSS **71.61 MiB**, client RSS **109.18 MiB**. Details and measurement limitations are in [BENCHMARKS.md](BENCHMARKS.md).
- A **2,000-file mixed tree** (64 MiB, including a 32 MiB file and an empty nested directory) passed both directions and independent destination hash checks. Peak server RSS was **75.85 MiB**.
- Caddy **2.11.4** reverse-proxy TLS test passed: certificate trust, Secure cookies, upload/verification/download/ranges and rejection of an untrusted certificate.

## Platform limits and remaining hardware acceptance

1. **80 GB mixed tree:** unmeasured. A same-volume source/server/download test needs about 240 GB plus margins; that free capacity was not available. The opt-in procedure is ready.
2. **Real LAN:** repeat large transfers on the user's router, all-Wi-Fi and laptop-Ethernet configurations, and with WAN disconnected. No fixed LAN speed is promised. No firewall/router settings were changed.
3. **Physical clients:** Android/iOS suspension, Firefox/Safari and trust installation on other devices remain untested. Keep mobile transfers foregrounded. Browser download resume depends on the browser; use the companion for reliable folders.
4. Optional directory-write browser downloads, archives, a standalone executable and automatic retention are outside the implemented v1. The CLI supports 1/2/4 upload workers, bounded by server capacity; downloads use a continuous stream with 16 MiB checkpoints.
5. Fidelity covers bytes, normalized destination names and directory structure. ACLs, ownership, hard-link relationships, executable bits and destination timestamps are not preserved. Windows long-path and power-loss behavior remain OS/filesystem/hardware dependent.
6. Keep server storage and companion credentials private to the OS account. Re-pairing creates a new session. Revocation/expiry cancels unfinished old jobs, which the laptop owner can clean up in the desktop interface. Browser queue metadata grows with manifest size, although file bytes and rendered transfer rows remain bounded.

## Next operational step

Run `scripts/start.ps1`, pair a real LAN device and follow the hardware matrix in [BENCHMARKS.md](BENCHMARKS.md). There is no remaining planned v1 software feature waiting for implementation.
