# Browser and client support

| Operation | Implementation | Validation in this workspace |
|---|---|---|
| Individual browser upload | XHR bounded chunks, worker SHA-256, per-file/batch pause and retry | Automated Edge checks, including real materialized byte comparison |
| Folder browser upload | `webkitdirectory`, relative paths, paged registration | Edge test covers nested Persian paths, hidden and zero-byte files |
| Reload/resume | Persist selected job metadata in IndexedDB, reselect source, compare every committed chunk digest | Edge tests cover lost manifest acknowledgement, reload during verification, interruption/reselection and changed-source rejection |
| Conflicts and controls | Persistent rename/skip, per-file/batch pause/retry/cancel, explicit discard | Edge tests cover conflict decisions, cleanup and retry while another file reconnects |
| Single-file download | Authenticated ordinary attachment link, server byte ranges | Server integration tests; browser resume depends on browser download manager |
| Browser folder download | Desktop companion instructions | Enhanced directory-write mode is not implemented |
| Reliable folder download | Python CLI: pinned versions, checkpoints, retries, SHA-256, no-replace publication | Automated mixed-tree interruption/resume test on Windows |
| Empty directories | CLI enumerates and preserves them | Automated |
| Mobile background transfer | Best effort; keep screen awake and page foreground | Real Android/iOS suspension not tested |

Edge desktop automation with a 390-pixel viewport is responsive-layout testing, not proof of Android/iOS compatibility. Firefox, Safari and real mobile devices remain untested. Large production transfers should use the desktop companion until your devices have passed the manual acceptance checks.

Browser reads/hashes bounded chunks per active file plus queue metadata; actual browser process memory is not yet profiled. File contents are not placed in IndexedDB, localStorage, browser private storage, or a full-response Blob. The transfer list is virtualized. Queues retain file metadata in memory; shared file lists have 500-entry next/previous pages. The dashboard loads every job/share page. If IndexedDB is denied, live transfers still work and authoritative jobs remain on the server, but local selection hints may be lost on reload. Folder pickers cannot enumerate every empty directory; use the CLI when those matter.

Local HTTP is not a secure origin on another device. Web Crypto `getRandomValues` and the bundled WASM hash worker support the basic upload flow without `crypto.subtle` or `randomUUID`. Enhanced filesystem APIs are not assumed. Browser-managed downloads write to the browser's chosen destination and do not provide application-controlled durable offsets.

References: [folder input](https://developer.mozilla.org/en-US/docs/Web/API/HTMLInputElement/webkitdirectory), [directory picker](https://developer.mozilla.org/en-US/docs/Web/API/Window/showDirectoryPicker), [secure contexts](https://developer.mozilla.org/en-US/docs/Web/Security/Defenses/Secure_Contexts), [writable-file commit behavior](https://developer.mozilla.org/en-US/docs/Web/API/FileSystemFileHandle/createWritable). These are capability references; the tested platform list above is deliberately narrower.
