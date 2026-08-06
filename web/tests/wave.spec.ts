/**
 * Acceptance tests for the Wave tab, run in a real browser.
 *
 * The two criteria that matter for this stage are here: 60 fps during a
 * continuous scroll with 40 signal rows, and a layout that survives closing
 * and reopening the tab.
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const CANVAS = '[data-testid="wave-canvas"]';

async function openApp(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.locator('[data-testid="signal-row"]').first()).toBeVisible();
  // §11.4b: a trace with findings opens on Checks, not Wave. These tests are
  // about Wave, so they ask for it rather than relying on where the app lands.
  await page.locator('[data-testid="tab-1"]').click();
  await expect(page.locator(CANVAS)).toBeVisible();
  // Wait for the first wave payload, so timings measure steady-state drawing.
  await page.waitForFunction(() => (window as any).__vtDataReady === true, null, {
    timeout: 20_000,
  });
}

/** The backend, addressed directly rather than through the dev-server proxy. */
const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

/** Reset to a known layout so tests do not inherit each other's state. */
async function resetLayout(request: APIRequestContext): Promise<void> {
  const root = await (await request.get(`${BACKEND}/`)).json();
  const sid = root.default_session as string;
  await request.put(`${BACKEND}/session/${sid}/layout`, {
    data: { signals: [], groups: [], radix: {}, bookmarks: [], cursors: [], zoom: null },
  });
}

