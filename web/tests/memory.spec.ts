/**
 * TAB 10 — Memory (§11.4b), and Prompt 11's acceptance criteria in the UI.
 *
 * Runs against `designs/sdram`: a minimal SDR SDRAM controller with three
 * timing violations injected, one from each category, plus the same RTL
 * compiled clean as the control.
 *
 * The two tests that matter most are the violation list and the bank
 * timeline. §8.20 warns against a report that only says something looks
 * stuck, so the violations are read out of the DOM with their cycles and
 * limits; and the timeline is checked for gap-free coverage, because a
 * timeline with holes is not one whose shape can be trusted.
 */

import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { cpSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";
import { waitForReady } from "./session";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

let violating = "";
let clean = "";

test.beforeAll(async ({ request }) => {
  const open = async (trace: string) => {
    const r = await request.post(`${BACKEND}/session`, { data: { trace_path: trace } });
    expect(r.ok(), `could not open ${trace}: ${await r.text()}`).toBeTruthy();
    const sid = (await r.json()).session_id as string;
    await waitForReady(request, BACKEND, sid);
    return sid;
  };
  violating = await open("designs/sdram/dump.vtx");
  clean = await open("designs/sdram/dump_ok.vtx");
});

test.beforeEach(async ({ request }) => {
  for (const sid of [violating, clean]) {
    await request.put(`${BACKEND}/session/${sid}/layout`, {
      data: { signals: [], groups: [], radix: {}, bookmarks: [], cursors: [], zoom: null },
    });
  }
});

async function ready(page: Page, session: string): Promise<void> {
  await page.goto(`/?session=${session}`);
  await expect(page.locator(".loading")).toHaveCount(0);
  await expect(page.locator('[data-testid="status-range"]')).not.toBeEmpty();
}

async function open(page: Page, session: string): Promise<void> {
  await ready(page, session);
  await page.locator('[data-testid="tab-10"]').click();
  await expect(page.locator('[data-testid="memory-tab"]')).toBeVisible();
}

test("a design with a memory interface opens on Memory", async ({ page }) => {
  // §11.4b: memory outranks the interface count and the findings list.
  await ready(page, violating);
  await expect(page.locator('[data-testid="tab-10"]')).toHaveAttribute("aria-selected", "true");
  await expect(page.locator('[data-testid="memory-tab"]')).toBeVisible();
});

test("the Memory tab is reachable by key", async ({ page }) => {
  await ready(page, violating);
  await page.locator('[data-testid="tab-1"]').click();
  await page.keyboard.press("0");
  await expect(page.locator('[data-testid="tab-10"]')).toHaveAttribute("aria-selected", "true");
});

test("real timing violations are shown without flagging the legal four-activate burst", async ({ page }) => {
  // Prompt 11's first acceptance criterion, through the interface.
  await open(page, violating);
  await expect(page.locator('[data-testid="mem-violation-count"]')).toContainText(
    "3 timing violations",
  );
  for (const constraint of ["tRCD", "tRP", "tRFC"]) {
    await expect(page.locator(`[data-testid="mem-violation-${constraint}"]`)).toBeVisible();
  }
  await expect(page.locator('[data-testid="mem-violation-row"]')).toHaveCount(3);
  await expect(page.getByTestId("mem-violation-tFAW")).toHaveCount(0);

  // Each row names both commands and the measured-versus-required gap, not
  // just that something is wrong.
  const tRCD = page.locator('[data-testid="mem-violation-tRCD"]');
  await expect(tRCD).toContainText("ACTIVATE@c8");
  await expect(tRCD).toContainText("READ@c9");
  await expect(tRCD).toContainText("min 2");
});

test("constraints that held are listed, not merely absent", async ({ page }) => {
  // §8.20's report ends with "conforme" — a stated fact.
  await open(page, violating);
  const clean_ = page.locator('[data-testid="mem-clean"]');
  await expect(clean_).toBeVisible();
  await expect(clean_).toContainText("tRAS");
  await expect(clean_).toContainText("conformant");
});

test("the same design compiled clean reports no violations", async ({ page }) => {
  await open(page, clean);
  await expect(page.locator('[data-testid="mem-no-violations"]')).toBeVisible();
  await expect(page.locator('[data-testid="mem-violation-row"]')).toHaveCount(0);
  // And it is not that nothing was analysed.
  await expect(page.locator('[data-testid="mem-count"]')).toContainText("13 commands");
});

test("the bank timeline draws every bank, gap-free, with its open row", async ({ page }) => {
  // Prompt 11's second acceptance criterion.
  await open(page, violating);
  await expect(page.locator('[data-testid="mem-timeline"]')).toBeVisible();

  const spans = await page.$$eval('[data-testid="mem-bank-track"] .mem-seg', (els) =>
    els.map((e) => ({
      bank: Number((e as HTMLElement).dataset.testid?.split("-")[2] ?? -1),
      state: (e as HTMLElement).dataset.state ?? "",
      t0: Number((e as HTMLElement).dataset.t0),
      t1: Number((e as HTMLElement).dataset.t1),
    })),
  );
  expect(spans.length).toBeGreaterThan(0);

  // Every bank's segments tile its row with no gap and no overlap.
  const banks = [...new Set(spans.map((s) => s.bank))];
  expect(banks.length).toBe(4);
  for (const b of banks) {
    const row = spans.filter((s) => s.bank === b).sort((x, y) => x.t0 - y.t0);
    for (let i = 1; i < row.length; i++) {
      expect(row[i].t0, `bank ${b} segment ${i}`).toBe(row[i - 1].t1);
    }
  }

  // An open row is labelled, which is the whole point of the view.
  await expect(page.locator('[data-testid="mem-bank-track"] .mem-seg-row').first()).toBeVisible();
  await expect(page.locator('[data-testid="mem-legend"]')).toContainText("precharging");
});

test("clicking a violation sends Wave to that cycle with the command wires", async ({ page }) => {
  // §8.20: *click pe o violare de timing -> Wave la ciclul respectiv, cu
  // semnalele de comanda deja incarcate.*
  await open(page, violating);
  await page.locator('[data-testid="mem-violation-jump"]').first().click();

  await expect(page.locator('[data-testid="tab-1"]')).toHaveAttribute("aria-selected", "true");
  const rows = await page.$$eval('[data-testid="signal-row"]', (r) =>
    r.map((x) => x.getAttribute("data-path") ?? ""),
  );
  expect(rows.length).toBeGreaterThan(0);
  // The command bus is what a timing violation is about, so those are the
  // wires that get loaded.
  expect(rows.some((p) => p.endsWith("cs_n"))).toBeTruthy();
  expect(rows.some((p) => p.endsWith("ras_n"))).toBeTruthy();
  await expect(page.locator('[data-testid="status-cursor"]')).not.toContainText("—");
});

test("clicking a bank segment jumps to it", async ({ page }) => {
  await open(page, violating);
  // The segments exist as soon as the report arrives, but a click that lands
  // while the track is still being sized hits nothing and the tab never
  // changes — visible only on a loaded machine, where the layout takes longer
  // than Playwright's implicit wait for the element itself. Waiting for a
  // non-zero width narrowed the window without closing it, because the
  // re-render can still move the segment out from under the pointer between
  // the measurement and the click. Retrying the click *with* its effect is the
  // only formulation that cannot race: a lost click is retried, and a landed
  // one passes on the first attempt.
  const segment = page.locator('[data-testid="mem-bank-track"] .mem-seg').first();
  await expect(segment).toBeVisible();
  await expect(async () => {
    await segment.click();
    await expect(page.locator('[data-testid="tab-1"]')).toHaveAttribute(
      "aria-selected",
      "true",
      { timeout: 1000 },
    );
  }).toPass({ timeout: 10_000 });
});

test("the command stream lists every decoded command and filters", async ({ page }) => {
  await open(page, violating);
  await expect(page.locator('[data-testid="mem-cmd"]')).toHaveCount(13);

  await page.locator('[data-testid="mem-filter"]').fill("ACTIVATE");
  await expect(page.locator('[data-testid="mem-cmd"]')).toHaveCount(7);

  // The decode is shown in the device's own vocabulary, not as raw pins.
  await expect(page.locator('[data-testid="mem-cmd"]').first()).toContainText("bank=0");
  await expect(page.locator('[data-testid="mem-cmd"]').first()).toContainText("row=0x1a4");
});

test("row hit / miss / conflict is broken out", async ({ page }) => {
  await open(page, violating);
  const card = page.locator('[data-testid="mem-rowhits"]');
  await expect(card).toContainText("hit");
  await expect(card).toContainText("conflict");
  await expect(page.locator('[data-testid="mem-rowhit-bar"]')).toBeVisible();
  // §8.20 wants the efficiency numbers beside them, as ratios not raw units.
  await expect(page.locator('[data-testid="mem-efficiency"]')).toContainText("bus");
});

test("the address map inspector decomposes an address", async ({ page }) => {
  // §8.20: *introduci o adresa, vezi maparea.*
  await open(page, violating);
  await page.locator('[data-testid="mem-addr-input"]').fill("0x00401004");
  const out = page.locator('[data-testid="mem-addr-out"]');
  await expect(out).toBeVisible();
  // The pack's own field names, the same vocabulary the command stream uses,
  // and the bit range each was taken from — so the answer can be checked
  // against the memory map rather than trusted.
  await expect(out).toContainText("bank");
  await expect(out).toContainText("row");
  await expect(out).toContainText("col");
  await expect(out).toContainText("[11:10]");

  // Nonsense is refused rather than decoded into zeroes.
  await page.locator('[data-testid="mem-addr-input"]').fill("not-an-address");
  await expect(page.locator('[data-testid="mem-addr-error"]')).toBeVisible();
  // The production SDRAM pack maps col from [9:1], not [9:0]: the byte-offset
  // bit is deliberately ignored. At 2**54, Number loses bit 1; BigInt must not.
  await page.getByTestId("mem-addr-input").fill("0x40000000000002");
  await expect(out.locator("tr").filter({ hasText: "col" }).locator("td.num").first()).toHaveText("0x1");
  await page.getByTestId("mem-addr-input").fill("0x1oops");
  await expect(page.getByTestId("mem-addr-error")).toBeVisible();
  await page.getByLabel("Address sequence", { exact: true }).fill("0 4 0x1000 0");
  await expect(page.getByTestId("mem-pattern-result")).toHaveText("1 hits · 1 misses · 2 conflicts");
  await page.getByLabel("Address sequence", { exact: true }).fill("0 0x400 4 0x404");
  await expect(page.getByTestId("mem-pattern-result")).toHaveText("2 hits · 2 misses · 0 conflicts");
});

test("a timing violation shows up in Checks like any other finding", async ({ request }) => {
  // §11.4, checked over the API so the assertion is about the pipeline.
  const checks = await (await request.get(`${BACKEND}/session/${violating}/checks`)).json();
  const memory = checks.findings.filter((f: { group: string }) => f.group === "memory");
  expect(memory.length).toBe(3);
  expect(memory[0].why).toBeTruthy();
});

test("real Icarus refresh intervals reach the compliance chart and Wave", async ({ page, request }) => {
  // The user-facing CLI must create the dump: no handcrafted trace or mocked report.
  const project = mkdtempSync(join(tmpdir(), "veritrace refresh "));
  writeFileSync(join(project, "tb.sv"), `\`timescale 1ns/1ps
module tb;
  reg clk = 0;
  always #5 clk = ~clk;
  reg cs_n = 1, ras_n = 0, cas_n = 0, we_n = 1;
  reg [1:0] ba = 0;
  reg [12:0] a = 0;
  initial begin
    @(negedge clk); cs_n = 0;
    @(negedge clk); cs_n = 1;
    repeat (1600) @(negedge clk);
    cs_n = 0;
    @(negedge clk); cs_n = 1;
    repeat (5) @(posedge clk);
    $finish;
  end
endmodule
`);
  const run = JSON.parse(execFileSync("uv", ["run", "--no-sync", "veritrace", "run", project,
    "--top", "tb", "--json"], { cwd: resolve(".."), encoding: "utf8", timeout: 60_000 }));
  const response = await request.post(`${BACKEND}/session`, { data: { trace_path: run.dump } });
  expect(response.ok(), await response.text()).toBeTruthy();
  const sid = (await response.json()).session_id;
  await waitForReady(request, BACKEND, sid);
  await open(page, sid);
  await expect(page.getByTestId("mem-refresh")).toContainText("15.625us");
  await expect(page.getByTestId("mem-refresh-chart")).toBeVisible();
  // A horizontal SVG line paints a stroke but has a zero-height DOM bbox.
  await expect(page.getByTestId("mem-refresh-limit")).toHaveAttribute("stroke", "currentColor");
  const interval = page.getByTestId("mem-refresh-interval");
  await expect(interval).toHaveCount(1);
  await expect(interval).toHaveAttribute("aria-label", "16.01us, exceeds tREFI");
  await interval.click();
  await expect(page.getByTestId("tab-1")).toHaveAttribute("aria-selected", "true");
  await expect(page.locator('[data-testid="signal-row"][data-path="tb.cs_n"]')).toBeVisible();
  // Layout is the real persisted state, independent of display unit preferences.
  await expect.poll(async () => (await (await request.get(
    `${BACKEND}/session/${sid}/layout`,
  )).json()).cursors[0]).toBe(16025000);
});

test("an unobserved refresh interval is not presented as measured compliance", async ({ page }) => {
  await open(page, violating);
  await expect(page.getByTestId("mem-refresh")).toContainText("compliance was not measured");
  await expect(page.getByTestId("mem-refresh-chart")).toHaveCount(0);
});

test("chip selection and uploaded timings change real findings and survive reload", async ({ page, request }) => {
  const project = mkdtempSync(join(tmpdir(), "veritrace timing "));
  const dump = join(project, "dump.vtx");
  cpSync(resolve("../designs/sdram/dump.vtx"), dump, { recursive: true });
  const response = await request.post(`${BACKEND}/session`, { data: { trace_path: dump } });
  expect(response.ok(), await response.text()).toBeTruthy();
  const sid = (await response.json()).session_id;
  await waitForReady(request, BACKEND, sid);
  await open(page, sid);
  await expect(page.getByTestId("mem-violation-count")).toContainText("3 timing violations");
  await page.getByLabel("Timing chip", { exact: true }).selectOption("__custom__");
  const template = await page.getByLabel("Custom timing TOML").inputValue();
  const custom = template.replace('name = "MT48LC16M16A2"', 'name = "Uploaded test timing"')
    .replace("tRCD = 20", "tRCD = 5");
  expect(custom).not.toEqual(template);
  await page.getByLabel("Custom timing TOML").fill("tRCD = nan");
  await page.getByRole("button", { name: "Apply timing", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("missing timing parameter");
  await expect(page.getByTestId("mem-violation-count")).toContainText("3 timing violations");
  await page.getByLabel("Load timing file").setInputFiles({
    name: "uploaded.toml", mimeType: "text/plain", buffer: Buffer.from(custom),
  });
  await expect(page.getByLabel("Custom timing TOML")).toHaveValue(custom);
  await page.getByRole("button", { name: "Apply timing", exact: true }).click();
  await expect(page.getByTestId("mem-violation-count")).toContainText("2 timing violations");
  await expect(page.getByTestId("mem-violation-tRCD")).toHaveCount(0);
  const checks = await (await request.get(`${BACKEND}/session/${sid}/checks`)).json();
  expect(checks.findings.filter((f: { group: string }) => f.group === "memory")).toHaveLength(2);
  await page.reload();
  await expect(page.getByTestId("mem-violation-count")).toContainText("2 timing violations");
  await expect(page.getByLabel("Custom timing TOML")).toHaveValue(custom);
  await page.getByLabel("Timing chip", { exact: true }).selectOption("mt48lc16m16a2");
  await expect(page.getByTestId("mem-violation-count")).toContainText("3 timing violations");
  await expect(page.getByTestId("mem-violation-tRCD")).toBeVisible();
});
