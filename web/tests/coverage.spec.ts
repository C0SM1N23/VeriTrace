/**
 * TAB 7 — Coverage (§11.4), and Prompt 12's acceptance criteria in the UI.
 *
 * Two designs, because the two criteria are about different things:
 *
 * - `designs/axi_lite` for the functional matrix. Its master drives
 *   `wstrb = 4'hF` on every write, uses only word-aligned addresses and
 *   strictly alternates a write and a read — so `partial_write`, `unaligned`,
 *   `WRITE -> WRITE` and `READ -> READ` are holes as a matter of fact about
 *   the source, and the assertions below can be checked against it.
 * - `designs/dma` for the injected corruption, which §8.19 says arrives as an
 *   ordinary finding rather than in a tab of its own.
 *
 * The tests that matter most are the ones about *empty* cells. §8.21's whole
 * value is the box nobody ticked, so a matrix assembled only from what happened
 * would pass every other check and be useless — which is why the cells are
 * counted in the DOM rather than merely looked at.
 */

import { expect, test, type Page } from "@playwright/test";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

let axiLite = "";
let corrupted = "";
let clean = "";

test.beforeAll(async ({ request }) => {
  const open = async (trace: string) => {
    const r = await request.post(`${BACKEND}/session`, { data: { trace_path: trace } });
    expect(r.ok(), `could not open ${trace}: ${await r.text()}`).toBeTruthy();
    return (await r.json()).session_id as string;
  };
  axiLite = await open("designs/axi_lite/dump.vtx");
  corrupted = await open("designs/dma/dump.vtx");
  clean = await open("designs/dma/dump_ok.vtx");
});

async function ready(page: Page, session: string): Promise<void> {
  await page.goto(`/?session=${session}`);
  await expect(page.locator(".loading")).toHaveCount(0);
  await expect(page.locator('[data-testid="status-range"]')).not.toBeEmpty();
}

async function open(page: Page, session: string): Promise<void> {
  await ready(page, session);
  await page.locator('[data-testid="tab-7"]').click();
  await expect(page.locator('[data-testid="coverage-tab"]')).toBeVisible();
}

test("the Coverage tab is reachable by click and by key", async ({ page }) => {
  await ready(page, axiLite);
  await page.keyboard.press("7");
  await expect(page.locator('[data-testid="tab-7"]')).toHaveAttribute("aria-selected", "true");
  await expect(page.locator('[data-testid="coverage-tab"]')).toBeVisible();
});

test("both sections are on the page at once", async ({ page }) => {
  // §11.4's consolidation: one tab, two sections, not two tools.
  await open(page, axiLite);
  await expect(page.locator('[data-testid="cov-functional"]')).toBeVisible();
  await expect(page.locator('[data-testid="cov-code"]')).toBeVisible();
});

test("the sequence matrix draws every cell, including the two never reached", async ({
  page,
}) => {
  // Prompt 12's second acceptance criterion. axil_master alternates a write
  // and a read, so WRITE -> WRITE and READ -> READ never happen — and both
  // cells are on screen, empty, which is the only way a hole is findable.
  await open(page, axiLite);
  const matrix = page.locator('[data-testid="cov-matrix-kind"]');
  await expect(matrix.locator("tbody td")).toHaveCount(4);
  await expect(matrix.locator("tbody td.on")).toHaveCount(2);
  await expect(page.locator('[data-testid="cov-cell-kind-WRITE-WRITE"]')).toHaveAttribute(
    "data-hits",
    "0",
  );
  await expect(page.locator('[data-testid="cov-cell-kind-READ-WRITE"]')).not.toHaveAttribute(
    "data-hits",
    "0",
  );
});

test("a response code that never occurred is an empty bin, not a missing one", async ({
  page,
}) => {
  // The slave answers OKAY and SLVERR; AXI's other two codes are holes.
  await open(page, axiLite);
  await expect(page.locator('[data-testid="cov-point-bresp"]')).toContainText("2/4");
  await expect(page.locator('[data-testid="cov-bin-bresp-DECERR"]')).toHaveAttribute(
    "data-hits",
    "0",
  );
  await expect(page.locator('[data-testid="cov-bin-bresp-OKAY"]')).not.toHaveAttribute(
    "data-hits",
    "0",
  );
});

test("the corners the design never reaches are shown, and the one it does is not", async ({
  page,
}) => {
  // The control that keeps the assertions above from passing vacuously: two
  // corners are holes and one is not, all three decided by axil_master.sv.
  await open(page, axiLite);
  await expect(page.locator('[data-testid="cov-point-partial_write"]')).toContainText("0/1");
  await expect(page.locator('[data-testid="cov-point-unaligned"]')).toContainText("0/1");
  await expect(page.locator('[data-testid="cov-point-error_response"]')).toContainText("1/1");
});

test("no coverage database is explained, not left blank", async ({ page }) => {
  // P1: an empty section that could equally mean "100%" is the failure mode
  // this feature cannot afford.
  await open(page, axiLite);
  const note = page.locator('[data-testid="cov-no-code"]');
  await expect(note).toBeVisible();
  await expect(note).toContainText("verilator --coverage");
  await expect(note).toContainText("xcrg");
});

test("the interface selector switches which matrix is shown", async ({ page }) => {
  await open(page, corrupted);
  const select = page.locator('[data-testid="cov-iface"]');
  await expect(select).toBeVisible();
  await select.selectOption("mem");
  await expect(page.locator('[data-testid="cov-summary"]')).toContainText("AXI4-Lite");
});

test("the injected corruption reaches Checks as an ordinary finding", async ({ page }) => {
  // Prompt 12's first acceptance criterion, through the interface: §8.19 says
  // mismatches are findings, so they arrive with the stuck signals.
  await ready(page, corrupted);
  await page.locator('[data-testid="tab-6"]').click();
  const rows = page.locator('[data-testid="check-row"]', { hasText: "byte(s) 0-1" });
  await expect(rows.first()).toBeVisible();
  await expect(rows.first()).toContainText("dma.s_axi");
});

test("the same design with the bug compiled out reports no corruption", async ({ page }) => {
  await ready(page, clean);
  await page.locator('[data-testid="tab-6"]').click();
  await expect(page.locator('[data-testid="checks-tab"]')).toBeVisible();
  await expect(
    page.locator('[data-testid="check-row"]', { hasText: "byte(s)" }),
  ).toHaveCount(0);
});

test("coverage measures the test, so the clean run has the same holes", async ({ page }) => {
  // Coverage is about the stimulus, not about the bug — the control run has
  // the same empty cells because it runs the same testbench.
  await open(page, clean);
  await expect(page.locator('[data-testid="cov-point-partial_write"]')).toContainText("0/1");
});
