/**
 * §11.3's third column, and the search half of its first.
 *
 * The layout in §11.3 is HIERARCHY | main | INSPECTOR, and the inspector was
 * never built — `--inspector-w: 300px` sat in tokens.css referenced by nothing,
 * while three parts of the spec sent information to a column that did not
 * exist. The tree column was likewise "arbore + cautare" with no search.
 */

import { expect, test, type Page } from "@playwright/test";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

let buggy = "";
let checks = "";
let arb = "";

test.beforeAll(async ({ request }) => {
  const open = async (trace: string, rtl: string) => {
    const r = await request.post(`${BACKEND}/session`, {
      data: { trace_path: trace, rtl_paths: [rtl] },
    });
    expect(r.ok(), `could not open ${trace}: ${await r.text()}`).toBeTruthy();
    return (await r.json()).session_id as string;
  };
  buggy = await open("designs/fifo_buggy/dump.vcd", "designs/fifo_buggy");
  checks = await open("designs/checks/dump.vcd", "designs/checks");
  arb = await open("designs/axi_arb/dump.vcd", "designs/axi_arb");
});

async function open(page: Page, session: string): Promise<void> {
  await page.goto(`/?session=${session}`);
  await expect(page.locator(".loading")).toHaveCount(0);
}

test("the inspector is the third column and describes the selection", async ({ page }) => {
  await open(page, buggy);
  await page.locator('[data-testid="tab-1"]').click();
  const insp = page.locator('[data-testid="inspector"]');
  await expect(insp).toBeVisible();
  await expect(insp).toContainText("Nothing selected");

  await page.locator(".sig-name", { hasText: "full" }).first().click();
  await expect(insp).toContainText("full");
  await expect(insp).toContainText("width");
  await expect(insp).toContainText("events");
});

test("it is collapsible, like the other two columns", async ({ page }) => {
  // §11.3: "toate cele trei coloane sunt colapsabile".
  await open(page, buggy);
  await expect(page.locator('[data-testid="inspector"]')).toBeVisible();
  await page.locator('[data-testid="inspector-toggle"]').click();
  await expect(page.locator('[data-testid="inspector"]')).toHaveCount(0);
  await page.locator('[data-testid="inspector-toggle"]').click();
  await expect(page.locator('[data-testid="inspector"]')).toBeVisible();
});

test("a signal in a second clock domain shows its own cycle, named", async ({ page }) => {
  // §5.5, problem 3: `c1247` is ambiguous with two clocks, so the domain that
  // counted it is named. designs/checks clocks `status` on slow_clk and
  // everything else on clk.
  await open(page, checks);
  await page.locator('[data-testid="tab-1"]').click();
  await page.locator("canvas").first().click({ position: { x: 400, y: 40 } });

  await page.locator(".sig-name", { hasText: "status" }).first().click();
  const insp = page.locator('[data-testid="inspector"]');
  await expect(insp).toContainText("domain");
  await expect(insp).toContainText("slow_clk");

  // A signal on the primary clock has no second cycle to report, so it says
  // nothing rather than repeating the same number under another name.
  await page.locator(".sig-name", { hasText: "lock_r" }).first().click();
  await expect(insp).not.toContainText("slow_clk");
});

test("clicking a transaction puts its fields and metrics in the inspector", async ({ page }) => {
  // §11.4, TAB 8: "Inspector arata toate campurile si metricile".
  await open(page, arb);
  await page.locator('[data-testid="tab-8"]').click();
  await expect(page.locator('[data-testid="transactions-tab"]')).toBeVisible();
  await page.locator('[data-testid^="txn-band-"]').first().click();

  const insp = page.locator('[data-testid="inspector"]');
  await expect(insp).toContainText("WRITE");
  await expect(insp).toContainText("FIELDS");
  await expect(insp).toContainText("awaddr");
  await expect(insp).toContainText("METRICS");
  await expect(insp).toContainText("latency");
});

test("the design tree has the search §11.3 asks for", async ({ page }) => {
  await open(page, buggy);
  const search = page.locator('[data-testid="tree-search"]');
  await expect(search).toBeVisible();

  await search.fill("wr_ptr");
  const hits = page.locator('[data-testid="tree-hits"]');
  await expect(hits).toBeVisible();
  await expect(hits).toContainText("wr_ptr");

  // Clearing it puts the tree back rather than leaving an empty column.
  await search.fill("");
  await expect(page.locator('[data-testid="tree-hits"]')).toHaveCount(0);
  await expect(page.locator('[data-testid="tree-scope"]').first()).toBeVisible();

  await search.fill("zzz-nothing-here");
  await expect(page.locator('[data-testid="tree-hits"]')).toContainText("Nothing matches");
});
