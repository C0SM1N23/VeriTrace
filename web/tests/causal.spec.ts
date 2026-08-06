/**
 * The main route through the application (§11.4): right-click a signal in Wave,
 * ask why, land in Causal, click a card, watch every panel follow (§11.6).
 *
 * Runs against the buggy FIFO, so the chain has a real root cause to find.
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";
const CANVAS = '[data-testid="wave-canvas"]';

async function reset(request: APIRequestContext): Promise<void> {
  const root = await (await request.get(`${BACKEND}/`)).json();
  await request.put(`${BACKEND}/session/${root.default_session}/layout`, {
    data: { signals: [], groups: [], radix: {}, bookmarks: [], cursors: [], zoom: null },
  });
}

async function open(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.locator('[data-testid="signal-row"]').first()).toBeVisible();
  // §11.4b lands a trace with findings on Checks; the route these tests follow
  // starts from the waveform, so ask for it explicitly.
  await page.locator('[data-testid="tab-1"]').click();
  await expect(page.locator(CANVAS)).toBeVisible();
  await page.waitForFunction(() => (window as any).__vtDataReady === true, null, {
    timeout: 20_000,
  });
}

/** Right-click the row for `path` in the waveform. */
async function rightClickSignal(page: Page, path: string): Promise<void> {
  const idx = await page.evaluate((p) => {
    const s = (window as any).__vtStore.getState();
    return s.rows.findIndex((r: any) => r.kind === "signal" && r.path === p);
  }, path);
  expect(idx).toBeGreaterThanOrEqual(0);
  const box = (await page.locator(CANVAS).boundingBox())!;
  const rowH = await page.evaluate(() => (window as any).__vtStore.getState().rowH);
  await page.mouse.move(box.x + box.width * 0.8, box.y + 26 + idx * rowH + rowH / 2);
  await page.mouse.down({ button: "right" });
  await page.mouse.up({ button: "right" });
}

