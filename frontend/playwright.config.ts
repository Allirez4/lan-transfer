import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 60000,
  workers: 1,
  use: { baseURL: 'http://127.0.0.1:8765', channel: 'msedge', headless: true },
  webServer: {
    command: process.platform === 'win32' ? '..\\backend\\.venv\\Scripts\\python.exe ..\\tests\\browser_server.py' : '../backend/.venv/bin/python ../tests/browser_server.py',
    url: 'http://127.0.0.1:8765/api/v1/health',
    reuseExistingServer: false,
    timeout: 30000,
  },
  reporter: 'list',
})
