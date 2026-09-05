# Hermes Hal frontend — Claude Code handoff

Read `AGENTS.md` before changing anything. It is the authoritative Hermes Hal persona and
runtime guidance; preserve it. Replies from the running bridge are spoken aloud, so
keep user-facing prose concise and address the user as Dave.

## Workspace

The repo root is whichever directory contains `run.sh` — **do not hardcode a
checkout path here**; the previous revision of this file named a machine that no
longer exists and misled every agent that read it.

This is a local Hermes Hal voice frontend for Hermes Agent. The Python bridge is the
source of truth for sessions, missions, permissions, chess, telemetry, viewscreen
events, STT, and TTS. The browser surface is `static/index.html`; direction
stylesheets are `static/bridge-option{1..4}.css` over `static/bridge-shared.css`; the
procedural Three.js optic is authored in `frontend/*.ts` and emitted to
`static/assets/` by Vite.

## Run and verify

The app does **not** run inside the Hermes venv on every machine — that venv is
missing `faster_whisper`/`piper` on some installs. `run.sh` prefers a repo-local
`.venv` when one exists and resolves `hermes`/`hermes-acp` separately, so no
environment variables are needed:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt agent-client-protocol sherpa-onnx
python3 download_model.py     # models/hal.onnx (61 MB, gitignored) — run.sh falls back to it
./run.sh
```

`hermes-acp` is spawned as a subprocess, so only that binary needs to exist outside
this venv; the ACP *client library* must be installed inside it. On Linux
`espeak-ng` is **not** a required system package — the `piper-tts` wheel bundles it.
`HAL_HERMES_VENV` / `HAL_HERMES_BIN` / `HAL_HERMES_ACP_BIN` still override detection
if you need to force an environment.

**The venv is not relocatable** — `bin/*` shebangs are absolute. If the checkout
moves, delete `.venv` and rebuild rather than copying it.

Then:

```sh
curl -fsS http://127.0.0.1:8000/api/health     # want status: operational, bridge alive
.venv/bin/python tests/run.py                  # zero-dep, seconds, "all tests passed"
.venv/bin/ruff check .
npm run check                                  # tsc --noEmit && vite build
npm run test:e2e                               # Playwright, boots its own server
```

`test:e2e` covers the behaviour layer the Python suite can only reach with
substring assertions. 45 tests across six specs: the session-reset path
(`reset.spec.ts`), push-to-talk and duplex WebSocket transport
(`websocket.spec.ts`), the Allow/Deny bar (`permission.spec.ts`),
full-duplex/VAD mode
(`duplex.spec.ts`), the chess board (`chess.spec.ts`, which runs against the
*real* engine since it needs no model), and the proposal bar, missions panel
and viewscreen (`surfaces.spec.ts`). It starts its own app instance on port 8123 with
`HAL_SKIP_MODELS=1` and `HAL_DATA_DIR=.playwright-data`, so it never loads a
model, never runs inference, and never writes to the real `data/`. Anything
needing a live turn belongs in `.claude/skills/run-hal/smoke.sh` instead.

**The behaviour layer's state is script-scoped, not on `window`.**
`static/index.html`'s inline JS is a classic `<script>`, so its top-level
`let`/`const` (`chessGame`, `isWsRecording`, `busy`, `loadChess`, `setState`, …)
live in the script's global *lexical* environment. `window.chessGame` is
`undefined`; the bare identifier resolves. Inside `page.evaluate` use bare
identifiers and `declare` them in the spec for TypeScript. Reaching for
`window.*` fails silently as `undefined`, which reads like a broken app rather
than a broken test.

Three mocking techniques make the unreachable paths reachable, and all are
load-bearing:

- **`--use-fake-device-for-media-stream`** plus `permissions: ["microphone"]`
  makes `getUserMedia` resolve, so full-duplex can actually be switched on
  instead of falling into its access-denied branch. The fake device emits a
  steady tone rather than speech, so don't wait on the VAD tripping by
  itself — drive `isWsRecording` directly for the capture-in-flight branches.

- **`page.routeWebSocket`** lets a test *be* the bridge, so any frame the
  server can emit is reproducible without models or audio. Frames like
  `tts_done` racing `turn_done` mid-commentary have no other trigger.
- **Routing `/api/events`** (`serveEvents` in `helpers.ts`) hands the page
  synthetic SSE. Note that fulfilling a route closes the stream, which trips
  the page's 5s reconnect — serve the frames once and keepalive after, or a
  replayed body re-raises a prompt the user already answered.

Two selector traps in this UI: `.mission-card-act` is shared by the chess and
viewscreen controls, so scope it (`#mission-cards .mission-card-act`); and the
left-hand panels render whenever they have content but only become *clickable*
once the rail action sets `data-active-surface`, so click
`[data-bridge-action="…"]` before driving their buttons.

The suite runs single-worker (~6m). Each test gets its own browser context and
therefore its own `hal_session` cookie, so parallelising is likely safe — but
it hasn't been tried, and chess and reset both mutate per-session server state.

`vite build` output is committed under `static/assets/` and is currently
byte-identical to a fresh build — keep it that way; a drifting bundle is a silent
bug. Keep the bridge on loopback unless the host allowlist and an authentication
story are deliberately addressed.

## Verifying visual work

`google-chrome --headless --screenshot` **hangs and never writes the file** — the
page holds an SSE stream and a WebSocket open, so it never reaches network-idle. No
flag combination fixes this. Use the claude-in-chrome MCP; WebGL renders correctly
there. Screenshot-based design QA is mandatory for shell/optic changes: the mission
log bug below was invisible in source and obvious on sight.

## Current product state

Four visual directions are implemented and selectable from the rail: 01 "Aperture
Sentinel" (default), 02 "Cognitive Orrery", 03 "Signal Vault"
(`docs/plans/directions-2-3.md`), 04 "Ember Chorus"
(`docs/plans/2026-07-19-ember-chorus-design.md`). QA records: `design-qa*.md`.
Direction-independent base/mobile styles live in `static/bridge-shared.css`; each
direction owns its desktop/tablet shell stylesheet.

Selection persists in `localStorage` (`hal_direction`); a pre-paint inline script in
`static/index.html` picks the stylesheet and `frontend/hal-optic.ts` dynamically
imports the scene module. The scene contract is `frontend/optic-api.ts`;
`tests/run.py` pins the cross-file invariants.

## Verifying CSS changes — read this before believing a screenshot

The browser aggressively caches the direction stylesheets, and `StaticFiles`
serves them with revalidation the browser may skip. **A reload is not enough**: a
correct fix will look broken. Cache-bust the sheet and measure geometry rather
than trusting your eyes:

```js
const link = document.querySelector('link[data-direction-style="vault"]');
link.href = link.getAttribute('href').split('?')[0] + '?cachebust=' + Date.now();
// then read getBoundingClientRect() on the elements you changed
```

This cost a wrong conclusion once — a working flex fix appeared to have no effect
for two screenshots.

## Known defects

- **Direction 03 "Signal Vault"**: fixed — the log header was a `min-height: 0`
  column-flex item inside a `max-height` container, so it compressed below its own
  two-row content and painted over the first entry. Kept here as the worked example
  of the cache trap above.
- `docs/ANALYSIS.md` is a point-in-time review against `37250a4` (2026-07-24),
  recovered from the pre-directions line. It covers the ledger, voiceprints,
  chess, the viewscreen and the Initiative, but predates the five visual
  directions and the cross-origin work entirely — it describes neither. Treat
  the README as authoritative where they disagree, and don't mistake its
  "still open" list for the current one.

## Known risks

`static/index.html` is ~3,000 lines: ~1,100 of legacy inline CSS (which still owns
the tablet and desktop media blocks every direction stylesheet must reset against)
and ~1,600 of untyped inline JS carrying the whole behavior layer — WS protocol, PCM
playback, VAD, permission bar, chess, missions — pinned only by substring assertions
in `tests/run.py`. Treat edits there as unpinned until you have looked at the result.

The frontend bundle is intentionally Three.js-heavy; check mobile GPU and
reduced-motion behavior when adding effects. The bridge has no authentication and
defaults to denying tool permissions. Treat `data/` as runtime state, and inspect
`git status` before changing or deleting generated artifacts. Do not add cloud API
keys or commit secrets.
