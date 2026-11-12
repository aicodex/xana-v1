import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests', workers: 1, timeout: 30000,
  use: {
    baseURL: 'http://127.0.0.1:18899',
    launchOptions: process.env.XANA_TEST_CHROMIUM ? { executablePath: process.env.XANA_TEST_CHROMIUM } : {},
  },
  webServer: {
    command: `PYTHONPATH=src:. ${process.env.XANA_TEST_PYTHON ?? 'python'} web/tests/server.py`,
    cwd: '..', url: 'http://127.0.0.1:18899/healthz', reuseExistingServer: false,
  },
})
