# LAN Transfer frontend

React/TypeScript/Vite interface served by FastAPI in production. See the [project guide](../README.md) for pairing, setup and transfer behavior.

- `npm ci` installs the exact lockfile.
- `npm run build` type-checks and builds local assets.
- `npm run lint` checks source.
- `npm run test:e2e` tests real browser transfers using installed Microsoft Edge and a disposable loopback Python service.
- `npm run dev` enables the development proxy to backend port 8000. Configure the exact localhost:5173 origin in the backend. Do not use this server for real transfers.

SHA-256 runs incrementally in a locally bundled Web Worker; only bounded chunks and metadata live in browser memory. The visible file queue is virtualized. Local HTTP does not require secure-context-only filesystem or Web Crypto hashing APIs.
