# Optional local HTTPS with Caddy

Normal operation requires no internet. Install Caddy and dependencies before disconnecting WAN. Caddy's internal CA provides local certificates; clients must explicitly trust its **public root certificate**. See [Caddy local HTTPS](https://caddyserver.com/docs/automatic-https#local-https).

The supplied configuration uses [`skip_install_trust`](https://caddyserver.com/docs/caddyfile/options#skip-install-trust), so certificate trust is an explicit setup step. A real loopback test passed through Caddy 2.11.4: trusted certificate validation, Secure/HttpOnly/SameSite cookies, upload, whole-file verification, download and ranges. An untrusted client correctly rejected the certificate. The test used an isolated CA directory and made no system trust changes. The Windows binary came from the official Caddy release and matched its published SHA-512 checksum.

1. Reserve the laptop's LAN address in the router's DHCP settings, for example `192.168.1.10`.
2. Copy `config.example.json` to `config.json`. Set `bind` to `127.0.0.1`, `secure` to `true`, keep backend `port` at `8000`, include `192.168.1.10` in `hosts`, and set `origins` to `["https://192.168.1.10"]`.
3. Edit `scripts/Caddyfile` to that address. Start the backend with `scripts/start.ps1`, then run `caddy run --config scripts/Caddyfile` in another terminal. Caddy forwards streams; this configuration does not buffer entire uploads or enable binary compression.
4. On the laptop, run `caddy environ` and inspect `caddy.AppDataDir`. For a typical interactive Windows account the public certificate is `%APPDATA%\Caddy\pki\authorities\local\root.crt`. A service account may use a different directory. Do not guess which CA issued the running server's certificate.
5. Transfer **only `root.crt`** to each client through a trusted channel. Confirm its SHA-256 fingerprint against the laptop (PowerShell `Get-FileHash -Algorithm SHA256 <certificate>`). Do not copy `root.key` or the CA directory; keep private keys restricted to the laptop owner/service account.
6. Trust the certificate as follows. Exact labels can vary by OS version; verify the final browser URL has no certificate warning.

| Client | Trust procedure |
|---|---|
| Windows Edge/Chrome | For the current user: `certutil -user -addstore Root .\root.crt`. Restart the browser. Remove later through `certmgr.msc` → Trusted Root Certification Authorities → Certificates, selecting this local Caddy root. |
| macOS Safari/Chrome | Open Keychain Access → login keychain → import root.crt → open certificate → Trust → Always Trust for SSL. Restart the browser. |
| iOS/iPadOS Safari | Open the transferred certificate → Settings → General → VPN & Device Management → install profile; then Settings → General → About → Certificate Trust Settings → enable full trust for that root. |
| Android Chrome | Settings → Security & privacy → More security settings → Encryption & credentials → Install a certificate → CA certificate; select the root. Names vary by vendor. Some apps do not accept user CAs. |
| Firefox desktop | Settings → Privacy & Security → Certificates → View Certificates → Authorities → Import → trust this CA to identify websites. |
| Linux desktop | Install the public root using your distribution's CA trust procedure; Firefox may need its own import. The CLI can use `--ca` directly. |
| Python companion | `lan-transfer pair --server https://192.168.1.10 --ca C:/path/to/root.crt`. The CA path persists with that server; certificate verification stays enabled. |

All listed OS trust procedures are setup guidance, not claims of execution on those devices. See the official [Windows certutil reference](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/certutil), [Apple certificate trust instructions](https://support.apple.com/en-ph/102390), and [Google Android certificate settings](https://support.google.com/googlepixeltablet/answer/2844832?hl=en). Do not disable certificate verification or treat a browser certificate warning as the completed setup. When the LAN IP changes, update the Caddy address, exact host/origin settings and saved server URL. Pair again at the new origin if necessary.

Only open the HTTPS listening port on the Windows **Private** network profile if needed. Do not disable the firewall, enable UPnP, or configure router port forwarding. The backend stays on loopback in this mode.

## Reproduce the TLS checks

`python -m pytest tests/test_https.py -q` creates a disposable test certificate and uses real TLS sockets. To also exercise Caddy, set `LAN_TRANSFER_TEST_CADDY` to the absolute path of a trusted Caddy executable before running that test. Neither mode disables certificate verification or installs a system CA.
