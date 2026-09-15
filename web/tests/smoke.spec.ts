/**
 * The app mounts, and mounts quietly.
 *
 * This exists because of a bug that broke every other spec at once and none of
 * them said why. A store selector written `useWave((s) => s.checks?.tables ?? [])`
 * builds a **new array on every render**, so zustand's snapshot never compares
 * equal, React re-renders, and the component loops until it throws "Maximum
 * update depth exceeded". The application never paints. Seventy tests then fail
 * on their own first assertion — `status-range` not found, `tab-3` not found —
 * and every one of those messages points somewhere other than the cause.
 *
 * So: one test that opens the page and reads the console. It is the cheapest
 * test in the suite and the only one that would have named that bug.
 */

import { expect, test } from "@playwright/test";
import { waitForReady } from "./session";
import { execFileSync } from "node:child_process";
import { copyFileSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const BACKEND = process.env.VERITRACE_BACKEND ?? "http://127.0.0.1:8765";

test("a newly opened simulation retains transient build defines in causal analysis", async ({ page, request }) => {
  const project = mkdtempSync(join(tmpdir(), "veritrace build flags "));
  writeFileSync(join(project, ".veritrace.toml"), '[design]\ntop="tb"\nrtl=["tb.sv"]\n[design.defines]\nWIDTH=4\n');
  writeFileSync(join(project, "tb.sv"), `\`timescale 1ns/1ps
module tb;
  reg clk=0;
  always #5 clk=~clk;
  reg [\`WIDTH-1:0] data=0;
  initial begin #7 data=42; #20 $finish; end
endmodule
`);
  const output = execFileSync("uv", ["run", "--no-sync", "veritrace", "run", project, "-D", "WIDTH=8", "--json"],
    { cwd: resolve(".."), encoding: "utf8", timeout: 60_000 });
  const opened = await request.post(`${BACKEND}/session`, { data: { trace_path: JSON.parse(output).dump } });
  expect(opened.ok(), await opened.text()).toBeTruthy();
  const sid = (await opened.json()).session_id;
  await waitForReady(request, BACKEND, sid);
  const why = await request.post(`${BACKEND}/session/${sid}/query`, { data: { vtq: "why(tb.data @ 8000)" } });
  expect(why.ok(), await why.text()).toBeTruthy();
  expect((await why.json()).root).toMatchObject({ signal: "tb.data", value: "00101010", width: 8 });
  await page.goto(`/?session=${sid}`);
  await expect(page.getByTestId("status-range")).not.toBeEmpty();
  await page.getByTestId("query-bar").fill("why(tb.data @ 8000)");
  await page.getByTestId("query-bar").press("Enter");
  await expect(page.getByTestId("causal-card").first().locator(".card-val")).toHaveText("= 00101010");
});

test("opening progress waits before fetching the real waveform", async ({ page, request }) => {
  const root = await (await request.get(`${BACKEND}/`)).json();
  const sid = root.default_session;
  await waitForReady(request, BACKEND, sid);
  let release = false;
  let prematureReads = 0;
  let layoutRequested = false;
  let releaseLayout!: () => void;
  const layoutGate = new Promise<void>((resolve) => { releaseLayout = resolve; });
  page.on("request", (req) => {
    if (!release && /\/session\/[^/]+\/(signals|layout)/.test(req.url())) prematureReads++;
  });
  // Only delay readiness; all waveform, hierarchy, and layout responses still
  // come from the real server and native store once the gate opens.
  await page.route(`**/session/${sid}/status`, async (route) => {
    if (release) await route.continue();
    else await route.fulfill({ json: { phase: "indexing", progress: 0.42 } });
  });
  await page.route(`**/session/${sid}/layout`, async (route) => {
    layoutRequested = true;
    await layoutGate;
    await route.continue();
  });
  await page.goto(`/?session=${sid}`);
  await expect(page.locator(".loading")).toContainText("indexing: 42%");
  expect(prematureReads).toBe(0);
  // Bootstrap restores the query, tab, and layout. Do not offer commands that
  // it would silently discard, including shortcuts with no input focused.
  await expect(page.getByTestId("query-bar")).toHaveCount(0);
  await page.keyboard.press("5");
  await expect(page.getByTestId("tab-5")).toHaveCount(0);
  release = true;
  await expect.poll(() => layoutRequested).toBe(true);
  await expect(page.locator(".loading")).toBeVisible();
  await expect(page.getByTestId("query-bar")).toHaveCount(0);
  releaseLayout();
  await expect(page.locator(".loading")).toHaveCount(0);
  await expect(page.getByTestId("query-bar")).toBeEditable();
  await expect(page.getByTestId("tab-5")).not.toHaveAttribute("aria-selected", "true");
  await expect(page.locator('[data-testid="signal-row"]').first()).toBeVisible();
  await expect(page.locator('[data-testid="status-range"]')).not.toBeEmpty();
});

test("opening failure displays the backend's actual error", async ({ page, request }) => {
  const root = await (await request.get(`${BACKEND}/`)).json();
  const sid = root.default_session;
  await page.route(`**/session/${sid}/status`, (route) => route.fulfill({
    json: { phase: "error", progress: 0.2, error: "Malformed waveform: missing enddefinitions" },
  }));
  await page.goto(`/?session=${sid}`);
  await expect(page.locator(".fatal-body")).toHaveText("Malformed waveform: missing enddefinitions");
  await expect(page.locator(".loading")).toHaveCount(0);
});

test("the application mounts with no console errors", async ({ page, request }) => {
  const r = await request.post(`${BACKEND}/session`, {
    data: { trace_path: "designs/fsm/dump.vcd.vtx", rtl_paths: ["designs/fsm"] },
  });
  expect(r.ok(), await r.text()).toBeTruthy();
  const session = (await r.json()).session_id as string;

  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });

  await page.goto(`/?session=${session}`);
  await expect(page.locator(".loading")).toHaveCount(0);
  await expect(page.locator('[data-testid="status-range"]')).not.toBeEmpty();
  await expect(page.locator(".tab-strip [role=tab]").first()).toBeVisible();

  // React's dev build reports a bad selector as a warning *before* it throws,
  // so the useful signal is there even when the loop has not tripped yet.
  const noisy = errors.filter(
    (e) => !e.includes("Download the React DevTools") && !e.includes("favicon"),
  );
  expect(noisy, noisy.join("\n")).toEqual([]);
});