test.describe("Wave tab", () => {
  test.beforeEach(async ({ request }) => {
    await resetLayout(request);
  });

  test("renders the trace with the spec palette", async ({ page }) => {
    await openApp(page);

    // §11.2: deep slate-blue, explicitly not pure black.
    const bg = await page.evaluate(() =>
      getComputedStyle(document.documentElement).getPropertyValue("--bg-deep").trim(),
    );
    expect(bg.toLowerCase()).toBe("#0e1116");

    const body = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    expect(body).toBe("rgb(14, 17, 22)");

    // The fonts of §11.2, not library defaults.
    const rowFont = await page.evaluate(
      () => getComputedStyle(document.querySelector('[data-testid="signal-row"]')!).fontFamily,
    );
    expect(rowFont).toContain("JetBrains Mono");
    const tabFont = await page.evaluate(
      () => getComputedStyle(document.querySelector('[data-testid="tab-1"]')!).fontFamily,
    );
    expect(tabFont).toContain("IBM Plex Sans Condensed");

    // Compact row height from the token table.
    const h = await page.evaluate(
      () => document.querySelector('[data-testid="signal-row"]')!.getBoundingClientRect().height,
    );
    expect(Math.round(h)).toBe(20);
  });

  /**
   * A trace must sit on the same line as its name. The canvas offsets row 0 by
   * the ruler height; the panel offsets it by its own header. If those drift
   * apart every signal is drawn against the wrong label.
   */
  test("signal names line up with their traces", async ({ page }) => {
    await openApp(page);
    const { rowTop, canvasTop, rulerH } = await page.evaluate(() => {
      const row = document.querySelector('[data-testid="signal-row"]')!.getBoundingClientRect();
      const canvas = document.querySelector('[data-testid="wave-canvas"]')!.getBoundingClientRect();
      return { rowTop: row.top, canvasTop: canvas.top, rulerH: 26 };
    });
    expect(Math.abs(rowTop - (canvasTop + rulerH))).toBeLessThanOrEqual(1);
  });

  test("draws actual waveform pixels on the canvas", async ({ page }) => {
    await openApp(page);
    // A canvas that is uniformly background means nothing was drawn.
    const distinct = await page.evaluate(async () => {
      const c = document.querySelector("canvas") as HTMLCanvasElement;
      const bmp = await createImageBitmap(c);
      const off = document.createElement("canvas");
      off.width = bmp.width;
      off.height = bmp.height;
      const g = off.getContext("2d")!;
      g.drawImage(bmp, 0, 0);
      const d = g.getImageData(0, 0, off.width, off.height).data;
      const seen = new Set<number>();
      for (let i = 0; i < d.length; i += 4) {
        seen.add((d[i] << 16) | (d[i + 1] << 8) | d[i + 2]);
      }
      return seen.size;
    });
    // Background, panel bands, grid, and at least one trace colour.
    expect(distinct).toBeGreaterThan(4);
  });

  /**
   * Acceptance criterion: 60 fps on a continuous scroll with 40 signals.
   *
   * fifo_async declares 29 signals, so the list is filled to 40 rows by adding
   * signals more than once — legitimate in a wave viewer, and it is the number
   * of rows drawn per frame that the criterion is about.
   */
  test("sustains 60fps while scrolling with 40 signal rows", async ({ page }) => {
    await openApp(page);

    await page.evaluate(() => {
      const store = (window as any).__vtStore;
      const s = store.getState();
      const rows: unknown[] = [];
      for (let i = 0; i < 40; i++) {
        const sig = s.signals[i % s.signals.length];
        rows.push({ kind: "signal", handle: sig.handle, path: sig.path });
      }
      store.setState({ rows });
    });
    await page.waitForTimeout(600);

    const rowCount = await page.locator('[data-testid="signal-row"]').count();
    expect(rowCount).toBe(40);

    // Sample frame paint times reported by the worker during a real pan.
    await page.evaluate(() => ((window as any).__vtFrames = []));
    const box = (await page.locator(CANVAS).boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);

    const STEPS = 90;
    for (let i = 0; i < STEPS; i++) {
      await page.mouse.wheel(24, 0);
      await page.waitForTimeout(8);
    }
    await page.waitForTimeout(200);

    const frames: number[] = await page.evaluate(() => (window as any).__vtFrames ?? []);
    expect(frames.length).toBeGreaterThan(20);

    const sorted = [...frames].sort((a, b) => a - b);
    const p95 = sorted[Math.floor(sorted.length * 0.95)];
    const avg = frames.reduce((a, b) => a + b, 0) / frames.length;

    // 60fps leaves 16.7ms per frame for everything; the draw itself must fit
    // well inside that to leave room for compositing.
    console.log(`frames=${frames.length} avg=${avg.toFixed(2)}ms p95=${p95.toFixed(2)}ms`);
    expect(avg).toBeLessThan(16.7);
    expect(p95).toBeLessThan(16.7);
  });

  test("scroll pans, ctrl+scroll zooms", async ({ page }) => {
    await openApp(page);
    const box = (await page.locator(CANVAS).boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);

    const read = () => page.evaluate(() => (window as any).__vtStore.getState().view);

    // Zoom in first: the initial view is the whole trace, and panning a view
    // that already spans everything correctly has nowhere to go.
    await page.keyboard.down("Control");
    await page.mouse.wheel(0, -300);
    await page.keyboard.up("Control");
    await page.waitForTimeout(120);
    const start = await read();
    const full = await page.evaluate(() => {
      const b = (window as any).__vtStore.getState().bounds;
      return b.t1 - b.t0;
    });
    expect(start.t1 - start.t0).toBeLessThan(full);

    await page.mouse.wheel(120, 0);
    await page.waitForTimeout(120);
    const panned = await read();
    expect(panned.t0).toBeGreaterThan(start.t0);
    // Panning preserves the span.
    expect(panned.t1 - panned.t0).toBeCloseTo(start.t1 - start.t0, 1);

    await page.keyboard.down("Control");
    await page.mouse.wheel(0, -240);
    await page.keyboard.up("Control");
    await page.waitForTimeout(120);
    const zoomed = await read();
    expect(zoomed.t1 - zoomed.t0).toBeLessThan(panned.t1 - panned.t0);

    // Zooming back out returns to the full trace and no further.
    await page.keyboard.down("Control");
    for (let i = 0; i < 12; i++) await page.mouse.wheel(0, 300);
    await page.keyboard.up("Control");
    await page.waitForTimeout(150);
    const out = await read();
    const bounds = await page.evaluate(() => (window as any).__vtStore.getState().bounds);
    expect(Math.round(out.t1 - out.t0)).toBe(Math.round(bounds.t1 - bounds.t0));
  });

  test("clicking sets the cursor", async ({ page }) => {
    await openApp(page);
    const box = (await page.locator(CANVAS).boundingBox())!;
    await page.mouse.click(box.x + box.width * 0.4, box.y + 120);
    await expect(page.locator('[data-testid="status-cursor"]')).not.toHaveText("cursor: —");
  });

  test("radix cycles per signal and the ruler switches to cycles", async ({ page }) => {
    await openApp(page);
    const radix = page.locator('[data-testid="radix-button"]').first();
    await expect(radix).toHaveText("hex");
    await radix.click();
    await expect(radix).toHaveText("dec");
    await radix.click();
    await expect(radix).toHaveText("bin");

    const ruler = page.locator('[data-testid="ruler-toggle"]');
    await expect(ruler).toHaveText("time");
    await ruler.click();
    await expect(ruler).toHaveText("cycles");
  });

  test("the palette finds a signal and adds it", async ({ page }) => {
    await openApp(page);
    const before = await page.locator('[data-testid="signal-row"]').count();
    await page.keyboard.press("Control+p");
    await expect(page.locator('[data-testid="palette"]')).toBeVisible();
    await page.locator('[data-testid="palette-input"]').fill("wr_ptr");
    await page.keyboard.press("Enter");
    await expect(page.locator('[data-testid="palette"]')).toBeHidden();
    expect(await page.locator('[data-testid="signal-row"]').count()).toBe(before + 1);
  });

  test("number keys switch tabs, ? opens the shortcut overlay", async ({ page }) => {
    await openApp(page);
    await expect(page.locator('[data-testid="tab-1"]')).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("3");
    await expect(page.locator('[data-testid="tab-3"]')).toHaveAttribute("aria-selected", "true");
    await page.keyboard.press("?");
    await expect(page.locator('[data-testid="help-overlay"]')).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.locator('[data-testid="help-overlay"]')).toBeHidden();
  });
});

