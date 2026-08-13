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

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

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
