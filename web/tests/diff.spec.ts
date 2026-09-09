/**
 * TAB 5 — Diff (§11.4, §8.7).
 *
 * Runs against `designs/deadlock`, built twice from one source: once clean and
 * once with the bug compiled in. Everything the two runs disagree about
 * therefore traces back to the injection, which is what makes "the first
 * divergence" a checkable claim rather than a plausible one.
 *
 * The assertions that carry the section: the alignment states which anchors it
 * matched *and* that the timescales were normalised (§8.7 calls that mandatory);
 * the first divergence is one card; the two chains sit side by side with the
 * first differing node in magenta and nowhere else (§11.2); and `n`/`p` walk the
 * list (§11.7).
 */

import { expect, test, type Page } from "@playwright/test";
import { readFile } from "node:fs/promises";
import { waitForReady } from "./session";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

let clean = "";

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
  clean = await open("designs/deadlock/dump_ok.vtx");
  // Opened so the tab has a second run to offer; the picker lists what the
  // server holds (§10.1).
  await open("designs/deadlock/dump.vtx");
});

test.beforeEach(async ({ request }) => {
  const current = await request.get(`${BACKEND}/session/${clean}/layout`);
  const saved = await request.put(`${BACKEND}/session/${clean}/layout`, { data: {
    ...await current.json(), diffOptions: { other: "", strategy: "cycle", anchor: "", marksA: "", marksB: "", ignore: [] },
  } });
  expect(saved.ok(), await saved.text()).toBeTruthy();
});

/**
 * Wait until the app is actually running.
 *
 * `.loading` having count 0 is not enough on its own: before React mounts there
 * is no overlay *either*, so the assertion passes on an empty page, a keypress
 * lands with no listener attached, and the tab never changes. The status bar is
 * only filled once `load()` has resolved, which is also when the default tab is
 * decided (§11.4b) — so waiting for it is waiting for the app to stop moving.
 */
async function ready(page: Page, session: string): Promise<void> {
  await page.goto(`/?session=${session}`);
  await expect(page.locator(".loading")).toHaveCount(0);
  await expect(page.locator('[data-testid="status-range"]')).not.toBeEmpty();
}

async function openDiff(page: Page): Promise<void> {
  await ready(page, clean);
  await page.locator('[data-testid="tab-5"]').click();
  await expect(page.locator('[data-testid="diff-other"]')).toBeVisible();
}

async function compare(page: Page): Promise<void> {
  await openDiff(page);
  // Selected by path, not by label: the label is whatever it takes to be
  // unambiguous among the traces this server happens to have open.
  await page
    .locator('[data-testid="diff-other"]')
    .selectOption(await buggyTrace(page));
  await page.locator('[data-testid="diff-run"]').click();
  await expect(page.locator('[data-testid="diff-first"]')).toBeVisible({ timeout: 30_000 });
}

/** The value of the option pointing at `designs/deadlock/dump.vtx`. */
async function buggyTrace(page: Page): Promise<string> {
  let found = "";
  // The selector mounts before /sessions returns. Waiting for the selector
  // alone tested network scheduling instead of whether the real run is offered.
  await expect.poll(async () => {
    const values = await page
      .locator('[data-testid="diff-other"] option')
      .evaluateAll((els) => els.map((e) => (e as HTMLOptionElement).value));
    found = values.find((v) => v.replace(/\\/g, "/").endsWith("deadlock/dump.vtx")) ?? "";
    return found;
  }).not.toBe("");
  return found;
}

test("the tab is built and reachable by key", async ({ page }) => {
  await ready(page, clean);
  await page.keyboard.press("5");
  await expect(page.locator('[data-testid="tab-5"]')).toHaveAttribute("aria-selected", "true");
  await expect(page.locator('[data-testid="diff-run"]')).toBeVisible();
});