test("a dropped waveform response reconnects and loads real backend data", async ({ page, request }) => {
  const project = mkdtempSync(join(tmpdir(), "veritrace reconnect "));
  const trace = join(project, "dump.vcd");
  copyFileSync(resolve("../designs/fifo_buggy/dump.vcd"), trace);
  const opened = await request.post(`${BACKEND}/session`, { data: { trace_path: trace } });
  expect(opened.ok(), await opened.text()).toBeTruthy();
  const sid = (await opened.json()).session_id;
  await waitForReady(request, BACKEND, sid);
  let interrupted = false;
  let delivered = false;
  let resumed = false;
  await page.routeWebSocket(`**/session/${sid}/ws`, (socket) => {
    if (interrupted && !resumed) {
      socket.close({ code: 1011, reason: "outage still active" });
      return;
    }
    const server = socket.connectToServer();
    server.onMessage((message) => {
      // Interrupt a real response, not a fixture pretending to be a waveform.
      if (!interrupted) {
        interrupted = true;
        socket.close({ code: 1011, reason: "integration test disconnect" });
      } else {
        delivered = true;
        socket.send(message);
      }
    });
  });
  await page.goto(`/?session=${sid}`);
  await page.getByTestId("tab-1").click();
  await expect(page.getByTestId("wave-connection-error")).toContainText("Reconnecting");
  resumed = true;
  await expect.poll(() => delivered).toBe(true);
  await expect(page.getByTestId("wave-connection-error")).toHaveCount(0);
  await expect.poll(() => page.evaluate(() =>
    (window as unknown as { __vtDataReady?: boolean }).__vtDataReady)).toBe(true);
});
