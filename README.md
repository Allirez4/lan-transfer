# LAN Transfer

Resumable file transfers between your laptop and devices on your home network. FastAPI serves a locally bundled React interface; a Python companion handles reliable recursive transfers. No cloud account or internet connection is needed after installation.

## Laptop desktop interface

After setup, double-click **Open LAN Transfer.vbs** in this folder, or run `scripts/desktop.ps1`. The native Windows interface works alongside the browser interface and includes:

- Start/stop the server, open its browser interface, display a QR code, and generate pairing codes.
- Add existing files/folders with native pickers, refresh changed sources, and remove shares without deleting originals.
- List/unpair devices and pause, resume, cancel, or discard incomplete incoming jobs from any device.
- Pair with other servers, browse their shares, send files/folders, download, pause, and resume companion jobs.
- Edit network settings, upload concurrency, chunk size, device lifetime, and batch limits while the server is stopped.

Keep the window open or minimized while using its server. Closing asks whether to stop it. A server started separately from the command line keeps running when this window closes. The **Other servers → Transfer history** tab holds companion resume jobs; the **Incoming jobs** tab manages this laptop's receiving jobs. These are different job IDs.

Desktop file selection explicitly approves only the chosen source for sharing. It does not grant paired devices filesystem administration or expand `share_roots`. Shares created through the command line remain subject to `share_roots` on every download. See [desktop usage and security](docs/DESKTOP.md).

## Start on Windows

Requirements: **Python 3.12+**, **Node.js 22.12+**, and sufficient local disk space (NTFS recommended). From this project directory:

```powershell
.\scripts\setup.ps1
.\scripts\start.ps1
```

The terminal prints usable LAN URLs, a QR code, and a single-use pairing code valid for five minutes. Open a printed URL on another device on the same router and enter the code. FastAPI serves the production browser build and API on the same origin, without auto-reload.

Generate a fresh code while the server runs:

```powershell
.\backend\.venv\Scripts\lan-transfer-server.exe pair-code
```

Default configuration detects local IPv4 addresses and uses `storage/` in this project. For explicit settings, copy `config.example.json` to `config.json` and replace the example IP and share roots. Restart after changes. Exact hosts/origins protect cookie-authenticated mutations. Configuration, credentials and storage are ignored by Git.

**HTTP is for a trusted LAN:** credentials and files are not encrypted. SHA-256 detects corruption; it is not transport encryption. For sensitive transfers, follow [local HTTPS setup](docs/HTTPS.md).

## Send and receive

- Use **Send files**, **Send folder**, or file drag-and-drop. Verified files appear in **Shared on your laptop**.
- The queue separates sent bytes from durable committed bytes. After reload, select your batch and reselect the originals. Committed chunk hashes must match before upload continues.
- Click a shared file's download arrow for a browser download. Resume depends on the browser download manager; use the companion for reliable folders.
- Received batches live in `storage/received/<job-id>/`. Resolve conflicting names with **Use this name** or **Skip file/folder**, then **Start resolved batch**. Choices survive reloads and never rename your originals.
- An interrupted registration can be resumed by reselecting the full original selection. The app checks the expected entry count before starting.
- Canceling retains partial bytes. **Discard incomplete data** releases them after cancellation; completed files remain available.
- Folder pickers can omit empty directories. Use the companion to preserve them. Keep mobile transfers foregrounded and the screen awake.

## Desktop companion

Activate the environment to use the short command names:

```powershell
.\backend\.venv\Scripts\Activate.ps1
lan-transfer pair --server http://192.168.1.10:8000
lan-transfer upload "D:/Photos" --server http://192.168.1.10:8000
lan-transfer download <share-id> --to "D:/Received"
lan-transfer jobs
lan-transfer resume <local-job-id>
lan-transfer resume <local-job-id> --on-conflict rename
lan-transfer upload "D:/Photos" --workers 2
```

Pairing secrets are prompted. HTTPS validates certificates; add `--ca C:/path/to/root.crt` to `pair` for an explicitly trusted local CA. Credentials and durable jobs live in your profile's `.lan-transfer/` directory. Keep it private. `LAN_TRANSFER_CLIENT_HOME` selects a separate profile; one companion process locks each profile.