test("the picker offers the other runs, named apart", async ({ page }) => {
  await openDiff(page);
  const select = page.locator('[data-testid="diff-other"]');
  const values = await select
    .locator("option")
    .evaluateAll((els) => els.map((e) => (e as HTMLOptionElement).value));
  // This run is not on offer; the other one is.
  expect(values.some((v) => v.replace(/\\/g, "/").endsWith("deadlock/dump_ok.vtx"))).toBe(false);
  expect(values.some((v) => v.replace(/\\/g, "/").endsWith("deadlock/dump.vtx"))).toBe(true);

  // Every design dumps to `dump.vtx`, so a picker of basenames would be a list
  // of identical labels. They have to be distinguishable.
  const labels = (await select.locator("option").allTextContents()).slice(1);
  expect(new Set(labels).size).toBe(labels.length);
});

test("it says what it aligned on and that the timescales were normalised", async ({ page }) => {
  await compare(page);
  const align = page.locator(".diff-align");
  await expect(align).toContainText("aligned on");
  await expect(align).toContainText("anchor(s) matched");
  // §8.7's mandatory step, stated rather than assumed.
  await expect(align).toContainText(/fs per tick/);
});

test("the first divergence is one card, with both values", async ({ page }) => {
  await compare(page);
  const card = page.locator('[data-testid="diff-first"]');
  await expect(card).toContainText("First divergence at c");
  await expect(card.locator(".diff-card-sig")).not.toBeEmpty();
  await expect(card.locator(".diff-va")).not.toBeEmpty();
  await expect(card.locator(".diff-vb")).not.toBeEmpty();
  // The injected difference is a compile-time constant, and the card says so
  // rather than presenting a parameter as a run-time divergence.
  await expect(card).toContainText("build difference");
});

test("n and p walk the divergences", async ({ page }) => {
  await compare(page);
  const card = page.locator('[data-testid="diff-first"]');
  await expect(card).toContainText("First divergence");
  await page.keyboard.press("n");
  await expect(card).toContainText("Divergence 2 at c");
  const selected = (await card.locator(".diff-card-sig").textContent())!.trim();
  await expect(page.getByTestId("diff-wave")).toHaveAttribute("data-signal", selected);
  await expect(page.locator(".diff-chain").first().locator(".diff-node").first()).toContainText(selected);
  await page.keyboard.press("p");
  await expect(card).toContainText("First divergence");
});

test("aligned real waves and regression export follow the inspected divergence", async ({ page }) => {
  await compare(page);
  const wave = page.getByTestId("diff-wave");
  await expect(wave.locator("svg")).toBeVisible();
  await expect(wave.locator('g[data-different="true"]').first()).toBeVisible();
  const exported = page.waitForEvent("download");
  await page.getByRole("button", { name: "Export regression JSON" }).click();
  const download = await exported;
  const report = JSON.parse(await readFile((await download.path())!, "utf8"));
  expect(report.focus).toBe((await page.locator(".diff-card-sig").textContent())!.trim());
  expect(report.wave.signal).toBe(report.focus);
  expect(report.wave.segments.some((s: { different: boolean }) => s.different)).toBe(true);
  expect(report.why_a.signal).toContain(report.focus);
  const first = wave.locator('g[data-different="true"]').first();
  const time = Number(await first.getAttribute("data-time-a"));
  await first.click();
  await expect(page.getByTestId("tab-1")).toHaveAttribute("aria-selected", "true");
  await expect.poll(async () => {
    const layout = await page.request.get(`${BACKEND}/session/${clean}/layout`);
    return (await layout.json()).cursors[0];
  }).toBe(time);
});

