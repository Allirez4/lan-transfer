# Antigravity build brief: reliable local file transfer with FastAPI and React

Give this entire file to your coding model in Antigravity. The instructions below are addressed to that model. This is an implementation specification, not a claim that the application has already been built or benchmarked.

## 1. Objective and working assumptions

Build a working application called **LAN Transfer**. My laptop is the server; my devices connect through my home Wi-Fi router. Support 5 GB and 10 GB individual files and folders totaling 80 GB or more, in both directions. Prioritize correct bytes, recovery after interruptions, sustained throughput, and simple setup, in that order.

Assume a Windows laptop first. Keep Python filesystem code portable to Linux/macOS. Client operating systems are not yet specified, so support ordinary desktop/mobile browsers for basic transfers and document each platform's actual capabilities. Use FastAPI and React with TypeScript. No cloud account, external relay, internet connection during normal operation, subscription, or public hosting is needed.

Files travel between a client and the laptop. A transfer from device A to device B through the laptop requires upload and download and uses laptop storage; do not describe it as direct peer-to-peer transfer. The laptop must remain powered on and awake.

Produce the application, tests, setup scripts, user guide, and honest benchmark report. Read this brief completely, inspect the workspace, record assumptions, then implement the phases in order. Resolve routine engineering decisions yourself. Ask only when missing information blocks implementation; do not stop after proposing another plan.

## 2. Architecture

| Component | Implementation |
|---|---|
| Web interface | React, TypeScript, Vite; responsive desktop/mobile interface; locally bundled assets |
| API and transfer service | FastAPI, compatible pinned Starlette, Uvicorn |
| Persistent state | SQLite on a local disk, WAL mode, durable transactions; metadata only |
| File storage | Ordinary filesystem, with incomplete files in a private staging area on the destination volume |
| Upload transport | Raw HTTP request bodies in bounded, resumable chunks |
| Download transport | HTTP streaming with byte ranges and stable source versions |
| Browser progress | XHR upload progress plus authoritative server status; poll active jobs initially |
| Browser hashing | Incremental SHA-256 in a Web Worker using a maintained locally bundled implementation |
| Reliable desktop client | Small Python CLI using the same API for recursive, resumable uploads/downloads |
| Local HTTPS | Optional Caddy configuration with a local CA and documented client trust setup |

Start with one Uvicorn worker and a process-level lock preventing two server instances from opening the same storage/state directory. Per-upload locks and bounded offloaded disk operations are sufficient for the initial architecture. Do not increase workers without redesigning shared locks and job ownership. Support Windows' available event loop; do not require uvloop.

Serve the built frontend and API from the same origin. Bind to the selected LAN interface or `0.0.0.0` in LAN mode. Print the actual LAN URLs; `0.0.0.0` is a bind address, not a URL users should open. Use a Vite proxy only in development. Do not use the development server or auto-reload for real transfers.

Do not introduce Redis, Celery, PostgreSQL, Kubernetes, WebRTC, a custom UDP protocol, or Docker as a prerequisite. Durable background verification can be a bounded in-process queue whose jobs are recorded in SQLite and recovered at startup.

## 3. Define browser support honestly

| Operation | Required behavior |
|---|---|
| Browser upload of individual files | Bounded chunks, pause/resume, retry after network loss |
| Browser folder upload | Use a folder picker where supported and preserve relative paths; provide multi-file fallback |
| Resume an upload after page reload | Restore metadata, then request file/folder reselection or renewed handle permission; verify source identity before continuing |
| Browser single-file download | Use a normal authenticated download link so the browser writes to disk; server supports ranges, but browser-managed resume remains browser-dependent |
| Browser folder download | Optional enhanced mode using directory write APIs in a supported secure context; completed files survive, but an interrupted active file may need to restart |
| Reliable large-folder download on desktop | Python companion preserves the tree, checkpoints partial files, retries, and verifies hashes |
| Mobile background transfers | Best effort only; document that screen locking, app switching, or OS suspension can interrupt a browser transfer |

