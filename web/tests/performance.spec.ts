/**
 * TAB 9 — Performance (§11.4b), and Prompt 10's acceptance criteria in the UI.
 *
 * Runs against `designs/deadlock`: two mailbox nodes writing to each other, each
 * refusing the peer while its own write is open. Two agents, one wait-for cycle,
 * and a control run of the same RTL with the bug compiled out.
 *
 * The two tests that matter most are the stall total and the deadlock chain.
 * §8.17 makes "the shares add to 100%, verifiable" part of the claim, so it is
 * verified where a user would check it — on screen — and not only in a unit
 * test. §8.18's report has to name the real agents and the real wires, so the
 * chain is read out of the DOM rather than asserted on a function.
 */

import { expect, test, type Page } from "@playwright/test";
import { waitForReady } from "./session";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

let deadlocked = "";
let healthy = "";

test.beforeAll(async ({ request }) => {
  const open = async (trace: string) => {
    const r = await request.post(`${BACKEND}/session`, {
      data: { trace_path: trace, rtl_paths: ["designs/deadlock"] },
    });
    expect(r.ok(), `could not open ${trace}: ${await r.text()}`).toBeTruthy();
    const sid = (await r.json()).session_id as string;
    await waitForReady(request, BACKEND, sid);
    return sid;
  };
  deadlocked = await open("designs/deadlock/dump.vtx");
  healthy = await open("designs/deadlock/dump_ok.vtx");
});

/** P5 keeps layout on disk, so a test that zooms leaves the next one zoomed. */
test.beforeEach(async ({ request }) => {
  for (const sid of [deadlocked, healthy]) {
    await request.put(`${BACKEND}/session/${sid}/layout`, {
      data: { signals: [], groups: [], radix: {}, bookmarks: [], cursors: [], zoom: null },
    });
  }
});

/**
 * Opening the session decides the default tab (§11.4b), and that decision
 * lands when `load()` resolves. Clicking before then is a race the user cannot
 * lose — the UI has not painted yet — but a test can, so wait for the loading
 * overlay to go before touching the tab strip.
 */
async function ready(page: Page, session: string): Promise<void> {
  await page.goto(`/?session=${session}`);
  await expect(page.locator(".loading")).toHaveCount(0);
  await expect(page.locator('[data-testid="status-range"]')).not.toBeEmpty();
}

async function open(page: Page, session: string): Promise<void> {
  await ready(page, session);
  await page.locator('[data-testid="tab-9"]').click();
  await expect(page.locator('[data-testid="performance-tab"]')).toBeVisible();
}

test("the Performance tab exists and is reachable by key", async ({ page }) => {
  await ready(page, deadlocked);
  await page.keyboard.press("9");
  await expect(page.locator('[data-testid="performance-tab"]')).toBeVisible();
  await expect(page.locator('[data-testid="tab-9"]')).toHaveAttribute("aria-selected", "true");
});

