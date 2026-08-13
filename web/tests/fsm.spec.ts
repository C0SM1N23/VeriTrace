/**
 * FSM mode — §8.8, and §11.4's decision that it is a *mode of Source*.
 *
 * Runs against `designs/fsm`, which carries one deliberate flaw per row of
 * §8.8's table plus a clean control machine. The testbench there never drives
 * the machines into most of those states, which is the point: the Checks tab
 * still lists all five, because they come from the RTL.
 *
 * What only a browser can check, and so is checked here: that the mode takes the
 * Source pane rather than a tab position, that `⌘M` goes both ways, that the
 * diagram distinguishes a state the run visited from one it did not, and that
 * scrubbing the timeline moves the shared cursor (§11.6).
 */

import { expect, test, type Page } from "@playwright/test";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

let session = "";

test.beforeAll(async ({ request }) => {
  const r = await request.post(`${BACKEND}/session`, {
    data: { trace_path: "designs/fsm/dump.vcd.vtx", rtl_paths: ["designs/fsm"] },
  });
  expect(r.ok(), await r.text()).toBeTruthy();
  session = (await r.json()).session_id as string;
});

/**
 * P5 keeps the cursor on disk, so a test that scrubs leaves the next run
 * starting exactly where it was going to end — and "the cursor moved" then
 * reads as a failure on a feature that works.
 */
test.beforeEach(async ({ request }) => {
  await request.put(`${BACKEND}/session/${session}/layout`, {
    data: { signals: [], groups: [], radix: {}, bookmarks: [], cursors: [], zoom: null },
  });
});

async function ready(page: Page): Promise<void> {
  await page.goto(`/?session=${session}`);
  await expect(page.locator(".loading")).toHaveCount(0);
  await expect(page.locator('[data-testid="status-range"]')).not.toBeEmpty();
}

async function openFsm(page: Page): Promise<void> {
  await ready(page);
  await page.keyboard.press("Control+m");
  await expect(page.locator('[data-testid="fsm-mode"]')).toBeVisible();
  await expect(page.locator('[data-testid="fsm-diagram"]')).toBeVisible({ timeout: 30_000 });
}

test("the mode takes the Source tab and gives it back", async ({ page }) => {
  await ready(page);
  // Source needs a file open before there is an editor to take over; opening one
  // is what a reader would have done before reaching for the diagram anyway.
  await page.evaluate(() =>
    (window as never as { __vtStore: { getState(): { openSource(f: string): Promise<void> } } })
      .__vtStore.getState()
      .openSource("fsm_dut.sv"),
  );
  await page.locator('[data-testid="tab-3"]').click();
  await expect(page.locator('[data-testid="source-editor"]')).toBeVisible();

  // §11.4: a mode, not a tab. There is no strip position for it.
  await expect(page.locator('[data-testid="tab-4"]')).toHaveClass(/disabled/);

  await page.locator('[data-testid="fsm-open"]').click();
  await expect(page.locator('[data-testid="fsm-mode"]')).toBeVisible();
  await expect(page.locator('[data-testid="source-editor"]')).toHaveCount(0);

  await page.locator('[data-testid="fsm-close"]').click();
  await expect(page.locator('[data-testid="fsm-mode"]')).toHaveCount(0);
  await expect(page.locator('[data-testid="source-editor"]')).toBeVisible();
});

test("cmd+m opens it from anywhere and closes it again", async ({ page }) => {
  await ready(page);
  await page.keyboard.press("Control+m");
  await expect(page.locator('[data-testid="fsm-mode"]')).toBeVisible();
  await expect(page.locator('[data-testid="tab-3"]')).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Control+m");
  await expect(page.locator('[data-testid="fsm-mode"]')).toHaveCount(0);
});

test("every machine in the design is on offer", async ({ page }) => {
  await openFsm(page);
  const options = await page.locator('[data-testid="fsm-select"] option').allTextContents();
  expect(options.length).toBe(6);
  expect(options.join(" ")).toContain("bad_dead.state");
  expect(options.join(" ")).toContain("good.state");
  // The coverage figure comes from the overlay, so it should be there.
  expect(options.join(" ")).toMatch(/% covered/);
});

test("the diagram distinguishes what the run reached from what it did not", async ({ page }) => {
  await openFsm(page);
  // By value, not by position: the list is ordered by the extractor, and a test
  // that assumes an index breaks the day another machine sorts before it.
  await page.locator('[data-testid="fsm-select"]').selectOption("tb_fsm.bad_dead.state");
  const diagram = page.locator('[data-testid="fsm-diagram"]');
  await expect(diagram.locator(".fsm-node")).toHaveCount(3, { timeout: 30_000 });

  // §8.8 step 5: S_TRAP is never entered by this testbench, so it is drawn as
  // an unvisited state — and the check found it anyway.
  const trap = diagram.locator('.fsm-node[data-state="S_TRAP"]');
  await expect(trap).toHaveClass(/unvisited/);
  await expect(diagram.locator('.fsm-node[data-state="S_IDLE"]')).not.toHaveClass(/unvisited/);
  await expect(diagram.locator('.fsm-node[data-state="S_IDLE"]')).toHaveClass(/reset/);
  // The edge into it was never taken either.
  await expect(diagram.locator(".fsm-edge.untaken")).not.toHaveCount(0);
});

test("scrubbing the timeline moves the shared cursor", async ({ page }) => {
  await openFsm(page);
  const before = await page.evaluate(
    () =>
      (window as never as { __vtStore: { getState(): { cursor: number | null } } })
        .__vtStore.getState().cursor,
  );
  const track = page.locator('[data-testid="fsm-timeline"]');
  const box = await track.boundingBox();
  expect(box).not.toBeNull();
  await page.mouse.move(box!.x + box!.width * 0.7, box!.y + box!.height / 2);
  await page.mouse.down();
  await page.mouse.up();

  await expect(page.locator(".fsm-cursor")).toBeVisible();
  const after = await page.evaluate(
    () =>
      (window as never as { __vtStore: { getState(): { cursor: number | null } } })
        .__vtStore.getState().cursor,
  );
  expect(after).not.toBe(before);
});

test("the diagram exports as SVG", async ({ page }) => {
  await openFsm(page);
  const link = page.locator('[data-testid="fsm-svg"]');
  await expect(link).toHaveAttribute("download", "");
  const href = await link.getAttribute("href");
  expect(href).toContain("/fsm/");

  const svg = await page.request.get(`${BACKEND}${href!.replace(/^\/api/, "")}`);
  expect(svg.ok()).toBeTruthy();
  expect(svg.headers()["content-type"]).toContain("image/svg+xml");
  expect(await svg.text()).toContain("<svg");
});

test("the static findings are in the Checks tab", async ({ page }) => {
  await ready(page);
  await page.locator('[data-testid="tab-5"]').click();
  const checks = page.locator('[data-testid="checks-tab"], .checks');
  await expect(checks.first()).toBeVisible();
  // §8.8: "apar direct in tab-ul Checks" — and the run that produced this dump
  // passed, so every one of them came from the RTL.
  await expect(page.locator("body")).toContainText("dead state");
  await expect(page.locator("body")).toContainText("FSM");
});
