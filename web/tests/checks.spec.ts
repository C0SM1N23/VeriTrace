/**
 * TAB 6 — Checks (§11.4), and Prompt 7's acceptance criterion.
 *
 * The criterion is behavioural, not structural: opening a session on the
 * checks design has to surface every injected problem *without a single manual
 * query*, and every row has to carry an exact source location and a working
 * `[why]`. So this test never types into the query bar.
 *
 * Runs against `designs/checks`, whose flaws are all deliberate.
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

/** Every problem injected into designs/checks, by check name. */
const INJECTED = [
  "inferred_latch",
  "case_no_default",
  "stuck",
  "blocking_in_always_ff",
  "nonblocking_in_always_comb",
  "incomplete_sensitivity",
  "cdc_no_sync",
  "async_reset_no_sync",
  "x_source",
  "x_optimism",
  "parameter_default",
];

/**
 * The server was started on another trace, so this suite opens the checks
 * design as a second session and reaches it with `?session=`. One server, many
 * sessions — which is what §10.1's `POST /session` is for.
 */
let sessionId = "";

test.beforeAll(async ({ request }) => {
  const r = await request.post(`${BACKEND}/session`, {
    data: {
      trace_path: "designs/checks/dump.vtx",
      rtl_paths: ["designs/checks"],
    },
  });
  expect(r.ok(), `could not open designs/checks: ${await r.text()}`).toBeTruthy();
  sessionId = (await r.json()).session_id;
});

/** Clear any suppression a previous run left behind (P5 keeps them on disk). */
test.beforeEach(async ({ request }) => {
  const checks = await (await request.get(`${BACKEND}/session/${sessionId}/checks`)).json();
  for (const id of Object.keys(checks.suppressed ?? {})) {
    await request.delete(`${BACKEND}/session/${sessionId}/checks/${id}/suppress`);
  }
});

async function open(page: Page): Promise<void> {
  await page.goto(`/?session=${sessionId}`);
  await expect(page.locator('[data-testid="checks-tab"]')).toBeVisible();
}

test("the session opens on Checks with the findings already there", async ({ page }) => {
  // §11.4b + §13.4: no click, no query — the first thing on screen is what the
  // tool already knows.
  await open(page);
  await expect(page.locator('[data-testid="tab-5"]')).toHaveAttribute("aria-selected", "true");
  await expect(page.locator('[data-testid="checks-badge"]')).toBeVisible();
  await expect(page.locator('[data-testid="check-row"]').first()).toBeVisible();
});

test("every injected problem is reported without a manual query", async ({ page }) => {
  await open(page);
  const reported = await page.$$eval('[data-testid="check-row"]', (rows) =>
    rows.map((r) => r.getAttribute("data-check")),
  );
  for (const check of INJECTED) {
    expect(reported, `${check} was not reported`).toContain(check);
  }
});

test("every row carries an exact source location", async ({ page }) => {
  await open(page);
  const rows = page.locator('[data-testid="check-row"]');
  const n = await rows.count();
  expect(n).toBeGreaterThan(0);
  for (let i = 0; i < n; i++) {
    await expect(rows.nth(i).locator(".check-loc")).toHaveText(/\.sv:\d+/);
  }
});

test("[why] runs the finding's query and lands in Causal", async ({ page }) => {
  await open(page);
  const row = page.locator('[data-testid="check-row"][data-check="stuck"]').first();
  const signal = await row.locator(".check-sig").innerText();
  await row.locator('[data-testid="check-why"]').click();

  await expect(page.locator('[data-testid="tab-2"]')).toHaveAttribute("aria-selected", "true");
  const root = page.locator('[data-testid="causal-card"]').first();
  await expect(root).toBeVisible();
  await expect(root).toHaveAttribute("data-signal", signal);
});

test("[why] is disabled, not broken, where there is nothing to explain", async ({ page }) => {
  // A parameter is elaboration-time: it has no value in the trace, so §11.4's
  // [why] has nothing to run. Offering a button that errors would be worse.
  await open(page);
  const row = page.locator('[data-testid="check-row"][data-check="parameter_default"]').first();
  await expect(row.locator('[data-testid="check-why"]')).toBeDisabled();
});

test("the CDC finding states that it is not formal sign-off", async ({ page }) => {
  await open(page);
  const row = page.locator('[data-testid="check-row"][data-check="cdc_no_sync"]').first();
  await expect(row).toContainText("not formal CDC sign-off");
});