test("a failed analysis remains actionable and Retry loads real data", async ({ page }) => {
  let requests = 0;
  await page.route(`**/session/${healthy}/performance`, async (route) => {
    requests++;
    if (requests === 1) {
      await route.fulfill({ status: 500, json: { detail: "Cannot read protocol cache" } });
    } else await route.continue();
  });
  await ready(page, healthy);
  await page.getByTestId("tab-9").click();
  await expect(page.getByRole("alert")).toContainText("Cannot read protocol cache");
  // Let the former effect loop run; it used to reissue failures indefinitely.
  await page.waitForTimeout(500);
  expect(requests).toBe(1);
  await page.getByRole("button", { name: "Retry", exact: true }).click();
  await expect(page.getByTestId("perf-stall-total")).toHaveText("100.0%");
  expect(requests).toBe(2);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("the stall shares on screen add to exactly 100%", async ({ page }) => {
  // §8.17's claim, checked where the user would check it.
  await open(page, healthy);
  await expect(page.locator('[data-testid="perf-stall-total"]')).toHaveText("100.0%");

  const shares = await page.$$eval('[data-testid="perf-legend"] tbody tr td:nth-child(2)', (tds) =>
    tds.map((t) => parseFloat(t.textContent ?? "0")),
  );
  expect(shares.length).toBeGreaterThan(1);
  const sum = shares.reduce((a, b) => a + b, 0);
  expect(Math.abs(sum - 100)).toBeLessThan(0.05);
});

test("every bucket is drawn and named, including the ones at zero", async ({ page }) => {
  await open(page, healthy);
  for (const bucket of ["reset", "transfer", "address_stall", "idle"]) {
    await expect(page.locator(`[data-testid="perf-bucket-${bucket}"]`)).toBeVisible();
  }
  // `other` must be present as a row: a cascade with a gap has to be visible,
  // and a bucket that only appears when non-zero would hide it.
  await expect(page.locator('[data-testid="perf-bucket-other"]')).toBeVisible();
  await expect(page.locator('[data-testid="perf-stall-chart"] rect')).not.toHaveCount(0);
});

test("the injected deadlock is on screen with its agents and wires", async ({ page }) => {
  // Prompt 10's first acceptance criterion, through the interface.
  await open(page, deadlocked);
  const card = page.locator('[data-testid="perf-deadlock"]');
  await expect(card).toHaveCount(1);
  await expect(card).toContainText("DEADLOCK");
  await expect(card).toContainText("Cycle of 2 agents");

  const edges = page.locator('[data-testid="perf-wait-edge"]');
  await expect(edges).toHaveCount(2);
  const agents = await edges.evaluateAll((els) =>
    els.map((e) => e.getAttribute("data-agent") ?? ""),
  );
  expect(new Set(agents).size).toBe(2);
  // Real wires, not an abstraction: each row names an `awready` you could put a
  // cursor on.
  await expect(card.locator("code").first()).toContainText("awready");
});

test("why on a deadlock link opens Causal on that wire", async ({ page }) => {
  // §8.18's "[Why on each link]".
  await open(page, deadlocked);
  const resource = await page
    .locator('[data-testid="perf-wait-edge"] code')
    .first()
    .textContent();
  await page.locator('[data-testid="perf-why-edge"]').first().click();
  await expect(page.locator('[data-testid="tab-2"]')).toHaveAttribute("aria-selected", "true");

  // The chain has to open *on that wire* — a why button that lands on Causal
  // showing something else is worse than no button.
  const root = page.locator('[data-testid="causal-card"]').first();
  await expect(root).toBeVisible();
  await expect(root).toContainText(resource!.split(".").pop()!);
  // And it leaves the blocked node: the reason is in the peer, not here.
  await expect(page.locator('[data-testid="causal-card"]')).not.toHaveCount(1);
});

test("the same design without the bug reports no deadlock", async ({ page }) => {
  await open(page, healthy);
  await expect(page.locator('[data-testid="perf-deadlock"]')).toHaveCount(0);
  await expect(page.locator('[data-testid="perf-liveness"]')).toContainText(
    "No deadlock, livelock or starvation found",
  );
});

test("latency shows the distribution with percentiles, not an average", async ({ page }) => {
  await open(page, healthy);
  const p = page.locator('[data-testid="perf-percentiles"]').first();
  for (const label of ["p50", "p95", "p99", "min", "max"]) {
    await expect(p).toContainText(label);
  }
  await expect(page.locator('[data-testid="perf-latency-bin"]')).not.toHaveCount(0);
});

test("a latency outlier lands on its transaction", async ({ page }) => {
  await open(page, healthy);
  await page.locator('[data-testid="perf-outlier"]').first().click();
  await expect(page.locator('[data-testid="tab-8"]')).toHaveAttribute("aria-selected", "true");
  await expect(page.locator('[data-testid="txn-table"] tbody tr').first()).toBeVisible();
});

test("fairness is per master, with the Jain index", async ({ page }) => {
  await open(page, healthy);
  const bars = page.locator('[data-testid="perf-agent"]');
  await expect(bars).toHaveCount(2);
  // The healthy run serves both nodes equally.
  await expect(page.locator('[data-testid="perf-jain"]')).toHaveText("1.000");
});

test("bandwidth consumes the real throughput series and states its ceiling", async ({ page }) => {
  await open(page, healthy);
  const card = page.locator('[data-testid="perf-bandwidth"]');
  await expect(card).toBeVisible();
  await expect(card.locator(".perf-line")).toBeVisible();
  await expect(card).toContainText("bytes moved");
  await expect(card).toContainText("theoretical bytes/cycle");
  await expect(page.locator('[data-testid="perf-bandwidth-share"]')).toBeVisible();
});

test("selecting a window narrows the charts and moves Wave", async ({ page }) => {
  // §11.4b: select a window in any chart and everything else follows.
  await open(page, healthy);
  await expect(page.locator('[data-testid="perf-window-clear"]')).toHaveCount(0);
  const bytes = page.locator('[data-testid="perf-bandwidth"] .perf-percentiles > span').first();
  const beforeBytes = parseInt((await bytes.textContent())!);
  const narrowed = page.waitForResponse((r) => r.url().includes("/performance?t0="));

  const chart = page.locator('[data-testid="perf-stall-chart"]');
  const box = await chart.boundingBox();
  expect(box).not.toBeNull();
  const b = box!;
  await page.mouse.move(b.x + b.width * 0.2, b.y + b.height / 2);
  await page.mouse.down();
  await page.mouse.move(b.x + b.width * 0.5, b.y + b.height / 2, { steps: 8 });
  await page.mouse.up();
  const response = await narrowed;
  expect(response.ok(), await response.text()).toBeTruthy();

  const chip = page.locator('[data-testid="perf-window-clear"]');
  await expect(chip).toBeVisible();

  // The total still reads 100% — of what is on screen, which is the property
  // that would break if the shares were reused from the whole run.
  await expect(page.locator('[data-testid="perf-stall-total"]')).toHaveText("100.0%");
  await expect.poll(async () => parseInt((await bytes.textContent())!)).toBeLessThan(beforeBytes);
  const restored = page.waitForResponse((r) => r.url().endsWith("/performance"));

  await chip.click();
  await restored;
  await expect(chip).toHaveCount(0);
  await expect(bytes).toHaveText(`${beforeBytes} bytes moved`);
});
