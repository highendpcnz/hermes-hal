import { expect, test, type Page } from "@playwright/test";

import { logText, opticReady, pinDirection, serveEvents } from "./helpers";

/**
 * The remaining Bridge surfaces: the Initiative's proposal bar, the missions
 * panel, and the viewscreen. All three are SSE-driven, so the tests hand the
 * page the frames the bridge would publish.
 *
 * Lower stakes than the permission bar — none of these gate execution — but
 * two carry real consequences: approving a proposal starts an autonomous
 * agent run, and the viewscreen renders files the *agent* wrote, which is why
 * its sandboxing is asserted here rather than assumed.
 */

const PROPOSAL_ID = "prop-abcdef0123456789";

async function openBridge(page: Page): Promise<void> {
  await pinDirection(page, "aperture");
  await page.goto("/");
  await opticReady(page);
}

const proposal = {
  type: "mission_proposal",
  request_id: PROPOSAL_ID,
  title: "Clear the overdue ledger",
  timeout: 600
};

function mission(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    id: "11111111-2222-3333-4444-555555555555",
    title: "Downloads sweep",
    cookie_id: "browser",
    session_id: "sess-1",
    status: "active",
    prompt: "",
    result: null,
    created_at: Date.now() / 1000 - 30,
    finished_at: null,
    allow_tools: false,
    session_dropped: false,
    dismissed_at: null,
    ...overrides
  };
}

test.describe("proposal bar (the Initiative)", () => {
  test("a proposal raises the bar and names the mission", async ({ page }) => {
    await serveEvents(page, [proposal]);
    await openBridge(page);

    await expect(page.locator("#propbar")).toHaveClass(/show/);
    await expect(page.locator("#propbar-text")).toContainText("Clear the overdue ledger");
    await expect.poll(() => logText(page)).toContain("Hermes Hal proposes");
  });

  test("approving posts approve for that proposal — it starts an agent run", async ({ page }) => {
    await serveEvents(page, [proposal]);
    await openBridge(page);
    await expect(page.locator("#propbar")).toHaveClass(/show/);

    const [request] = await Promise.all([
      page.waitForRequest((r) => r.url().includes("/api/proposal/") && r.method() === "POST"),
      page.click("#prop-approve")
    ]);

    expect(decodeURIComponent(request.url())).toContain(`/api/proposal/${PROPOSAL_ID}`);
    expect(JSON.parse(request.postData() ?? "{}")).toEqual({ decision: "approve" });
    await expect(page.locator("#propbar")).not.toHaveClass(/show/);
  });

  test("declining posts decline, never approve", async ({ page }) => {
    await serveEvents(page, [proposal]);
    await openBridge(page);
    await expect(page.locator("#propbar")).toHaveClass(/show/);

    const [request] = await Promise.all([
      page.waitForRequest((r) => r.url().includes("/api/proposal/") && r.method() === "POST"),
      page.click("#prop-decline")
    ]);

    expect(JSON.parse(request.postData() ?? "{}")).toEqual({ decision: "decline" });
    await expect(page.locator("#propbar")).not.toHaveClass(/show/);
  });

  test("a proposal answered by voice lowers the bar here too", async ({ page }) => {
    await serveEvents(page, [
      proposal,
      {
        type: "mission_proposal_resolved",
        request_id: PROPOSAL_ID,
        title: "Clear the overdue ledger",
        approved: true
      }
    ]);
    await openBridge(page);

    await expect(page.locator("#propbar")).not.toHaveClass(/show/);
    await expect.poll(() => logText(page)).toContain("approved");
  });
});

test.describe("missions panel", () => {
  test("a running mission appears as a card with its elapsed time", async ({ page }) => {
    await serveEvents(page, [{ type: "mission_update", mission: mission() }]);
    await openBridge(page);

    await expect(page.locator("#mission-cards .mission-card")).toHaveCount(1);
    await expect(page.locator("#mission-cards .mission-card-title")).toContainText("Downloads sweep");
    await expect(page.locator("#mission-cards .mission-card-meta")).toContainText("running");
    await expect(page.locator("#mission-cards .mission-card-act")).toHaveText("CANCEL");
    await expect.poll(() => logText(page)).toContain("Downloads sweep");
  });

  test("a finished mission switches its control from CANCEL to DISMISS", async ({ page }) => {
    await serveEvents(page, [
      { type: "mission_update", mission: mission() },
      {
        type: "mission_update",
        mission: mission({
          status: "completed",
          result: "Sorted 42 files into ~/Documents.",
          finished_at: Date.now() / 1000
        })
      }
    ]);
    await openBridge(page);

    await expect(page.locator("#mission-cards .mission-card-meta")).toContainText("completed");
    await expect(page.locator("#mission-cards .mission-card-act")).toHaveText("DISMISS");
  });

  test("CANCEL interrupts the run rather than dropping the card", async ({ page }) => {
    await serveEvents(page, [{ type: "mission_update", mission: mission() }]);
    await openBridge(page);
    await expect(page.locator("#mission-cards .mission-card-act")).toHaveText("CANCEL");

    const [request] = await Promise.all([
      page.waitForRequest((r) => r.url().includes("/api/missions/") && r.method() === "POST"),
      page.click("#mission-cards .mission-card-act")
    ]);
    expect(request.url()).toContain("/cancel");
    expect(request.url()).not.toContain("/dismiss");
  });

  test("clicking a finished card expands its result", async ({ page }) => {
    await serveEvents(page, [
      {
        type: "mission_update",
        mission: mission({
          status: "completed",
          result: "Sorted 42 files into ~/Documents.",
          finished_at: Date.now() / 1000
        })
      }
    ]);
    await openBridge(page);

    await expect(page.locator("#mission-cards .mission-card-result")).toHaveCount(0);
    await page.click("#mission-cards .mission-card-title");
    await expect(page.locator("#mission-cards .mission-card-result")).toContainText("Sorted 42 files");
  });
});