test("manual anchors affect alignment, reject invalid marks, and survive reload", async ({ page }) => {
  await openDiff(page);
  await page.getByTestId("diff-other").selectOption(await buggyTrace(page));
  await page.getByTestId("diff-strategy").selectOption("manual");
  await page.getByLabel("Anchors A", { exact: true }).fill("0, 100");
  await page.getByLabel("Anchors B", { exact: true }).fill("0, 100");
  await page.getByTestId("diff-run").click();
  await expect(page.locator(".diff-align")).toContainText("aligned on manual · 2 anchor(s)");
  await expect(page.getByTestId("diff-first")).toContainText("at anchor");
  await expect.poll(async () => (await (await page.request.get(`${BACKEND}/session/${clean}/layout`)).json()).diffOptions.marksA).toBe("0, 100");
  await page.reload();
  // As in ready(): wait for bootstrap to restore the saved session state.
  await expect(page.getByTestId("status-range")).not.toBeEmpty();
  await expect(page.locator(".loading")).toHaveCount(0);
  await page.getByTestId("tab-5").click();
  await expect(page.getByTestId("diff-strategy")).toHaveValue("manual");
  await expect(page.getByLabel("Anchors A", { exact: true })).toHaveValue("0, 100");
  await page.getByLabel("Anchors A", { exact: true }).fill("0, 0");
  await page.getByTestId("diff-run").click();
  await expect(page.getByRole("alert")).toContainText("strictly increasing");
  await expect(page.getByTestId("diff-first")).toHaveCount(0);
});

test("B's causal node opens B's session and native timestamp, never A's", async ({ page, context }) => {
  await compare(page);
  const popup = context.waitForEvent("page");
  await page.locator(".diff-chain").nth(1).locator(".diff-node").first().click();
  const other = await popup;
  await other.waitForLoadState();
  const target = new URL(other.url());
  expect(target.searchParams.get("session")).not.toBe(clean);
  await expect(other.getByTestId("status-range")).not.toBeEmpty();
  await expect(other.getByTestId("tab-3")).toHaveAttribute("aria-selected", "true");
  await expect.poll(async () => {
    const r = await page.request.get(`${BACKEND}/session/${target.searchParams.get("session")}/layout`);
    return (await r.json()).cursors[0];
  }).toBe(Number(target.searchParams.get("time")));
  await other.close();
});

test("the two chains sit side by side with one node in magenta", async ({ page }) => {
  await compare(page);
  const chains = page.locator(".diff-chains .diff-chain");
  await expect(chains).toHaveCount(2);
  // §11.2: magenta is the Diff tab's alone, and marks exactly one line.
  await expect(page.locator(".diff-node.parted")).toHaveCount(2);
});

test("a failed focus request keeps the previous answer coherent and can be retried", async ({ page }) => {
  await compare(page);
  const card = page.getByTestId("diff-first");
  const first = (await card.locator(".diff-card-sig").textContent())!.trim();
  // Inject only a transport failure; both the baseline and retry use the real
  // backend, native stores, alignment and causal analysis.
  await page.route("**/session/*/diff", (route) => route.fulfill({
    status: 503, contentType: "application/json", body: JSON.stringify({ detail: "temporary backend failure" }),
  }), { times: 1 });
  await page.keyboard.press("n");
  await expect(page.getByRole("alert")).toContainText("temporary backend failure");
  await expect(card.locator(".diff-card-sig")).toHaveText(first);
  await expect(page.getByTestId("diff-wave")).toHaveAttribute("data-signal", first);
  await expect(page.locator(".diff-chain").first().locator(".diff-node").first()).toContainText(first);
  await page.keyboard.press("n");
  await expect(card).toContainText("Divergence 2 at c");
  await expect(card.locator(".diff-card-sig")).not.toHaveText(first);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("ignoring a signal takes it out and compares again", async ({ page }) => {
  await compare(page);
  const first = page.locator('[data-testid="diff-first"] .diff-card-sig');
  const name = (await first.textContent())?.trim() ?? "";
  expect(name).not.toBe("");

  await page.locator(".diff-row").first().hover();
  await page.locator(".diff-row").first().getByRole("button", { name: "ignore" }).click();

  await expect(page.locator('[data-testid="diff-first"]')).toBeVisible({ timeout: 30_000 });
  await expect(first).not.toHaveText(name);
  await expect(
    page.locator(".diff-section", { hasText: "Diverging signals" }),
  ).toContainText("ignoring:");
});

test("the transaction-level answer is reported beside the signal one", async ({ page }) => {
  await compare(page);
  const section = page.locator(".diff-section", { hasText: "First diverging transaction" });
  await expect(section.locator(".diff-txn").first()).toBeVisible();
  await expect(section.locator(".diff-txn").first()).toContainText(/WRITE|READ/);
});