test("suppressing needs a reason and survives a reload", async ({ page }) => {
  await open(page);
  const before = await page.locator('[data-testid="check-row"]').count();
  const row = page.locator('[data-testid="check-row"][data-check="async_reset_no_sync"]').first();

  await row.locator('[data-testid="check-suppress"]').click();
  const reason = row.locator('[data-testid="suppress-reason"]');
  await expect(reason).toBeVisible();
  // Empty reason: the button stays disabled rather than silently hiding a row.
  await expect(row.locator('button:has-text("hide it")')).toBeDisabled();

  await reason.fill("reset is held for 16 cycles by the PLL, checked by hand");
  await row.locator('button:has-text("hide it")').click();

  await expect(page.locator('[data-testid="check-row"][data-check="async_reset_no_sync"]')).toHaveCount(0);

  // P5: it is on disk, not in the tab.
  await page.reload();
  await expect(page.locator('[data-testid="checks-tab"]')).toBeVisible();
  await expect(page.locator('[data-testid="check-row"][data-check="async_reset_no_sync"]')).toHaveCount(0);

  await page.locator('button:has-text("SUPPRESSED")').click();
  await expect(page.locator('[data-testid="suppressed-list"]')).toContainText("held for 16 cycles");
  await page.locator('button:has-text("restore")').first().click();
  await expect(page.locator('[data-testid="check-row"]')).toHaveCount(before);
});

test("the filter narrows the list without hiding the section structure", async ({ page }) => {
  await open(page);
  const all = await page.locator('[data-testid="check-row"]').count();
  await page.locator('[data-testid="checks-filter"]').fill("lock_r");
  const rows = page.locator('[data-testid="check-row"]');
  const shown = await rows.count();
  expect(shown).toBeGreaterThan(0);
  expect(shown).toBeLessThan(all);
  // The filter matches the signal or the title: a CDC row naming lock_r as the
  // source is a hit even though its own signal is the destination flop.
  for (const text of await rows.allInnerTexts()) {
    expect(text).toContain("lock_r");
  }
});

test("the stuck window can be narrowed without restarting the server", async ({ page }) => {
  // §8.4's threshold decides what this tab says, and its right value depends
  // on the run — until now it lived only in `.veritrace.toml`.
  await open(page);
  const before = await page.locator('[data-testid="check-row"][data-check="stuck"]').count();
  expect(before).toBeGreaterThan(0);
  const latches = page.locator('[data-testid="check-row"][data-check="inferred_latch"]');
  const nLatches = await latches.count();
  expect(nLatches).toBeGreaterThan(0);

  await page.locator('[data-testid="stuck-cycles"]').fill("1000000");
  await page.locator('[data-testid="stuck-apply"]').click();

  // Empty, and saying why it is empty rather than reading as a clean design.
  await expect(page.locator('[data-testid="stuck-note"]')).toContainText("1000000 cycles");
  await expect(page.locator('[data-testid="check-row"][data-check="stuck"]')).toHaveCount(0);
  // And only that group moved: the rest of the report is the session's.
  await expect(latches).toHaveCount(nLatches);

  await page.locator('[data-testid="stuck-reset"]').click();
  await expect(page.locator('[data-testid="check-row"][data-check="stuck"]')).toHaveCount(before);
});

test("the correlation rate opens the list of what is missing", async ({ page }) => {
  // §7.2 makes the rate first-class; a rate is only actionable next to the
  // names it summarises, and those lived only in `veritrace correlate`.
  await open(page);
  const crumb = page.locator('[data-testid="correlation-crumb"]');
  await expect(crumb).toContainText("% correlated");
  await crumb.click();

  const panel = page.locator('[data-testid="correlation-panel"]');
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("signals correlated");
  await expect(panel).toContainText("by method:");
  // Either there is a list of losses or an explicit statement that there are
  // none — never an unexplained empty box.
  const listed =
    (await panel.locator('[data-testid="correlation-unmatched"] li').count()) +
    (await panel.locator('[data-testid="correlation-complete"]').count());
  expect(listed).toBeGreaterThan(0);

  await page.locator(".palette-backdrop").click({ position: { x: 5, y: 5 } });
  await expect(panel).toHaveCount(0);
});

test("the parameter tree marks the default an ancestor contradicts", async ({ page }) => {
  await open(page);
  await page.locator('button:has-text("PARAMETER TREE")').click();
  const tree = page.locator('[data-testid="param-tree"]');
  await expect(tree).toBeVisible();
  await expect(tree).toContainText("DEPTH");
  await expect(tree.locator(".param-warn").first()).toContainText("default");
});
