import { defineConfig } from "@playwright/test";
import { UI_ORIGIN } from "./devserver";

// Exercise exactly the assets shipped in the wheel, without a Vite proxy:
// VERITRACE_TEST_ORIGIN=http://127.0.0.1:8765 npx playwright test
const productionOrigin = process.env.VERITRACE_TEST_ORIGIN;

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
    baseURL: productionOrigin ?? UI_ORIGIN,
    // Locally, drive the Chrome that is already installed rather than
    // downloading a second browser. CI installs Playwright's own chromium, so
    // no channel is requested there.
    ...(process.env.CI ? {} : { channel: "chrome" }),
    headless: true,
    viewport: { width: 1600, height: 900 },
  },
  webServer: productionOrigin ? undefined : {
    command: "npm run dev",
    url: UI_ORIGIN,
    reuseExistingServer: true,
    timeout: 60_000,
  },
});
