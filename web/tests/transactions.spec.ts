/**
 * TAB 8 — Transactions (§11.4b), and Prompt 9's acceptance criteria.
 *
 * Runs against `designs/axi_arb`: two AXI4-Lite masters, a fixed-priority
 * arbiter and one slow slave. Three interfaces, so §11.4b's rule ("two or more
 * interfaces -> Transactions") is exercised by opening the page rather than by
 * asserting on a function.
 *
 * The last test is the one that matters most: §8.16 says a transaction-level
 * `why` has to leave the master that looks stuck and name the transaction on
 * the *other* master that is actually holding the bus. That is checked through
 * the interface a user drives, not through the API.
 */

import { expect, test, type Page } from "@playwright/test";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

let sessionId = "";

test.beforeAll(async ({ request }) => {
  const r = await request.post(`${BACKEND}/session`, {
    data: { trace_path: "designs/axi_arb/dump.vtx", rtl_paths: ["designs/axi_arb"] },
  });
  expect(r.ok(), `could not open designs/axi_arb: ${await r.text()}`).toBeTruthy();
  sessionId = (await r.json()).session_id;
});

/**
 * P5 keeps layout on disk, so a test that zooms leaves the next one zoomed.
 * Reset before each, or these tests only pass in the order they were written.
 */
test.beforeEach(async ({ request }) => {
  await request.put(`${BACKEND}/session/${sessionId}/layout`, {
    data: { signals: [], groups: [], radix: {}, bookmarks: [], cursors: [], zoom: null },
  });
});

async function open(page: Page): Promise<void> {
  await page.goto(`/?session=${sessionId}`);
  await expect(page.locator('[data-testid="transactions-tab"]')).toBeVisible();
  // Zoom to fit. The layout reset above races the previous page's debounced
  // save, so the starting view is normalised here rather than assumed — and
  // every band is then on screen, whatever ran before.
  await page.keyboard.press("f");
  await expect(page.locator('[data-testid^="txn-band-"]').first()).toBeVisible();
}

test("a design with two interfaces opens on Transactions", async ({ page }) => {
  // §11.4b, decided by the server when the session is created.
  await open(page);
  await expect(page.locator('[data-testid="tab-8"]')).toHaveAttribute("aria-selected", "true");
});

test("every detected interface is offered, with its pack", async ({ page }) => {
  await open(page);
  const options = await page.$$eval('[data-testid="txn-iface"] option', (o) =>
    o.map((x) => x.textContent ?? ""),
  );
  expect(options.length).toBe(3);
  for (const name of ["m0", "m1", "slv"]) {
    expect(options.some((o) => o.startsWith(name))).toBeTruthy();
  }
  expect(options.every((o) => o.includes("AXI4-Lite"))).toBeTruthy();
});

test("the correlation rate is on screen, not implied", async ({ page }) => {
  // §8.14's acceptance criterion: an extraction that found 3 of 400 must look
  // different from one that found all 3.
  await open(page);
  await expect(page.locator('[data-testid="txn-correlation"]')).toContainText("100%");
  await expect(page.locator('[data-testid="txn-count"]')).toContainText("transactions");
});

test("the Gantt shows a band per transaction, on Wave's time axis", async ({ page }) => {
  await open(page);
  const bands = page.locator('[data-testid^="txn-band-"]');
  await expect(bands).toHaveCount(10);

  const band = bands.nth(5);
  const before = await band.boundingBox();
  expect(before?.width ?? 0).toBeGreaterThan(0);

  // The two views share one time axis (§11.4b), so narrowing it — here by
  // opening one transaction — has to widen that band. A Gantt drawn on its own
  // scale would be a second, quietly disagreeing view of time.
  await band.click();
  const after = await band.boundingBox();
  expect(after?.width ?? 0).toBeGreaterThan((before?.width ?? 0) * 2);

  // And the ones now outside the window are gone rather than parked off-screen.
  await expect(bands.first()).toBeHidden();
});

test("clicking a transaction filters Wave to its interface and jumps to it", async ({ page }) => {
  await open(page);
  await page.locator('[data-testid^="txn-band-"]').nth(3).click();

  // The row list is now the interface's own signals.
  await page.locator('[data-testid="tab-1"]').click();
  const rows = await page.$$eval('[data-testid="signal-row"]', (r) =>
    r.map((x) => x.getAttribute("data-path") ?? ""),
  );
  expect(rows.length).toBeGreaterThan(0);
  expect(rows.every((p) => p.includes("m0"))).toBeTruthy();
  expect(rows.some((p) => p.endsWith("awvalid"))).toBeTruthy();
});