test.describe("layout persistence", () => {
  /**
   * Acceptance criterion: reopen the browser tab and the layout is exactly as
   * it was left.
   */
  test("survives a full page reload", async ({ page, request }) => {
    await resetLayout(request);
    await openApp(page);

    // Make a set of changes across every persisted dimension.
    await page.evaluate(() => {
      const store = (window as any).__vtStore;
      const s = store.getState();
      const pick = s.signals.filter((x: any) => x.width > 1).slice(0, 3);
      store.setState({
        rows: pick.map((x: any) => ({ kind: "signal", handle: x.handle, path: x.path })),
      });
    });
    await page.locator('[data-testid="radix-button"]').first().click(); // hex -> dec
    await page.locator('[data-testid="density-toggle"]').click(); // compact -> tall
    await page.locator('[data-testid="ruler-toggle"]').click(); // time -> cycles

    const box = (await page.locator(CANVAS).boundingBox())!;
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.keyboard.down("Control");
    await page.mouse.wheel(0, -300);
    await page.keyboard.up("Control");
    await page.mouse.click(box.x + box.width * 0.35, box.y + 100);

    const before = await page.evaluate(() => {
      const s = (window as any).__vtStore.getState();
      return {
        rows: s.rows,
        radix: s.radix,
        rowH: s.rowH,
        rulerMode: s.rulerMode,
        view: { t0: Math.round(s.view.t0), t1: Math.round(s.view.t1) },
        cursor: s.cursor,
      };
    });

    // Give the debounced save time to land, then confirm it actually reached
    // the server rather than trusting the timer.
    await page.waitForFunction(() => ((window as any).__vtSaves ?? 0) > 0, null, { timeout: 5000 });

    await page.reload();
    await expect(page.locator('[data-testid="signal-row"]').first()).toBeVisible();
    await page.waitForFunction(() => (window as any).__vtStore?.getState().ready === true);

    const after = await page.evaluate(() => {
      const s = (window as any).__vtStore.getState();
      return {
        rows: s.rows,
        radix: s.radix,
        rowH: s.rowH,
        rulerMode: s.rulerMode,
        view: { t0: Math.round(s.view.t0), t1: Math.round(s.view.t1) },
        cursor: s.cursor,
      };
    });

    expect(after.rows).toEqual(before.rows);
    expect(after.radix).toEqual(before.radix);
    expect(after.rulerMode).toBe(before.rulerMode);
    expect(after.cursor).toBe(before.cursor);
    expect(after.view.t0).toBe(before.view.t0);
    expect(after.view.t1).toBe(before.view.t1);
    // Row height is chrome rather than layout data, but it is persisted too.
    expect(after.rowH).toBe(before.rowH);
  });

  test("a reordered list keeps its order", async ({ page, request }) => {
    await resetLayout(request);
    await openApp(page);

    const order = () =>
      page.locator('[data-testid="signal-row"]').evaluateAll((els) =>
        els.map((e) => e.getAttribute("data-path")),
      );

    const original = await order();
    await page.evaluate(() => (window as any).__vtStore.getState().moveRow(0, 4));
    const moved = await order();
    expect(moved).not.toEqual(original);

    await page.waitForFunction(() => ((window as any).__vtSaves ?? 0) > 0, null, { timeout: 5000 });
    await page.reload();
    await expect(page.locator('[data-testid="signal-row"]').first()).toBeVisible();
    expect(await order()).toEqual(moved);
  });
});
