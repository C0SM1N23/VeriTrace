/**
 * Where the Vite dev server listens.
 *
 * One value, read by `vite.config.ts` and by `playwright.config.ts`, because a
 * dev server on one port and a `baseURL` on another is a five-minute puzzle
 * every time.
 *
 * Overridable, and that is the point. Windows reserves blocks of ports for
 * Hyper-V/WinNAT and **re-randomises them on reboot** — `netsh interface ipv4
 * show excludedportrange protocol=tcp` prints the current set. A reserved port
 * fails to bind with `EACCES` and no listener anywhere, which reads like a
 * broken config rather than an occupied port. When that happens the fix is one
 * environment variable, not an edit in two files:
 *
 *     VERITRACE_UI_PORT=5500 npx playwright test
 */
export const UI_PORT = Number(process.env.VERITRACE_UI_PORT ?? 5400);
export const UI_ORIGIN = `http://127.0.0.1:${UI_PORT}`;