test.describe("causality", () => {
  test.beforeEach(async ({ request }) => {
    await reset(request);
  });

  test("the design is served with RTL and full correlation", async ({ request }) => {
    const root = await (await request.get(`${BACKEND}/`)).json();
    const status = await (
      await request.get(`${BACKEND}/session/${root.default_session}/status`)
    ).json();
    expect(status.has_rtl).toBe(true);
    expect(status.correlation_rate).toBeGreaterThanOrEqual(98);
  });

  test("right-click offers why, and it lands on Causal with the root cause", async ({ page }) => {
    await open(page);
    await rightClickSignal(page, "tb_fifo_buggy.dut.full");

    const menu = page.locator('[data-testid="wave-menu"]');
    await expect(menu).toBeVisible();
    await expect(menu).toContainText("Why is this value here?");
    await page.locator('[data-testid="ctx-why"]').click();

    // Causal becomes the active tab.
    await expect(page.locator('[data-testid="tab-2"]')).toHaveAttribute("aria-selected", "true");
    const cards = page.locator('[data-testid="causal-card"]');
    await expect(cards.first()).toBeVisible({ timeout: 15_000 });
    await expect(cards.first()).toContainText("tb_fifo_buggy.dut.full");

    // The injected cause is on the open (primary) path, not buried.
    await expect(
      page.locator('[data-testid="causal-card"][data-signal="tb_fifo_buggy.dut.rd_rst_n"]'),
    ).toBeVisible();
  });

  test("secondary branches are collapsed but reachable", async ({ page }) => {
    await open(page);
    await rightClickSignal(page, "tb_fifo_buggy.dut.full");
    await page.locator('[data-testid="ctx-why"]').click();
    await expect(page.locator('[data-testid="causal-card"]').first()).toBeVisible();

    const before = await page.locator('[data-testid="causal-card"]').count();

    // A collapsed branch shows the closed marker. Nothing is hidden — the
    // likely path is simply the one already open (§11.4).
    const collapsed = page.locator('[data-testid="branch-toggle"]', { hasText: "▸" });
    expect(await collapsed.count()).toBeGreaterThan(0);
    await collapsed.first().click();

    expect(await page.locator('[data-testid="causal-card"]').count()).toBeGreaterThan(before);
  });

  test("clicking a causal card moves Wave and Source together (§11.6)", async ({ page }) => {
    await open(page);
    await rightClickSignal(page, "tb_fifo_buggy.dut.full");
    await page.locator('[data-testid="ctx-why"]').click();
    await expect(page.locator('[data-testid="causal-card"]').first()).toBeVisible();

    const target = page.locator(
      '[data-testid="causal-card"][data-signal="tb_fifo_buggy.dut.rd_rst_n"]',
    );
    await target.click();

    // One click, every panel: cursor, wave selection and source location.
    const state = await page.evaluate(() => {
      const s = (window as any).__vtStore.getState();
      return {
        cursor: s.cursor,
        selected: s.selected,
        sourceLoc: s.sourceLoc,
        selectedPath: s.selected !== null ? s.signalsByHandle.get(s.selected)?.path : null,
      };
    });
    expect(state.cursor).not.toBeNull();
    expect(state.selectedPath).toBe("tb_fifo_buggy.dut.rd_rst_n");
    expect(state.sourceLoc?.file).toBe("fifo_buggy.sv");
    expect(state.sourceLoc?.line).toBeGreaterThan(0);
  });

  test("Source shows the RTL with a causality gutter and value inlays", async ({ page }) => {
    await open(page);
    await rightClickSignal(page, "tb_fifo_buggy.dut.full");
    await page.locator('[data-testid="ctx-why"]').click();
    await expect(page.locator('[data-testid="causal-card"]').first()).toBeVisible();

    await page.keyboard.press("3");
    await expect(page.locator('[data-testid="tab-3"]')).toHaveAttribute("aria-selected", "true");
    const editor = page.locator('[data-testid="source-editor"]');
    await expect(editor).toBeVisible();
    await expect(editor).toContainText("module fifo_buggy", { timeout: 15_000 });

    // Amber gutter bars mark the lines of the current chain. CodeMirror also
    // renders a hidden spacer with the same class, so count the drawn ones.
    const bars = await page.evaluate(() =>
      [...document.querySelectorAll(".cm-causal-bar")]
        .map((el) => ({
          w: (el as HTMLElement).getBoundingClientRect().width,
          h: (el as HTMLElement).getBoundingClientRect().height,
          color: getComputedStyle(el as Element).backgroundColor,
        }))
        .filter((b) => b.w > 0 && b.h > 0),
    );
    expect(bars.length).toBeGreaterThan(0);
    expect(bars[0].color).toBe("rgb(232, 163, 61)"); // --causal

    // Inlays show values at the cursor time.
    await expect(page.locator(".cm-inlay").first()).toBeVisible();
    const inlay = await page.locator(".cm-inlay").first().textContent();
    expect(inlay).toMatch(/=/);
  });

  test("moving the cursor in Wave updates the Source inlays", async ({ page }) => {
    await open(page);
    await rightClickSignal(page, "tb_fifo_buggy.dut.full");
    await page.locator('[data-testid="ctx-why"]').click();
    await expect(page.locator('[data-testid="causal-card"]').first()).toBeVisible();
    await page.keyboard.press("3");
    await expect(page.locator(".cm-inlay").first()).toBeVisible({ timeout: 15_000 });

    const first = await page.locator(".cm-inlay").allTextContents();
    // Move the cursor to a very different time; the numbers must follow.
    await page.evaluate(() => (window as any).__vtStore.getState().setCursor(30_000));
    await page.waitForTimeout(300);
    const second = await page.locator(".cm-inlay").allTextContents();
    expect(second.join("|")).not.toBe(first.join("|"));
  });

  test("the query bar runs why() directly", async ({ page }) => {
    await open(page);
    const bar = page.locator('[data-testid="query-bar"]');
    await expect(bar).toBeEnabled();
    await bar.fill("why(tb_fifo_buggy.dut.full @ 455000)");
    await bar.press("Enter");
    await expect(page.locator('[data-testid="causal-card"]').first()).toBeVisible({
      timeout: 15_000,
    });
  });

  test("a bad query reports the problem instead of failing silently", async ({ page }) => {
    await open(page);
    const bar = page.locator('[data-testid="query-bar"]');
    await bar.fill("cone(tb_fifo_buggy.dut.full)");
    await bar.press("Enter");
    await expect(page.locator(".pane-note.error")).toBeVisible({ timeout: 15_000 });
  });

  test("amber is spent on causality and nothing else", async ({ page }) => {
    /** §11.1: the interface is monochrome until something enters a chain. */
    await open(page);
    const amberBefore = await page.evaluate(() =>
      [...document.querySelectorAll("*")].filter((el) => {
        const c = getComputedStyle(el as Element);
        return c.borderLeftColor === "rgb(232, 163, 61)" || c.color === "rgb(232, 163, 61)";
      }).length,
    );
    expect(amberBefore).toBe(0);

    await rightClickSignal(page, "tb_fifo_buggy.dut.full");
    await page.locator('[data-testid="ctx-why"]').click();
    await expect(page.locator('[data-testid="causal-card"]').first()).toBeVisible();

    const amberAfter = await page.evaluate(() =>
      [...document.querySelectorAll("*")].filter((el) => {
        const c = getComputedStyle(el as Element);
        return c.borderLeftColor === "rgb(232, 163, 61)" || c.color === "rgb(232, 163, 61)";
      }).length,
    );
    expect(amberAfter).toBeGreaterThan(0);
  });
});