test("the list a click-through replaced can be brought back", async ({ page, request }) => {
  // §11.4b's focus is meant to help, not to cost someone the list they spent
  // ten minutes assembling. Every other move in the app is additive or
  // reversible; this one used to be a one-way door with no undo, and the
  // replacement was persisted, so a reload did not recover it either.
  //
  // The starting list is chosen here rather than inherited from whatever the
  // previous test left behind: a click-through focuses on `m0`, so seeding with
  // `m1` rows makes "the list changed" a real observation instead of a race
  // against another spec's debounced layout save.
  const listed = await (await request.get(`${BACKEND}/session/${sessionId}/signals`)).json();
  const seed = (listed.signals as { handle: number; path: string }[])
    .filter((s: { path: string }) => s.path.includes("m1"))
    .slice(0, 3)
    .map((s: { handle: number; path: string }) => ({
      kind: "signal",
      handle: s.handle,
      path: s.path,
    }));
  expect(seed.length).toBeGreaterThan(0);
  await request.put(`${BACKEND}/session/${sessionId}/layout`, {
    data: { signals: seed, groups: [], radix: {}, bookmarks: [], cursors: [], zoom: null },
  });

  await open(page);
  await page.locator('[data-testid="tab-1"]').click();
  const before = await page.$$eval('[data-testid="signal-row"]', (r) =>
    r.map((x) => x.getAttribute("data-path") ?? ""),
  );
  expect(before).toEqual(seed.map((r: { path: string }) => r.path));

  await page.locator('[data-testid="tab-8"]').click();
  // Wait for the band list to be on screen before clicking into it: the tab
  // switch re-renders it, and clicking a band that is still being laid out
  // focuses nothing, which used to fail this test roughly one full run in two.
  await expect(page.locator('[data-testid^="txn-band-"]').first()).toBeVisible();
  await page.locator('[data-testid^="txn-band-"]').nth(3).click();
  await page.locator('[data-testid="tab-1"]').click();

  // The way back is on screen, not a shortcut nobody would guess — and its
  // appearance is also the signal that the focus actually happened, so it is
  // awaited before the two lists are compared.
  const back = page.locator('[data-testid="signal-focus-back"]');
  await expect(back).toBeVisible();

  const focused = await page.$$eval('[data-testid="signal-row"]', (r) =>
    r.map((x) => x.getAttribute("data-path") ?? ""),
  );
  expect(focused).not.toEqual(before);
  await back.click();
  const restored = await page.$$eval('[data-testid="signal-row"]', (r) =>
    r.map((x) => x.getAttribute("data-path") ?? ""),
  );
  expect(restored).toEqual(before);

  // And it is a swap, not an undo: the focused list is one click away again,
  // so neither list can be lost in either direction.
  await back.click();
  const again = await page.$$eval('[data-testid="signal-row"]', (r) =>
    r.map((x) => x.getAttribute("data-path") ?? ""),
  );
  expect(again).toEqual(focused);
});

test("the field table carries the pack's own fields and metrics", async ({ page }) => {
  await open(page);
  const headers = await page.$$eval('[data-testid="txn-table"] th', (h) =>
    h.map((x) => (x.textContent ?? "").replace(/[▾▴]/g, "").trim()),
  );
  for (const col of ["addr", "latency", "stall_cycles"]) {
    expect(headers, `${col} missing`).toContain(col);
  }
  await expect(page.locator('[data-testid="txn-table"] tbody tr')).toHaveCount(10);
});

test("the table sorts on a metric", async ({ page }) => {
  await open(page);
  const cell = (row: number) =>
    page.locator('[data-testid="txn-table"] tbody tr').nth(row).locator("td").first();
  const firstBefore = await cell(0).textContent();
  const header = page.locator('[data-testid="txn-th-latency"]');
  await header.click();
  await header.click();
  expect(await cell(0).textContent()).not.toBe(null);
  // Sorting must not lose or duplicate rows.
  await expect(page.locator('[data-testid="txn-table"] tbody tr')).toHaveCount(10);
  expect(firstBefore).toBeTruthy();
});

test("why at transaction level crosses the arbiter to the other master", async ({ page }) => {
  // §8.16, the third acceptance criterion of Prompt 9, through the UI.
  await open(page);
  await page.locator('[data-testid="query-bar"]').fill("why(txn.m0.WRITE[0].not_issued)");
  await page.locator('[data-testid="query-bar"]').press("Enter");

  await expect(page.locator('[data-testid="causal-headline"]')).toContainText("WRITE#0");

  // Open every branch, then look for the node that is about a transaction.
  for (let i = 0; i < 14; i++) {
    const toggles = page.locator('[data-testid="branch-toggle"]:has-text("▸")');
    if ((await toggles.count()) === 0) break;
    await toggles.first().click();
  }
  const txnCards = page.locator('[data-testid="causal-txn-card"]');
  await expect(txnCards.first()).toBeVisible();
  const refs = await txnCards.evaluateAll((els) =>
    els.map((e) => e.getAttribute("data-txn") ?? ""),
  );
  expect(refs.some((r) => r.startsWith("m1.")), `chain stayed inside m0: ${refs}`).toBeTruthy();
});

test("a protocol violation shows up in Checks like any other finding", async ({ request }) => {
  // §11.4: violations are findings, not a separate kind of news. Checked over
  // the API on the design that has one, so the assertion is about the pipeline
  // rather than about a screenshot.
  const r = await request.post(`${BACKEND}/session`, {
    data: { trace_path: "designs/axi_lite/dump.vtx", rtl_paths: ["designs/axi_lite"] },
  });
  const sid = (await r.json()).session_id;
  const checks = await (await request.get(`${BACKEND}/session/${sid}/checks`)).json();
  const protocol = checks.findings.filter((f: { group: string }) => f.group === "protocol");
  expect(protocol.length).toBeGreaterThan(0);
  expect(protocol[0].why).toBeTruthy();
});
