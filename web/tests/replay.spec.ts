/**
 * §11.5 Replay mode, and §11.4's (b) subtrace and (c) repro sections.
 *
 * Runs against `designs/fifo_buggy`, whose bug is the §8.1 shape: `full` sticks
 * high because a read-side reset is tied low three hops away. That makes it the
 * right design for a story — the chain is short enough to read and the cause is
 * nowhere near the symptom.
 *
 * What is asserted here and not in `test_repro.py`: that replay is a *mode*.
 * §11.5 says it takes the screen, that one step is one subtrace event, and that
 * the waveform follows the step. Those are claims about the interface, and the
 * only place they can be checked is in a browser.
 */

import { expect, test, type Page } from "@playwright/test";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";
const SYMPTOM = "tb_fifo_buggy.dut.full";

let session = "";

test.beforeAll(async ({ request }) => {
  const r = await request.post(`${BACKEND}/session`, {
    data: { trace_path: "designs/fifo_buggy/dump.vtx", rtl_paths: ["designs/fifo_buggy"] },
  });
  expect(r.ok(), await r.text()).toBeTruthy();
  session = (await r.json()).session_id as string;
});

test.beforeEach(async ({ request }) => {
  await request.put(`${BACKEND}/session/${session}/layout`, {
    data: { signals: [], groups: [], radix: {}, bookmarks: [], cursors: [], zoom: null },
  });
});

/**
 * Land on the Causal tab with the golden chain already answered.
 *
 * The status bar, not just the absence of the loading overlay: before React
 * mounts there is no overlay either, so waiting only for that waits for nothing
 * and the keyboard tests race the app's own startup.
 */
async function withChain(page: Page): Promise<void> {
  await page.goto(`/?session=${session}`);
  await expect(page.locator(".loading")).toHaveCount(0);
  await expect(page.locator('[data-testid="status-range"]')).not.toBeEmpty();
  await page.evaluate(
    ([sig]) => (window as never as { __vtStore: { getState(): { runQueryText(q: string): Promise<void> } } })
      .__vtStore.getState()
      .runQueryText(`why(${sig})`),
    [SYMPTOM],
  );
  await expect(page.locator('[data-testid="causal-card"]').first()).toBeVisible();
}

test("the subtrace section minimises the chain to a narrative", async ({ page }) => {
  await withChain(page);
  const section = page.locator('[data-testid="subtrace-section"]');
  await expect(section).toBeVisible();
  await section.getByRole("button", { name: /minimise/ }).click();

  const rows = section.locator(".sub-row");
  await expect(rows.first()).toBeVisible();
  const n = await rows.count();
  expect(n).toBeGreaterThan(0);
  expect(n).toBeLessThanOrEqual(12); // §8.2 asks for a story, not a list

  // Amber marks causality and nothing else (§11.1): the root cause row.
  await expect(section.locator(".sub-row.root")).toHaveCount(1);
  await expect(section.locator(".sub-row.root")).toContainText("rd_rst_n");
  // Every row carries a sentence, not just numbers (§11.5).
  await expect(rows.first().locator(".sub-text")).not.toBeEmpty();
});

test("replay takes the screen and walks the steps", async ({ page }) => {
  await withChain(page);
  await page.locator('[data-testid="replay-open"]').click();

  const replay = page.locator('[data-testid="replay"]');
  await expect(replay).toBeVisible();
  // §11.5: it is a mode. The tab strip and the signal panel are not there.
  await expect(page.locator(".tab-strip")).toBeHidden();
  await expect(page.locator(".signal-panel")).toBeHidden();
  // ...but the waveform it is narrating still is.
  await expect(page.locator("canvas").first()).toBeVisible();

  await expect(replay.locator(".replay-count")).toContainText("Step 1 of");
  const first = await replay.locator(".replay-text").textContent();

  await page.locator('[data-testid="replay-next"]').click();
  await expect(replay.locator(".replay-count")).toContainText("Step 2 of");
  await expect(replay.locator(".replay-text")).not.toHaveText(first ?? "");

  // Arrows navigate, Escape leaves — §11.5.
  await page.keyboard.press("ArrowLeft");
  await expect(replay.locator(".replay-count")).toContainText("Step 1 of");
  await page.keyboard.press("Escape");
  await expect(replay).toHaveCount(0);
  await expect(page.locator(".tab-strip")).toBeVisible();
});

test("a step moves the cursor to its own event", async ({ page }) => {
  await withChain(page);
  await page.locator('[data-testid="replay-open"]').click();
  await expect(page.locator('[data-testid="replay"]')).toBeVisible();

  const read = () =>
    page.evaluate(
      () =>
        (window as never as { __vtStore: { getState(): { cursor: number | null } } })
          .__vtStore.getState().cursor,
    );
  const at1 = await read();
  await page.locator('[data-testid="replay-next"]').click();
  await expect(page.locator(".replay-count")).toContainText("Step 2 of");
  expect(await read()).not.toBe(at1);
});

test("r opens replay and the shortcut is advertised", async ({ page }) => {
  await withChain(page);
  await page.keyboard.press("r");
  await expect(page.locator('[data-testid="replay"]')).toBeVisible();
  await page.keyboard.press("Escape");

  await page.keyboard.press("?");
  const help = page.locator('[data-testid="help-overlay"]');
  await expect(help).toContainText("replay the chain");
  await expect(help).toContainText("minimal subtrace");
});

test("the repro section generates a testbench and says what it is", async ({ page }) => {
  await withChain(page);
  const section = page.locator('[data-testid="repro-section"]');
  await section.getByRole("button", { name: "generate", exact: true }).click();

  await expect(section.locator('[data-testid="repro-code"]')).toContainText("module tb_repro_full;");
  await expect(section.locator('[data-testid="repro-code"]')).toContainText("fifo_buggy");
  // §8.3 insists the two kinds are named apart, and the reason is on screen.
  await expect(section).toContainText(/minimal repro|focused testbench/);
  await expect(section).toContainText("input ports");
  // Not run, so it must not claim to have been.
  await expect(section.locator(".verdict")).toHaveText("not run");
});

test("generate and verify reports the simulator's own verdict", async ({ page }) => {
  test.setTimeout(120_000); // it compiles and runs a simulation
  await withChain(page);
  const section = page.locator('[data-testid="repro-section"]');
  await section.locator('[data-testid="repro-validate"]').click();

  const verdict = section.locator(".verdict");
  await expect(verdict).not.toHaveText("not run", { timeout: 90_000 });
  await expect(verdict).toHaveText(/verified with (iverilog|verilator)/);
  await expect(verdict).toHaveClass(/ok/);
});
