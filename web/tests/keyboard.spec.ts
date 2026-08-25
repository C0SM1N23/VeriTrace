/**
 * §11.7's keyboard contract, and the §9.4 query bar.
 *
 * Every shortcut the help overlay advertises has to do what it says. Three of
 * them did not: `⌘B` was listed with nothing behind it, `c` was unbound, and
 * `f` zoomed instead of taking the fan-out §11.7 gives it. §9.4's query bar was
 * an uncontrolled input that never showed what any action was equivalent to.
 */

import { expect, test, type Page } from "@playwright/test";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

let sessionId = "";

test.beforeAll(async ({ request }) => {
  const r = await request.post(`${BACKEND}/session`, {
    data: { trace_path: "designs/fifo_buggy/dump.vcd", rtl_paths: ["designs/fifo_buggy"] },
  });
  expect(r.ok(), `could not open designs/fifo_buggy: ${await r.text()}`).toBeTruthy();
  sessionId = (await r.json()).session_id;
});

test.beforeEach(async ({ request }) => {
  await request.put(`${BACKEND}/session/${sessionId}/layout`, {
    data: { signals: [], groups: [], radix: {}, bookmarks: [], cursors: [], zoom: null },
  });
});

async function open(page: Page): Promise<void> {
  await page.goto(`/?session=${sessionId}`);
  await expect(page.locator(".loading")).toHaveCount(0);
  await page.locator('[data-testid="tab-1"]').click();
}

/** Click a signal row so the keys that act on "the selection" have one. */
async function selectSignal(page: Page, name: string): Promise<void> {
  const row = page.locator(".sig-name", { hasText: name }).first();
  await row.click();
}

test("c takes the fan-in cone of the selection and filters Wave to it", async ({ page }) => {
  // §8.6 — "din 4000 de semnale ramai cu 8". The endpoint and the analysis were
  // both there; no key and no button reached them.
  await open(page);
  const before = await page.locator('[data-testid="signal-row"]').count();
  await selectSignal(page, "full");
  await page.keyboard.press("c");

  await expect(page.locator('[data-testid="query-bar"]')).toHaveValue(/^cone\(.*full, depth=4\)$/);
  const after = await page.locator('[data-testid="signal-row"]').count();
  expect(after).toBeGreaterThan(0);
  expect(after).toBeLessThan(before);
  // The move is reversible, like every other click-through (§11.4b).
  await expect(page.locator('[data-testid="signal-focus-back"]')).toBeVisible();
});

test("f takes the fan-out, and z is what zooms", async ({ page }) => {
  await open(page);
  await selectSignal(page, "wr_en");
  await page.keyboard.press("f");
  await expect(page.locator('[data-testid="query-bar"]')).toHaveValue(
    /^fanout\(.*wr_en, depth=4\)$/,
  );
});

test("cone without a selection says what to do rather than nothing", async ({ page }) => {
  await open(page);
  await page.keyboard.press("c");
  await expect(page.locator('[data-testid="status-note"]')).toContainText("Select a signal");
});

test("every route writes its query into the bar", async ({ page }) => {
  // §9.4: *"asa se invata limbajul fara tutorial"*.
  await open(page);
  const bar = page.locator('[data-testid="query-bar"]');
  await expect(bar).toHaveValue("");

  await selectSignal(page, "full");
  await page.keyboard.press("w");
  await expect(bar).toHaveValue(/^why\(tb_fifo_buggy.*full @ \d+\)$/);
  await expect(page.locator('[data-testid="tab-2"]')).toHaveAttribute("aria-selected", "true");
});

test("the bar remembers what this session asked", async ({ page }) => {
  await open(page);
  const bar = page.locator('[data-testid="query-bar"]');
  await bar.fill("why(tb_fifo_buggy.dut.full @ 455000)");
  await bar.press("Enter");
  await expect(page.locator('[data-testid="causal-card"]').first()).toBeVisible({
    timeout: 15_000,
  });

  await bar.click();
  await bar.fill("");
  await bar.press("ArrowUp");
  await expect(bar).toHaveValue("why(tb_fifo_buggy.dut.full @ 455000)");
  // Forward again, off the end, is an empty bar ready for a new question.
  await bar.press("ArrowDown");
  await expect(bar).toHaveValue("");
});

test("typing offers the verbs and then the signal names", async ({ page }) => {
  await open(page);
  const bar = page.locator('[data-testid="query-bar"]');
  await bar.click();
  await bar.fill("co");
  await expect(page.locator('[data-testid="query-suggestion"]').first()).toHaveText("cone(");

  await bar.fill("why(dut.fu");
  const sug = page.locator('[data-testid="query-suggestion"]');
  await expect(sug.first()).toBeVisible();
  expect(await sug.first().textContent()).toContain("full");

  // A finished query is not completed out from under whoever typed it.
  await bar.fill("why(tb_fifo_buggy.dut.full @ 455000)");
  await bar.press("Enter");
  await expect(page.locator('[data-testid="causal-card"]').first()).toBeVisible({
    timeout: 15_000,
  });
});

test("cmd-B bookmarks the cursor, and the note survives a reload", async ({ page }) => {
  // §11.4 and P5. The shortcut was advertised in the help overlay from the
  // start; nothing was behind it, and the layout wrote an empty list every save.
  await open(page);
  await selectSignal(page, "full");
  await page.locator("canvas").first().click({ position: { x: 300, y: 40 } });
  await page.keyboard.press("ControlOrMeta+b");

  await page.locator('[data-testid="bookmarks-toggle"]').click();
  const panel = page.locator('[data-testid="bookmarks-panel"]');
  await expect(panel).toBeVisible();
  await panel.locator('[data-testid="bookmark-label"]').fill("full goes high here");
  await page.keyboard.press("Escape");
  await page.locator(".palette-backdrop").click({ position: { x: 5, y: 5 } });

  await page.reload();
  await expect(page.locator(".loading")).toHaveCount(0);
  await page.locator('[data-testid="bookmarks-toggle"]').click();
  await expect(
    page.locator('[data-testid="bookmarks-panel"] [data-testid="bookmark-label"]'),
  ).toHaveValue("full goes high here");
});

test("n and p walk the findings in Checks, not only in Diff", async ({ page }) => {
  // §11.7 lists the keys for "Checks/Diff"; only Diff had them.
  await open(page);
  await page.locator('[data-testid="tab-5"]').click();
  await expect(page.locator('[data-testid="checks-tab"]')).toBeVisible();
  const rows = page.locator('[data-testid="check-row"]');
  await expect(rows.first()).toBeVisible();

  await page.keyboard.press("n");
  await expect(page.locator(".check-row.walked")).toHaveCount(1);
  const first = await page.locator(".check-row.walked").getAttribute("data-finding-index");
  await page.keyboard.press("n");
  const second = await page.locator(".check-row.walked").getAttribute("data-finding-index");
  expect(second).not.toBe(first);
  await page.keyboard.press("p");
  expect(await page.locator(".check-row.walked").getAttribute("data-finding-index")).toBe(first);
});
