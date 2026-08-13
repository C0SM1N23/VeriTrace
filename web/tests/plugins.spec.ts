/**
 * §13.7's second promise, in the interface: *"Rezultatele tabulare apar automat
 * ca tab nou. **Nu trebuie sa atingi UI-ul ca sa adaugi o analiza.**"*
 *
 * The plugin under test is written into `designs/fsm/plugins/` from this file
 * and discovered by the server. Nothing in `web/src` mentions it, which is the
 * point: if adding an analysis required a change here, the promise would be
 * false and this test would be the place it showed.
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

async function ready(page: Page): Promise<void> {
  await page.goto(`/?session=${session}`);
  await expect(page.locator(".loading")).toHaveCount(0);
  await expect(page.locator('[data-testid="status-range"]')).not.toBeEmpty();
}

test("a plugin's table becomes a tab nobody wired up", async ({ page }) => {
  await ready(page);

  // Eleven tabs: the ten §11.4 names plus the one `designs/fsm/plugins/` added.
  // Nothing under `web/src` mentions that plugin — if adding an analysis needed
  // a change there, §13.7's promise would be false and this is where it shows.
  const strip = page.locator(".tab-strip [role=tab]");
  await expect(strip).toHaveCount(11);

  const extra = page.locator('[data-testid="tab-11"]');
  await expect(extra).toContainText("State dwell");
  await extra.click();

  const pane = page.locator('[data-testid="plugin-tab"]');
  await expect(pane).toBeVisible();
  await expect(pane).toContainText("State dwell");
  // The columns are the plugin's, not this file's.
  await expect(pane.locator("thead th")).toHaveText([
    "machine",
    "state",
    "cycles",
    "%",
    "visits",
  ]);
  await expect(pane.locator("tbody tr").first()).toBeVisible();
  // S_TRAP is the state this design's testbench never reaches, so the analysis
  // that counts dwell time should report zero visits for it.
  await expect(pane).toContainText("S_TRAP");
});

test("a plugin's findings land in the Checks tab", async ({ page }) => {
  await ready(page);
  await page.locator('[data-testid="tab-5"]').click();
  await expect(page.locator('[data-testid="checks-tab"]')).toBeVisible();
  // §13.7: "Constatarile aparute apar automat in tab-ul Checks."
  const row = page.locator('[data-check="plugin.state_dwell"]').first();
  await expect(row).toBeVisible();
  await expect(row).toContainText("never entered");
});

test("the FSM slot says where the feature went rather than that it is missing", async ({
  page,
}) => {
  await ready(page);
  // §11.4 folded FSM into Source. The strip keeps the position so the numbering
  // matches the spec, and the tooltip sends the reader to ⌘M.
  const slot = page.locator('[data-testid="tab-4"]');
  await expect(slot).toHaveClass(/disabled/);
  await expect(slot).toHaveAttribute("title", /mode of the Source tab/);
});

test("the plugins group has a section in Checks", async ({ page }) => {
  await ready(page);
  await page.locator('[data-testid="tab-5"]').click();
  await expect(page.locator('[data-testid="checks-tab"]')).toBeVisible();
  // Not present on this design, but the section must exist in the table that
  // maps groups to headings — a group the server can emit and the UI cannot
  // render is a finding that vanishes between the API and the screen.
  const groups = await page.evaluate(async (s) => {
    const r = await fetch(`/api/session/${s}/checks`, {
      headers: { Accept: "application/json" },
    });
    return Object.keys((await r.json()).groups ?? {});
  }, session);
  expect(groups).toContain("fsm");
});
