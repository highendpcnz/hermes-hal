import { expect, test, type Page } from "@playwright/test";

import { opticReady, pinDirection } from "./helpers";

/**
 * The chess board.
 *
 * Unlike the other specs this runs against the *real* server end to end —
 * chess_engine.py is pure Python with no model behind it, so
 * HAL_SKIP_MODELS=1 leaves the whole game working. Every move here is
 * generated, validated and answered by the actual engine.
 *
 * The board is the one surface where a client bug silently corrupts server
 * state: it posts UCI strings assembled from two clicks, and the selection
 * logic decides which. tests/run.py pins the engine's perft counts but has
 * never seen a square clicked.
 *
 * NOTE ON SCOPE: index.html's behaviour layer is a classic <script>, so its
 * top-level `let`/`const` live in the script's global lexical environment and
 * are NOT properties of `window`. `window.chessGame` is undefined; the bare
 * identifier resolves. Everything below reads state through bare identifiers
 * for that reason.
 */

declare const chessGame: {
  dave_color: string;
  moves: string[];
  status: string;
  legal: string[];
} | null;
declare const loadChess: () => void;

const PANEL = "#chess-panel";
const SQUARES = "#chess-board .chess-sq";

async function openBoard(page: Page, color: "white" | "black" = "white"): Promise<void> {
  await pinDirection(page, "aperture");
  await page.goto("/");
  await opticReady(page);
  // Straight to the endpoint rather than a typed /chess turn: a WS turn would
  // need TTS, which HAL_SKIP_MODELS deliberately removes.
  await page.evaluate(async (c) => {
    await fetch("/api/chess/new", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ color: c })
    });
    loadChess();
  }, color);
  await expect(page.locator(PANEL)).toHaveClass(/on/, { timeout: 10_000 });
  await expect(page.locator(SQUARES)).toHaveCount(64);
}

/** Read the board back as a name → glyph map, straight from the DOM. */
async function boardMap(page: Page): Promise<Record<string, string>> {
  return page.evaluate(() => {
    const flipped = chessGame?.dave_color === "b";
    const out: Record<string, string> = {};
    Array.from(document.querySelectorAll("#chess-board .chess-sq")).forEach((el, index) => {
      const row = Math.floor(index / 8);
      const col = index % 8;
      const file = flipped ? 7 - col : col;
      const rank = flipped ? row + 1 : 8 - row;
      const glyph = el.querySelector("span")?.textContent ?? "";
      if (glyph) out["abcdefgh"[file]! + rank] = glyph;
    });
    return out;
  });
}

async function clickSquare(page: Page, name: string): Promise<void> {
  await page.evaluate((target) => {
    const flipped = chessGame?.dave_color === "b";
    const squares = Array.from(document.querySelectorAll("#chess-board .chess-sq"));
    for (let index = 0; index < squares.length; index += 1) {
      const row = Math.floor(index / 8);
      const col = index % 8;
      const file = flipped ? 7 - col : col;
      const rank = flipped ? row + 1 : 8 - row;
      if ("abcdefgh"[file]! + rank === target) {
        (squares[index] as HTMLElement).click();
        return;
      }
    }
    throw new Error(`no square ${target}`);
  }, name);
}

const movesPlayed = (page: Page): Promise<number> =>
  page.evaluate(() => chessGame?.moves?.length ?? 0);

test.describe("chess board", () => {
  test("a new game renders 64 squares in the starting position", async ({ page }) => {
    await openBoard(page);
    const board = await boardMap(page);

    expect(Object.keys(board)).toHaveLength(32);
    expect(board.e1).toBe("♔");
    expect(board.d8).toBe("♛");
    expect(board.a2).toBe("♙");
    expect(board.h7).toBe("♟");
    await expect(page.locator("#chess-status")).toContainText("Your move");
  });

  test("selecting a piece reveals exactly its legal destinations", async ({ page }) => {
    await openBoard(page);
    await clickSquare(page, "e2");

    // A starting-position pawn has exactly two: one step and two.
    await expect(page.locator(`${SQUARES}.tgt`)).toHaveCount(2);
    await expect(page.locator(`${SQUARES}.sel`)).toHaveCount(1);
  });

  test("two clicks play a move and the real engine answers", async ({ page }) => {
    await openBoard(page);
    await clickSquare(page, "e2");
    await clickSquare(page, "e4");

    await expect.poll(async () => (await boardMap(page)).e4, { timeout: 25_000 }).toBe("♙");
    expect((await boardMap(page)).e2).toBeUndefined();

    // The engine replied, so two plies are on the board and it is Dave again.
    await expect.poll(() => movesPlayed(page), { timeout: 25_000 }).toBe(2);
    await expect(page.locator("#chess-status")).toContainText("Your move");
    // HAL's reply is highlighted from- and to-square.
    await expect(page.locator(`${SQUARES}.last`)).toHaveCount(2);
  });

  test("an illegal destination deselects instead of posting a bad move", async ({ page }) => {
    await openBoard(page);
    let posted = 0;
    await page.route("**/api/chess/move", async (route) => {
      posted += 1;
      await route.continue();
    });

    await clickSquare(page, "e2");
    await clickSquare(page, "e5"); // three squares — not legal from the start
    await page.waitForTimeout(600);

    expect(posted, "the client must not post a move the engine would reject").toBe(0);
    expect((await boardMap(page)).e2).toBe("♙");
  });

  test("playing black flips the board and HAL opens", async ({ page }) => {
    await openBoard(page, "black");

    // Orientation is a render concern; the mapping must stay truthful.
    const board = await boardMap(page);
    expect(board.e1).toBe("♔");
    expect(board.e8).toBe("♚");

    // Dave on black means HAL moves first.
    await expect.poll(() => movesPlayed(page), { timeout: 25_000 }).toBeGreaterThanOrEqual(1);
  });

  test("resigning ends the game and the board stops accepting moves", async ({ page }) => {
    await openBoard(page);
    await page.click("#chess-resign");

    await expect(page.locator("#chess-status")).toContainText("Hermes Hal wins", { timeout: 10_000 });
    expect(await page.evaluate(() => chessGame?.status)).not.toBe("active");

    // Two independent guards stop play here: the client's status check, and
    // the server publishing no legal moves for a finished game. The second is
    // the load-bearing one — removing the client check alone changes nothing,
    // because an empty `legal` means no selection can ever form.
    expect(await page.evaluate(() => chessGame?.legal?.length ?? -1)).toBe(0);

    let posted = 0;
    await page.route("**/api/chess/move", async (route) => {
      posted += 1;
      await route.continue();
    });
    await clickSquare(page, "d2");
    await clickSquare(page, "d4");
    await page.waitForTimeout(600);

    expect(posted, "a finished game must ignore clicks").toBe(0);
  });
});
