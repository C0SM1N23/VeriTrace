/**
 * The app mounts, and mounts quietly.
 *
 * This exists because of a bug that broke every other spec at once and none of
 * them said why. A store selector written `useWave((s) => s.checks?.tables ?? [])`
 * builds a **new array on every render**, so zustand's snapshot never compares
 * equal, React re-renders, and the component loops until it throws "Maximum
 * update depth exceeded". The application never paints. Seventy tests then fail
 * on their own first assertion — `status-range` not found, `tab-3` not found —
 * and every one of those messages points somewhere other than the cause.
 *
 * So: one test that opens the page and reads the console. It is the cheapest
 * test in the suite and the only one that would have named that bug.
 */

import { expect, test } from "@playwright/test";
import { waitForReady } from "./session";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

test("opening progress waits before fetching the real waveform", async ({ page, request }) => {
  const root = await (await request.get(`${BACKEND}/`)).json();
  const sid = root.default_session;
  await waitForReady(request, BACKEND, sid);
  let release = false;
  let prematureReads = 0;
  page.on("request", (req) => {
    if (!release && /\/session\/[^/]+\/(signals|layout)/.test(req.url())) prematureReads++;
  });
  // Only delay readiness; all waveform, hierarchy, and layout responses still
  // come from the real server and native store once the gate opens.
  await page.route(`**/session/${sid}/status`, async (route) => {
    if (release) await route.continue();
    else await route.fulfill({ json: { phase: "indexing", progress: 0.42 } });
  });
  await page.goto(`/?session=${sid}`);
  await expect(page.locator(".loading")).toContainText("indexing: 42%");
  expect(prematureReads).toBe(0);
  release = true;
  await expect(page.locator(".loading")).toHaveCount(0);
  await expect(page.locator('[data-testid="signal-row"]').first()).toBeVisible();
  await expect(page.locator('[data-testid="status-range"]')).not.toBeEmpty();
});

test("opening failure displays the backend's actual error", async ({ page, request }) => {
  const root = await (await request.get(`${BACKEND}/`)).json();
  const sid = root.default_session;
  await page.route(`**/session/${sid}/status`, (route) => route.fulfill({
    json: { phase: "error", progress: 0.2, error: "Malformed waveform: missing enddefinitions" },
  }));
  await page.goto(`/?session=${sid}`);
  await expect(page.locator(".fatal-body")).toHaveText("Malformed waveform: missing enddefinitions");
  await expect(page.locator(".loading")).toHaveCount(0);
});

test("the application mounts with no console errors", async ({ page, request }) => {
  const r = await request.post(`${BACKEND}/session`, {
    data: { trace_path: "designs/fsm/dump.vcd.vtx", rtl_paths: ["designs/fsm"] },
  });
  expect(r.ok(), await r.text()).toBeTruthy();
  const session = (await r.json()).session_id as string;

  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });

  await page.goto(`/?session=${session}`);
  await expect(page.locator(".loading")).toHaveCount(0);
  await expect(page.locator('[data-testid="status-range"]')).not.toBeEmpty();
  await expect(page.locator(".tab-strip [role=tab]").first()).toBeVisible();

  // React's dev build reports a bad selector as a warning *before* it throws,
  // so the useful signal is there even when the loop has not tripped yet.
  const noisy = errors.filter(
    (e) => !e.includes("Download the React DevTools") && !e.includes("favicon"),
  );
  expect(noisy, noisy.join("\n")).toEqual([]);
});
