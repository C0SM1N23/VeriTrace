import { defineConfig } from "@playwright/test";

/**
 * Drives the Chrome already installed on the machine (`channel: "chrome"`)
 * rather than downloading Playwright's own browser build.
 *
 * The backend is expected to be serving `designs/fifo_async/dump.vtx` on 8765;
 * `npm run test:e2e` starts the Vite dev server, which proxies to it.
 */
export default defineConfig({
  testDir: "./tests",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://127.0.0.1:5400",
    // Locally, drive the Chrome that is already installed rather than
    // downloading a second browser. CI installs Playwright's own chromium, so
    // no channel is requested there.
    ...(process.env.CI ? {} : { channel: "chrome" }),
    headless: true,
    viewport: { width: 1600, height: 900 },
  },
  webServer: {
    command: "npm run dev",
    url: "http://127.0.0.1:5400",
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