Folder upload through `webkitdirectory` exposes relative file paths, but a file-only picker cannot faithfully describe every empty directory. Preserve empty directories when the selected API/CLI can enumerate them; explicitly disclose that limitation in fallback mode. [MDN folder input documentation](https://developer.mozilla.org/en-US/docs/Web/API/HTMLInputElement/webkitdirectory).

Do not assume that `showDirectoryPicker()` is available everywhere. Feature-detect it and check `window.isSecureContext`. A remote client opening `http://192.168.x.x` does not receive the localhost secure-context exception. [MDN directory picker](https://developer.mozilla.org/en-US/docs/Web/API/Window/showDirectoryPicker), [MDN secure contexts](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Secure_Contexts).

Browser file writes may remain in temporary storage until the writable stream closes. Reopening with `keepExistingData` can copy existing data. Therefore, do not promise crash-safe byte-offset browser downloads merely because `seek()` exists, and do not checkpoint by repeatedly copying a growing 10 GB file. The CLI is the required robust desktop path. [MDN writable file behavior](https://developer.mozilla.org/en-US/docs/Web/API/FileSystemFileHandle/createWritable).

## 4. Upload protocol and invariants

Implement a small, explicitly versioned application protocol with sequential offsets. The established tus protocol is a useful reference for offset reconciliation. Our extra atomic-chunk and checksum semantics are custom: do not claim tus compatibility or use a tus client unless you actually implement and test the specification. [tus protocol](https://tus.io/protocols/resumable-upload).

Start with a **16 MiB chunk size**, configurable to 4, 8, 16, or 32 MiB. Fix the chunk size for the life of a file transfer. Start with **one request at a time per file**, up to **two active file transfers globally**. Use one active transfer in the HDD profile. These are starting values to benchmark, not universal optimal settings.

1. Create a folder/batch job with an idempotency key. Register file metadata in bounded pages, e.g. 500 entries, rather than one enormous request. Seal the manifest before treating the folder as completely described.
2. Register each file with relative path, exact byte length, last-modified metadata, and optional known SHA-256. The server allocates an opaque upload ID and a private `.part` file. Never use a user-supplied path as the staging filename.
3. The client queries the server's committed offset before starting or resuming. Server state is authoritative.
4. Send `application/octet-stream` containing `File.slice(offset, end)`, with offset, declared chunk length, chunk SHA-256, and a unique request/idempotency identifier.
5. The server acquires that upload's lock and validates ownership, state, offset, exact permitted length, and total-size bounds before writing.
6. Consume `request.stream()` incrementally. Hash and write bounded pieces to the existing `.part` file. Offload blocking writes/fsync/hash-heavy work through bounded workers; never accumulate the whole request or file. Await storage capacity to apply backpressure. Starlette exposes a streaming request interface. [Starlette requests](https://starlette.dev/requests/).
7. A chunk is committed only after exact length and SHA-256 match. Flush and fsync the data, then commit the new offset and chunk receipt in SQLite, then acknowledge. Use a durable SQLite setting appropriate to this order. A client progress event is not a durability acknowledgement.
8. On incomplete request, timeout, wrong length, or wrong hash, return to the prior committed offset and truncate uncommitted data. Make cleanup cancellation-safe and recover the same invariant after a process kill.
9. On a lost acknowledgement, an identical retry must never append twice. Return its recorded result, or an offset-conflict response that causes reconciliation. Reject conflicting bytes or out-of-order writes without modifying committed data. Serialize competing tabs/clients for the same upload.
10. Finalize only at the exact declared size and after final checksum verification. Move from staging to the final path on the same volume without silently replacing an existing file. Only then expose it for download.

Record `finalizing` durably before the final move, and make startup recovery handle a crash before or after that move. Recovery must reconcile the database and filesystem rather than deleting an apparently orphaned completed file. Document platform limits of power-loss durability; do not claim immunity to faulty storage hardware.

On restart, truncate bytes beyond the last committed offset. If a staging file is shorter than the recorded offset, mark it as requiring repair rather than claiming the missing bytes exist. Preserve chunk receipts needed to validate resumed sources. Protect in-progress transfers against cleanup jobs.

## 5. Integrity and source identity

Use SHA-256 for each chunk. Compute the source's whole-file SHA-256 incrementally in a worker while reading/uploading the file; do not require a complete 80 GB prehash before sending the first byte. Maintain bounded read-ahead. Retry a chunk without counting its bytes twice in the whole-file hash.

At finalization, stream-read the staged file from disk to calculate its whole-file SHA-256 and compare it with the source digest. Verification is a real extra disk pass: show a separate **Verifying** state and include it in end-to-end completion time. Run at most one expensive verification job initially, balancing it against disk transfer load. Do not silently disable verification to improve speed figures.

After a browser refresh, name, size, and modification time are only matching hints. Reconstruct hashing state by rereading the selected file's committed prefix and comparing stored chunk digests before appending; continue the incremental hash for the suffix. Show **Checking selected source** while doing this. A changed source must not produce a mixed old/new output.

The same distinction applies to downloads: use a pinned source version and expected SHA-256, not a filename alone. A metadata-based ETag is a version validator, not an integrity checksum. Reject/restart a transfer if its registered source has changed. Verify final destination bytes before marking a CLI transfer complete.

## 6. Storage and folder behavior

- Store bytes once in their staging/final location; do not create thousands of chunk files and concatenate them into a second full-size file.
- Keep staging on the destination volume so final publication does not require another full copy.
- Check free space when admitting a job and periodically during transfer. Account for outstanding admitted bytes across concurrent jobs and keep a configurable safety margin. Still handle an actual disk-full write failure, since other programs can consume space.
- Warn about filesystems with insufficient single-file size limits, including FAT32. Detect where feasible and surface a clear actionable error otherwise.
- Folder success requires all registered files/directories to finish or be explicitly skipped; never report full success with hidden failures.
- Persist queue order, paths, sizes, offsets, receipts, errors, and verification status. Store file content in neither SQLite nor browser IndexedDB/localStorage.
- Preserve Unicode/Persian filenames and nested structure. Handle zero-byte files, hidden files, and long paths. Source ACLs, ownership, hard-link relationships, and executable bits are outside v1's portable fidelity contract; document what is preserved.
- Default conflicts to an explicit skip/rename decision with a stable mapping. Never silently overwrite existing files. Detect duplicate normalized paths and Windows case-insensitive collisions before transfer.
- Reject path traversal, absolute paths, drive/UNC paths, NULs, Windows device names/alternate streams, and escapes through symlinks or junctions. Validate before access on both server and CLI. Do not follow directory symlinks in v1.
- Do not delete source files. Keep incomplete transfers until the user cancels/deletes them or an explicitly configured retention policy applies.

## 7. Download implementation and laptop file sharing

Allow the laptop owner to register existing local files/folders for sharing **without uploading or copying them back into the server**. Configure allowed share roots locally using configuration/CLI; remote clients cannot register arbitrary laptop paths. Serve only registered IDs within those roots.

Use Starlette `FileResponse` initially for file downloads. Verify the installed version's behavior for `HEAD`, `Range`, `If-Range`, `206`, `416`, `Content-Range`, `Content-Length`, ETag, and attachment filenames; implement only missing required behavior. Framework support for byte ranges is documented, but the actual installed combination must pass integration tests. [Starlette responses](https://starlette.dev/responses/).

Create/cache per-file source hashes lazily when files are prepared for reliable downloading. Avoid hashing an entire shared drive on startup. Pin a file's transfer version and prevent app-managed replacement during active reads. Detect changes to externally managed files and abort safely; require users to keep shared sources unchanged while transferring.

For normal browser downloads, use a same-origin authenticated link with `Content-Disposition: attachment`. Do not call `response.blob()` or `arrayBuffer()` on a complete multi-gigabyte response.

Implement a small installable Python CLI with these intended commands, adapting flags consistently in documentation:

```text
lan-transfer pair --server https://<laptop-address>:<port>
lan-transfer upload <local-path> --server <server-url>
lan-transfer download <share-id> --to <local-directory>
lan-transfer resume <job-id>
```

Prompt for pairing secrets instead of putting long-lived tokens in command-line arguments. Reuse HTTP connections and stream to `.part` files. Keep a local durable job manifest with expected version/hash and verified checkpoint offsets. Flush file data before recording a checkpoint. On recovery, validate the checkpoint and truncate any uncommitted suffix.

Resume with `Range` plus a source-version validator. If a resumed request returns `200` instead of the expected `206`, never append that full response to a partial file. Validate `Content-Range`, length, and version before accepting bytes. Handle `416` by reconciling sizes/state rather than assuming completion. Verify whole-file SHA-256, then publish without replacing unrelated files. Completed, verified files are skipped on a resumed folder job.

The CLI should validate the HTTPS certificate or use the explicitly trusted local CA. Do not default to disabled certificate verification. Make Windows installation and startup simple; document Python requirements. A packaged executable can follow after the CLI is correct.

Optional browser folder downloads must stream each file to a user-selected destination, close/commit it, and track completed files. Do not advertise byte-level recovery after browser termination until it is genuinely implemented and tested. Do not use browser private storage to stage 80 GB by default.

Archive download is optional. A streamed ZIP64 with compression disabled may be useful for convenience, but live-generated archives are not the required resumable solution. Do not create a giant in-memory ZIP or compress videos/game archives by default. A prepared archive needs extra disk space and a stable file for reliable range downloads.

## 8. Connectivity, authentication, and offline operation

Ship a straightforward trusted-LAN HTTP mode and a documented HTTPS mode. HTTP mode must clearly state that pairing credentials and files are not protected by application-level TLS; hashes detect corruption, not an active attacker. Sensitive transfers should use HTTPS.

For HTTPS, provide a Caddy local-CA configuration and exact steps to trust its public root certificate on each supported client. A self-signed certificate warning is not a substitute for a correctly trusted secure origin. Protect the CA private key and never bundle it with client instructions. [Caddy local HTTPS](https://caddyserver.com/docs/automatic-https).

Use locally initiated, short-lived, single-use pairing codes with rate limits, issuing revocable random session credentials. Use HttpOnly, SameSite cookies for browser sessions and Secure cookies in HTTPS mode; apply CSRF/origin protection to cookie-authenticated mutations. Store server-side credential hashes, not plaintext tokens. Persist server identity/session configuration across restarts. Protect file listing, status, downloads, and transfer operations as well as upload endpoints.

Use exact permitted Host/origin values, avoid wildcard credentialed CORS, and avoid logging tokens or pairing codes in access logs. Shared content must download as attachments with appropriate content-type protections, rather than execute uploaded HTML on the app's origin. Bound JSON manifests, active jobs, request lengths, and open connections without imposing a small total-file limit.

Bundle scripts, fonts, icons, hashing libraries, and styles locally. After installation, unplugging the router's internet/WAN connection must not stop transfers between LAN devices. No telemetry or CDN requests during normal operation.

Print troubleshooting guidance for the Windows Private network firewall rule, VPN routing, wrong IP/interface, guest Wi-Fi/client isolation, laptop sleep, and DHCP address changes. Never disable the firewall, enable UPnP, or open router port forwarding automatically. Generate a QR code for the local address; keep secrets out of permanent URLs.

## 9. React interface

Build a usable interface around real transfer state:

- Pair/connect screen and device/capability status.
- Send files / Send folder controls with drag-and-drop as an enhancement.
- Shared files/folders list and download actions.
- Transfer queue with relative path, total size, sent bytes, committed bytes, speed, ETA, retries, and per-file errors.
- Pause, resume, retry, cancel, and reconnect controls; folder-level and file-level progress.
- Clear states: scanning, queued, checking source, transferring, paused, reconnecting, verifying, completed, failed, canceled.
- Disk-space status and actionable setup/error messages.
- Simple instructions for the desktop companion when reliable folder downloading is requested.

Throttle UI updates to roughly 4 times per second and virtualize long file lists. Calculate throughput with a moving average. Keep speculative upload progress separate from acknowledged committed progress; a retry must not double-count transferred success. Never show completed while verification is still running.

Persist metadata in IndexedDB, but not entire files. Restore jobs after reload and ask for reselection/permission when necessary. Cancel requests with AbortController or XHR abort; then reconcile server status. Make backoff exponential with jitter and a maximum delay; honor server retry guidance and pause on disk/auth/permanent errors rather than retrying forever.

## 10. Performance plan

Optimize from measurements. Local transfer speed depends on Wi-Fi contention, signal, router and adapter capability, Ethernet link speeds, disks, CPU/hash throughput, and other workloads. Internet subscription speed is not the LAN benchmark.

Prefer the laptop connected by gigabit-or-faster Ethernet when available, with clients using a strong 5/6 GHz connection where supported. Also test the all-Wi-Fi arrangement the user actually has. Do not promise a fixed MB/s or a fixed improvement from cabling.

Use sustained network testing with iperf3 where available, plus disk read/write and checksum-throughput measurements. iperf3 measures network throughput; it is not a file-copy or disk benchmark. On Windows, document the actual binary provenance or an alternative, rather than claiming ESnet supplies an official Windows build. [ESnet iperf3 documentation](https://software.es.net/iperf/).

Benchmark 4/8/16/32 MiB upload chunks and 1/2/4 active files. Start with sequential chunks for a single file. Consider parallel ranges only if profiling demonstrates a bottleneck that warrants the recovery and random-write complexity. Never assume more connections are faster.

Keep HTTP connections alive. Disable binary response compression. Avoid multipart spooling followed by a second copy, unbounded async queues, per-packet logs, and full-response/request buffering. Batch disk writes into useful bounded buffers instead of tiny writes. Include any reverse proxy in real performance tests and avoid whole-request buffering there.

Track backend RSS, browser/CLI memory, CPU, disk usage, network throughput, verification time, and total wall-clock time. Target memory independent of file size, with a starting backend goal below 256 MiB during two active transfers; measure process memory separately from the OS filesystem cache. Explain any target miss rather than concealing it.

Use a large-file transfer throughput goal near the measured storage/network/hash limit; investigate results below roughly 70% of that measured limit. This is an investigation threshold, not an acceptance guarantee. Report strict verification time separately and in the final total. Do not remove durability fsyncs silently; any later faster checkpoint policy must explicitly state how much progress can be resent after a crash.

Illustrative transfer-only times, using decimal GB and sustained useful MB/s:

| Sustained useful speed | 10 GB | 80 GB |
|---|---:|---:|
| 10 MB/s | 16 min 40 sec | 2 hr 13 min 20 sec |
| 30 MB/s | 5 min 33 sec | 44 min 27 sec |
| 60 MB/s | 2 min 47 sec | 22 min 13 sec |
| 100 MB/s | 1 min 40 sec | 13 min 20 sec |

These are arithmetic examples, not predictions for this router. Scanning, retries, small-file overhead, and verification add time. Label MB/s versus Mbps accurately; 8 bits equal 1 byte.

## 11. API and repository outline

Implement these responsibilities; finalize exact schemas in OpenAPI and the protocol document before coding clients:

| Endpoint area | Responsibility |
|---|---|
| `/api/v1/health`, `/capabilities` | Health and supported transfer/browser configuration |
| `/api/v1/pair`, `/sessions` | Pairing, session lifecycle and revocation |
| `/api/v1/jobs` | Create/list jobs; bounded manifest registration; seal/pause/cancel |
| `/api/v1/uploads` | Idempotent file registration |
| `/api/v1/uploads/{id}` | Authoritative offset/state and paginated chunk receipts |
| `/api/v1/uploads/{id}/data` | Atomic sequential raw chunk append |
| `/api/v1/uploads/{id}/complete` | Idempotent durable verification/finalization job |
| `/api/v1/shares` | Authorized listings and paginated versioned manifests |
| `/api/v1/files/{id}/content` | Authenticated HEAD/GET and range downloads |
| `/api/v1/files/{id}/metadata` | Size, source version, hash readiness and expected digest |

Separate `backend/`, `frontend/`, `client/`, `tests/`, `scripts/`, and `docs/`. Keep storage access, transfer protocol, metadata repository, auth, verification, and API routes as testable modules. Use dependency lockfiles and record actual compatible versions instead of guessing version numbers in advance.

## 12. Build phases and gates

**Phase 1 — Foundation and one real transfer.** Scaffold the project, configure local storage and pairing, serve React from FastAPI, upload a real file by streamed chunks, download it, and compare bytes. Include basic path confinement from the start. Deliver Windows setup/start scripts.

**Phase 2 — Recovery and verification.** Implement persistent offsets, chunk receipts, retries, source reselection checks, final hashing, disk-full handling, startup repair, and crash-safe finalization. Gate: disconnect mid-chunk, lose an acknowledgement, and kill/restart the server without corrupting or duplicating bytes.

**Phase 3 — Folders and daily usability.** Implement paginated manifests, nested paths, conflicts, large queue UI, pause/resume, local share registration, and offline operation. Gate: a mixed tree transfers faithfully and resumes without retransmitting completed files.

**Phase 4 — Reliable downloads and client compatibility.** Implement the Python companion and range-resume tests. Add capability-based browser downloads and HTTPS instructions. Gate: restart a partially completed recursive desktop download and finish with matching paths, sizes, and SHA-256 values.

**Phase 5 — Measure, tune, and package.** Run realistic large-file tests, tune bounded buffers/concurrency, finish troubleshooting and operational docs. If real hardware or space is unavailable, provide a runnable manual benchmark procedure and clearly mark those results as unmeasured.

Keep `docs/STATUS.md` updated with completed behavior, passing tests, known limitations, and the next task. Continue through the phases; do not report the project finished after a polished UI or a small-file demo.

## 13. Required validation

Automate small fixtures for protocol correctness, and provide opt-in large tests with explicit disk requirements. Never allocate an unexpected 80 GB test dataset by default.

| Test | Required result |
|---|---|
| Real 5 GB and 10 GB upload/download | Exact byte lengths and matching whole-file SHA-256 |
| Real 80 GB nested folder | Correct paths, counts, sizes, and hashes; no skipped failures hidden as success |
| Thousands of small files plus large files | Bounded memory, responsive queue, correct folder completion |
| Network disconnect mid-chunk | Resume from a verified committed point |
| Acknowledgement lost after commit | Retry cannot duplicate data |
| Server/client killed at commit boundaries | Restart reconciles file and metadata safely |
| Server killed around final rename | Recovery identifies the correct final state |
| Disk full and read/write errors | Actionable failure; committed progress and source files preserved |
| Wrong checksum or truncated/oversized body | Reject corruption without advancing the offset |
| File changed before resume | Detect mismatch; do not combine different sources |
| Two tabs write to the same upload | No overlapping writes or conflicting commits |
| Range download, source change, 200/206/416 cases | Correct restart/reconciliation; never append a full response to partial data |
| Persian/Unicode, empty files, collisions, traversal | Faithful supported names; unsafe/ambiguous paths rejected clearly |
| Pairing expiry, revoked sessions, cross-origin requests | Unauthorized transfer/listing operations rejected |
| Internet disconnected but LAN working | App loads and transfers without external requests |
| Target desktop/mobile browsers | Document actual capability and suspension behavior |

Real performance data must use materialized bytes representative of actual files. Sparse or all-zero files are useful for limited logical tests but can produce misleading throughput results. Do not call those an 80 GB real-storage benchmark. Do not claim tests on devices or operating systems that you did not actually run.

The final handoff must contain working source, lockfiles, setup/start commands, configuration example, protocol and recovery notes, browser capability table, meaningful test results, measured benchmarks or a clearly labeled manual procedure, and a short list of remaining limitations.
