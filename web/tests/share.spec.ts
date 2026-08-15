/**
 * §13.8 — a session survives being handed over.
 *
 * The `.vtsession` file itself is CLI territory (tests/test_share.py); what has
 * to hold here is the half that makes the file worth anything: the question is
 * part of the saved layout, so opening the trace again asks it again against
 * *this* dump — and does not steal the tab §11.4b chose.
 */

import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";
const QUERY = "why(tb_fifo_buggy.dut.full @ 455000)";

async function session(request: APIRequestContext): Promise<string> {
  return (await (await request.get(`${BACKEND}/`)).json()).default_session;
}

async function reset(request: APIRequestContext): Promise<void> {
  await request.put(`${BACKEND}/session/${await session(request)}/layout`, {
    data: { signals: [], groups: [], radix: {}, bookmarks: [], cursors: [], zoom: null },
  });
}

async function open(page: Page): Promise<void> {
  await page.goto("/");
  await expect(page.locator('[data-testid="signal-row"]').first()).toBeVisible();
  await page.waitForFunction(() => (window as any).__vtStore?.getState().ready === true);
}

test.describe("shareable sessions", () => {
  test.beforeEach(async ({ request }) => {
    await reset(request);
  });
  test.afterEach(async ({ request }) => {
    await reset(request);
  });

  test("the question is saved with the layout and asked again on open", async ({
    page,
    request,
  }) => {
    await open(page);
    await page.locator('[data-testid="tab-1"]').click();
    const bar = page.locator('[data-testid="query-bar"]');
    await bar.fill(QUERY);
    await bar.press("Enter");
    await expect(page.locator('[data-testid="causal-card"]').first()).toBeVisible({
      timeout: 15_000,
    });

    // It has to reach the file, not just the store: that file is what `share`
    // bundles and what a colleague applies.
    await expect
      .poll(async () => {
        const l = await (
          await request.get(`${BACKEND}/session/${await session(request)}/layout`)
        ).json();
        return l.query;
      }, { timeout: 10_000 })
      .toBe(QUERY);

    await page.reload();
    await open(page);
    // Nothing typed this time — the answer is waiting on the Causal tab.
    await page.locator('[data-testid="tab-2"]').click();
    await expect(page.locator(".causal-head .mono").first()).toContainText(QUERY, {
      timeout: 15_000,
    });
    await expect(page.locator('[data-testid="causal-card"]').first()).toBeVisible();
  });

  test("a restored question does not steal the tab the server chose", async ({ page }) => {
    // §11.4b decides where a session opens. A saved query fills the Causal tab;
    // it does not move the user into it.
    await open(page);
    const before = await page.evaluate(() => (window as any).__vtStore.getState().activeTab);
    await page.locator('[data-testid="tab-1"]').click();
    const bar = page.locator('[data-testid="query-bar"]');
    await bar.fill(QUERY);
    await bar.press("Enter");
    await expect(page.locator('[data-testid="causal-card"]').first()).toBeVisible({
      timeout: 15_000,
    });

    await page.reload();
    await open(page);
    await expect
      .poll(() => page.evaluate(() => (window as any).__vtStore.getState().activeTab))
      .toBe(before);
  });

  test("clearing the question clears it in the file too", async ({ page, request }) => {
    await open(page);
    await page.locator('[data-testid="tab-1"]').click();
    const bar = page.locator('[data-testid="query-bar"]');
    await bar.fill(QUERY);
    await bar.press("Enter");
    await expect(page.locator('[data-testid="causal-card"]').first()).toBeVisible({
      timeout: 15_000,
    });

    await page.evaluate(() => (window as any).__vtStore.getState().clearCausal());
    await expect
      .poll(async () => {
        const l = await (
          await request.get(`${BACKEND}/session/${await session(request)}/layout`)
        ).json();
        return l.query;
      }, { timeout: 10_000 })
      .toBe("");
  });
});