The printed companion job ID is the **local resume ID**. A share ID identifies a downloadable batch; find it in the browser's companion command or local share command. Conflicts prompt for rename/skip; for unattended work use `--on-conflict rename` or `--on-conflict skip` with upload, download or resume. Destination choices persist. Partial files stay under `.lan-transfer-parts`; resumed ranges and checkpoints are verified. Every final destination gets a full disk hash before publication.

On another desktop, install from a copy of the project with `python -m pip install .` in a virtual environment. Linux/macOS use the environment's `bin/activate`. Only the serving laptop needs the browser build. A standalone executable is not included.

## Share existing laptop files without copying

Add allowed directories to `share_roots` in local config.json, then:

```powershell
.\backend\.venv\Scripts\lan-transfer-server.exe --config config.json share "D:/Photos"
.\backend\.venv\Scripts\lan-transfer-server.exe sessions
.\backend\.venv\Scripts\lan-transfer-server.exe revoke <session-id>
```

Only local administration registers paths. Paired devices can download all published shares; upload jobs belong to their creating session. Keep shared sources unchanged. Hashes are prepared lazily, so startup does not hash the entire drive. Refresh or register a new share after modifying a source.

The desktop interface also registers paths locally. `lan-transfer-server shares`, `unshare <share-id>`, and `refresh-share <share-id>` manage published sources. `jobs`, `pause <job-id>`, `resume <job-id>`, `cancel <job-id>`, and `discard <job-id>` manage incoming batches locally, including abandoned ones. `stop` requests a graceful stop of a server started with `serve`. Local commands use the storage database; there is no network administration endpoint.

Device access expires after 30 days by default (`session_days`). Unpairing/expiry cancels unfinished jobs and releases their future-byte reservations; partial data remains until discarded locally. The server restricts client addresses to configured private/loopback networks and applies per-device job/entry limits. Keep Windows firewall rules scoped to your Private LAN. HTTP remains unencrypted; local HTTPS is available below.

## Troubleshooting

- **Cannot connect:** verify the same LAN, current laptop IP, VPN routing and router guest/client isolation. `0.0.0.0` is a bind address, not a browser URL.
- **Firewall:** use a trusted Windows Private network profile and allow the chosen TCP port for that profile only. This app does not change firewall rules, UPnP or router forwarding.
- **Sleep:** keep the laptop powered and awake. Phone locking or app switching can suspend browser transfers.
- **Host/origin rejected:** update exact hosts/origins after IP or port changes; do not use wildcards. A DHCP reservation helps keep URLs stable.
- **Disk errors:** free space for admitted bytes plus the safety margin. FAT32 cannot hold 4 GiB+ files. Windows long-path behavior depends on OS/filesystem configuration.
- **Expired/revoked session:** create a fresh local code and pair again. A new session cannot take over old session-owned upload jobs.
- **Retention:** canceled files retain their bytes until you choose **Discard incomplete data**. No automatic cleanup runs. See the recovery guide before local storage maintenance.

## Development and validation

```powershell
.\scripts\test.ps1
cd frontend
npm run test:e2e
```

Browser tests use installed Microsoft Edge and disposable loopback storage. Vite development uses a proxy to port 8000; add the exact development origin, for example `http://localhost:5173`, to config.json. Use the production start script for real transfers.

- [Current status and remaining gates](docs/STATUS.md)
- [Protocol v1](docs/PROTOCOL.md) and generated `/openapi.json`
- [Recovery and fidelity contract](docs/RECOVERY.md)
- [Browser support and actual tests](docs/BROWSERS.md)
- [Benchmark results and opt-in large tests](docs/BENCHMARKS.md)

Real **5 GB and 10 GB** loopback round trips passed with matching SHA-256 hashes. Two simultaneous uploads met the server's 256 MiB memory target. The 80 GB tree, physical mobile devices, actual Wi-Fi performance and WAN-disconnected LAN still need hardware validation; see the report for scope and measurements.
