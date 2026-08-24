/**
 * §11.3's design tree — the left column of the three the spec lays out.
 *
 * These tests exist because of a real report: on a design with 1,461 signals
 * the wave showed fewer than ten, and there was no way to see the rest short
 * of the command palette, one signal per invocation. The tree, its counts and
 * `+ all` are what close that; so each test is about *getting signals onto the
 * screen*, not about how the tree draws.
 */

import { expect, test, type Page } from "@playwright/test";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

let sessionId = "";

test.beforeAll(async ({ request }) => {
  const r = await request.post(`${BACKEND}/session`, {
    data: { trace_path: "designs/checks/dump.vtx", rtl_paths: ["designs/checks"] },
  });
  expect(r.ok(), `could not open designs/checks: ${await r.text()}`).toBeTruthy();
  sessionId = (await r.json()).session_id;
});

/** Start from an empty wave, so what arrives is only what the tree put there. */
test.beforeEach(async ({ request }) => {
  await request.put(`${BACKEND}/session/${sessionId}/layout`, {
    data: { signals: [], groups: [], radix: {}, bookmarks: [], cursors: [], zoom: null },
  });
});

async function open(page: Page): Promise<void> {
  await page.goto(`/?session=${sessionId}`);
  await expect(page.locator('[data-testid="hierarchy"]')).toBeVisible();
  await page.locator('[data-testid="tab-1"]').click();
}

test("the tree opens on the top module and says how much is under each scope", async ({ page }) => {
  await open(page);
  const top = page.locator('[data-testid="tree-scope"][data-path="tb_checks"]');
  await expect(top).toBeVisible();
  // A design with one top module starts expanded: a first click that only
  // reveals the thing you already knew about is a wasted click.
  await expect(top).toHaveAttribute("aria-expanded", "true");

  // Every scope carries the size of its subtree, so "+ all" is a decision.
  const dut = page.locator('[data-testid="tree-scope"][data-path="tb_checks.dut"]');
  await expect(dut).toBeVisible();
  const count = dut.locator("xpath=following-sibling::span[1]");
  expect(Number(await count.innerText())).toBeGreaterThan(0);
});

test("one click puts a whole scope on the wave", async ({ page }) => {
  // The point of the whole panel: `add wave -r`, not a signal at a time.
  await open(page);
  // An empty layout is seeded with a fixed handful of rows, so what matters is
  // the jump, not the absolute count.
  const rows = page.locator('[data-testid="signal-row"]');
  const before = await rows.count();

  const row = page.locator('[data-testid="tree-scope"][data-path="tb_checks.dut"]');
  await row.hover();
  await page
    .locator('[data-testid="tree-add-scope"][data-path="tb_checks.dut"]')
    .click();

  await expect(page.locator('[data-testid="hierarchy-note"]')).toContainText("+");
  await expect
    .poll(async () => rows.count())
    .toBeGreaterThan(before);
  // Added under a group header named for the scope, so the wave reads like the
  // design rather than as one flat list.
  await expect(page.locator('[data-testid="signal-row"], .group-row').first()).toBeVisible();
});

test("adding the same scope twice does not duplicate it", async ({ page }) => {
  await open(page);
  const scope = page.locator('[data-testid="tree-scope"][data-path="tb_checks.dut"]');
  const add = page.locator('[data-testid="tree-add-scope"][data-path="tb_checks.dut"]');
  const rows = page.locator('[data-testid="signal-row"]');

  await scope.hover();
  await add.click();
  await expect(page.locator('[data-testid="hierarchy-note"]')).not.toBeEmpty();
  const after = await rows.count();

  // The second press is the assertion: the scope is entirely on screen now, so
  // there is nothing left to add and nothing is duplicated.
  await scope.hover();
  await add.click();
  await expect(page.locator('[data-testid="hierarchy-note"]')).toContainText("already on screen");
  await expect(rows).toHaveCount(after);
});

test("expanding a scope fetches its level and its signals can be added one by one", async ({
  page,
}) => {
  await open(page);
  await page.locator('[data-testid="tree-scope"][data-path="tb_checks.dut"]').click();
  const sig = page.locator('[data-testid="tree-signal"]').first();
  await expect(sig).toBeVisible();
  const path = await sig.getAttribute("data-path");

  await sig.click();
  const rows = await page.$$eval('[data-testid="signal-row"]', (r) =>
    r.map((x) => x.getAttribute("data-path") ?? ""),
  );
  expect(rows).toContain(path);
});

test("the tree collapses, and comes back", async ({ page }) => {
  // §11.3: "Toate cele trei coloane sunt colapsabile". The chord was in the
  // help overlay from the start and did nothing.
  await open(page);
  await page.locator('[data-testid="tree-toggle"]').click();
  await expect(page.locator('[data-testid="hierarchy"]')).toHaveCount(0);
  await page.locator('[data-testid="tree-toggle"]').click();
  await expect(page.locator('[data-testid="hierarchy"]')).toBeVisible();
});