test.describe("viewscreen", () => {
  /** Stand in for the drop-folder listing. */
  async function serveItems(page: Page, items: { name: string; mtime: number }[]): Promise<void> {
    await page.route("**/api/viewscreen", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ items: items.map((i) => ({ ...i, size: 1024 })) })
      });
    });
  }

  test("an announced visual opens the panel and shows the newest first", async ({ page }) => {
    await serveItems(page, [
      { name: "chart.png", mtime: 200 },
      { name: "older.png", mtime: 100 }
    ]);
    await serveEvents(page, [{ type: "viewscreen", name: "chart.png", count: 2 }]);
    await openBridge(page);

    await expect(page.locator("#viewscreen-panel")).toHaveClass(/on/, { timeout: 10_000 });
    await expect(page.locator("#vs-caption")).toContainText("chart.png");
    await expect(page.locator("#vs-caption")).toContainText("1/2");
    await expect(page.locator("#vs-media img")).toHaveCount(1);
  });

  /** The panel renders whenever it has items, but its controls only become
   *  clickable once the viewscreen is the active left-hand surface — which
   *  is what the rail action does. */
  async function focusViewscreen(page: Page): Promise<void> {
    await page.click('[data-bridge-action="viewscreen"]');
    await expect(page.locator("#vs-clear")).toBeVisible();
  }

  test("◀ and ▶ page through the history", async ({ page }) => {
    await serveItems(page, [
      { name: "newest.png", mtime: 300 },
      { name: "middle.png", mtime: 200 },
      { name: "oldest.png", mtime: 100 }
    ]);
    await serveEvents(page, [{ type: "viewscreen", name: "newest.png", count: 3 }]);
    await openBridge(page);
    await expect(page.locator("#vs-caption")).toContainText("newest.png");
    await focusViewscreen(page);

    await page.click("#vs-prev");
    await expect(page.locator("#vs-caption")).toContainText("middle.png");
    await page.click("#vs-next");
    await expect(page.locator("#vs-caption")).toContainText("newest.png");
  });

  test("agent-written HTML renders sandboxed, and PDFs deliberately do not", async ({ page }) => {
    // The viewscreen shows files the *agent* wrote. The iframe sandbox is one
    // of the two controls keeping that content from acting as this origin —
    // the other is the CSP header main.py sets on /viewscreen/*.
    await serveItems(page, [{ name: "page.html", mtime: 100 }]);
    await serveEvents(page, [{ type: "viewscreen", name: "page.html", count: 1 }]);
    await openBridge(page);

    const frame = page.locator("#vs-media iframe");
    await expect(frame).toHaveCount(1);
    await expect(frame).toHaveAttribute("sandbox", "");

    // PDFs are exempt on purpose: the browser's viewer needs its own origin.
    await page.evaluate(() => {
      vsItems = [{ name: "report.pdf", mtime: 100, size: 1 }];
      vsIndex = 0;
      renderViewscreen();
    });
    await expect(page.locator("#vs-media iframe")).toHaveCount(1);
    expect(
      await page.evaluate(
        () => document.querySelector("#vs-media iframe")?.hasAttribute("sandbox") ?? true
      )
    ).toBe(false);
  });

  test("CLEAR empties the folder through the endpoint", async ({ page }) => {
    await serveItems(page, [{ name: "chart.png", mtime: 100 }]);
    await serveEvents(page, [{ type: "viewscreen", name: "chart.png", count: 1 }]);
    await openBridge(page);
    await expect(page.locator("#viewscreen-panel")).toHaveClass(/on/);
    await focusViewscreen(page);

    const [request] = await Promise.all([
      page.waitForRequest(
        (r) => r.url().includes("/api/viewscreen/clear") && r.method() === "POST"
      ),
      page.click("#vs-clear")
    ]);
    expect(request.method()).toBe("POST");
  });
});

// Script-scoped, not window properties — see the note in CLAUDE.md.
declare let vsItems: { name: string; mtime: number; size: number }[];
declare let vsIndex: number;
declare const renderViewscreen: () => void;
