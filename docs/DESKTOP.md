# Laptop interface

Run setup once, then double-click `Open LAN Transfer.vbs`, use `scripts/desktop.ps1`, or launch `backend/.venv/Scripts/lan-transfer-desktop.exe` from the project directory. A custom configuration can be selected with `scripts/desktop.ps1 -Config C:/path/config.json`. Python must include Tk (the standard Windows Python installation does).

## Server and sharing

Start the server on the Server tab. Use its address or QR code on another LAN device and generate a single-use five-minute pairing code. The browser and native interface can be used together. Starting the desktop app alone does not start a listening server.

**Add files** and **Add folder** register the originals in place. No staging copy is made. Local picker approval covers precisely the selected file/folder; it does not broaden the command-line share root allowlist. Every paired device can read every published share.

**Refresh selected source** rescans the selected share atomically and assigns fresh file IDs. Use it after changing a source. Existing downloads must start again against the refreshed versions. If the source moved, remove the old share and add its new location. A failed refresh leaves the previous registration intact. **Remove from sharing** disables downloads and never deletes the originals or received files. In-flight responses check access between bounded sends; bytes already delivered cannot be recalled.

## Devices and incoming jobs

Unpairing revokes the credential and cancels unfinished incoming jobs. Expired credentials are rejected immediately, and maintenance reclaims their jobs. Completed files remain shared; incomplete bytes remain available for explicit cleanup. The laptop owner can cancel and then discard abandoned jobs, including jobs whose device is no longer paired. Browser devices cannot administer another device's jobs.

Local commands are durable records in the storage database. When online, the server processes them with the same file locks as browser operations. When offline, the desktop/CLI obtains the exclusive server lock before processing them. Pending commands survive restart. Activity shows completion or failure.

Stopping the server interrupts active connections; durable checkpoints remain resumable. Closing a window that started the server asks to stop it; keep it open or minimized to keep sharing. A separately started server is unaffected by closing the desktop window.

## Companion transfers

On **Other servers**, enter the other server's address and pairing code. For HTTPS, select its trusted public CA certificate when needed. Certificate verification stays enabled; the app does not install trust roots or bypass certificate errors.

Load shares to browse available downloads, or send a file/folder. Select rename/skip for existing names and 1/2/4 upload workers (bounded by the receiving server). Downloads stream through bounded buffers and commit durable checkpoints every 16 MiB. Pausing stops only the companion process launched by this window, retaining its state. Select the job in **Transfer history** and resume. One process at a time uses a companion profile, shared with the existing CLI.

The activity log shows progress/errors. Credentials remain in the companion profile, not the configuration or activity log. Environment HTTP proxies are ignored for these direct LAN transfers.

## Network settings

Stop the server before saving settings. Configuration paths are resolved relative to the configuration file. Upload chunk sizes remain 4/8/16/32 MiB. Uploads and downloads have separate bounded slot pools so stalled downloads cannot consume all receiving capacity. Stalled response writes time out after 30 seconds by default.

Defaults admit loopback, RFC1918 IPv4, IPv4 link-local, IPv6 ULA and IPv6 link-local addresses. Add an explicit VPN subnet to `allowed_networks` only if wanted. No router/firewall settings are modified. The Host/Origin allowlists remain separate protections, not substitutes for network restrictions.

HTTP remains an explicit trusted-LAN mode. HTTPS requires the documented local reverse proxy, backend binding `127.0.0.1`, exact HTTPS origins, and client certificate trust. See [HTTPS.md](HTTPS.md). Device credentials expire after `session_days` (default 30), including credentials created before this update. The default quotas are 20 open jobs per device, 100,000 entries per batch, and 200,000 registered entries per session across its history. Manifest replay does not consume the quota twice.

Back up the storage directory before manual maintenance. The schema additions preserve existing jobs and shares; removing a configured share root now prevents downloads of its command-line shares. Native picker shares use their explicit local approval until removed.
