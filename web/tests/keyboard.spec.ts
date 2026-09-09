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

test("c takes the fan-in cone of the selection and filters Wave to it", async ({ page, request }) => {
  // §8.6 — "din 4000 de semnale ramai cu 8". The endpoint and the analysis were
  // both there; no key and no button reached them.
  await open(page);
  await selectSignal(page, "full");
  await page.keyboard.press("c");

  await expect(page.locator('[data-testid="query-bar"]')).toHaveValue(/^cone\(.*full, depth=4\)$/);

  // What matters is that Wave is showing the cone, not that it is showing
  // *fewer* rows: on a design this small the cone reaches everything, and
  // asserting a reduction would be asserting a property of fifo_buggy.
  const q = await page.locator('[data-testid="query-bar"]').inputValue();
  const r = await request.post(`${BACKEND}/session/${sessionId}/query`, { data: { vtq: q } });
  const expected: string[] = (await r.json()).nodes.map((n: { path: string }) => n.path);
  expect(expected.length).toBeGreaterThan(0);

  const shown = await page.$$eval('[data-testid="signal-row"]', (rows) =>
    rows.map((el) => el.getAttribute("data-path") ?? ""),
  );
  expect(shown.length).toBe(expected.length);
  expect(new Set(shown)).toEqual(new Set(expected));

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
  await page.locator('[data-testid="tab-6"]').click();
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

test("subtrace() and repro() are verbs, not only buttons", async ({ page }) => {
  // §9.2 lists them beside why(). Both were reachable by clicking in Causal and
  // by neither name in the bar, so the language and the buttons disagreed about
  // what the tool can do.
  await open(page);
  const bar = page.locator('[data-testid="query-bar"]');

  await bar.fill("subtrace(tb_fifo_buggy.dut.full @ 455000)");
  await bar.press("Enter");
  await expect(page.locator('[data-testid="subtrace-section"] .subtrace li').first()).toBeVisible({
    timeout: 20_000,
  });

  await bar.fill("repro(tb_fifo_buggy.dut.full @ 455000)");
  await bar.press("Enter");
  await expect(page.locator('[data-testid="repro-code"]')).toBeVisible({ timeout: 30_000 });
});

test("a chord still works while a field has focus", async ({ page }) => {
  // ⌘M and ⌘B sat below the "is the user typing?" guard, so any focused input
  // swallowed them. A modifier says the keystroke is for the application, and
  // there is now an input in the design tree that is always on screen — which
  // is how this surfaced.
  await open(page);
  await page.locator('[data-testid="tree-search"]').click();
  await page.locator('[data-testid="tree-search"]').fill("wr");

  await page.locator('[data-testid="tab-3"]').click();
  await page.locator('[data-testid="tree-search"]').click();
  await page.keyboard.press("ControlOrMeta+m");
  await expect(page.locator('[data-testid="fsm-mode"]')).toBeVisible();

  // And the bare letters still yield to the field, so typing "c" in a filter
  // does not run a cone.
  await page.locator('[data-testid="tree-search"]').click();
  await page.keyboard.press("c");
  await expect(page.locator('[data-testid="query-bar"]')).toHaveValue("");
});

test("cmd-backslash toggles both side panels", async ({ page }) => {
  await open(page);
  await expect(page.locator('[data-testid="hierarchy"]')).toBeVisible();
  await expect(page.locator('[data-testid="inspector"]')).toBeVisible();

  await page.keyboard.press("ControlOrMeta+\\");
  await expect(page.locator('[data-testid="hierarchy"]')).toHaveCount(0);
  await expect(page.locator('[data-testid="inspector"]')).toHaveCount(0);

  await page.keyboard.press("ControlOrMeta+\\");
  await expect(page.locator('[data-testid="hierarchy"]')).toBeVisible();
  await expect(page.locator('[data-testid="inspector"]')).toBeVisible();
});

test("cmd-shift-F searches indexed signals across the design", async ({ page }) => {
  await open(page);
  await page.locator('[data-testid="tab-3"]').click();
  await page.keyboard.press("ControlOrMeta+Shift+f");
  const palette = page.locator('[data-testid="palette"]');
  await expect(palette).toBeVisible();
  await palette.locator("input").fill("full");
  await expect(palette.locator("button", { hasText: "full" }).first()).toBeVisible();
});

test("cycle ruler uses the backend's resolved primary clock", async ({ page, request }) => {
  await open(page);
  const status = await (await request.get(`${BACKEND}/session/${sessionId}/status`)).json();
  const primary = status.clock_domains.find((d: { primary: boolean }) => d.primary);
  expect(primary).toBeTruthy();

  const clock = await page.evaluate(() => {
    const s = (window as any).__vtStore.getState();
    return { period: s.clockPeriod, origin: s.clockOrigin };
  });
  expect(clock).toEqual({ period: primary.period, origin: primary.origin });
});
