# Repository Analysis

*Written July 2026, against the state of `main` at `37250a4` — a follow-up
pass over the one recorded at `0b5fc03` (~2,500 lines and eight features
earlier). A point-in-time review — file names are stable, line numbers are
not.*

## Overview

This repo is a **fully local voice interface that puts Hermes Hal's face and
voice on the Hermes Agent CLI**. A FastAPI server chains three stages —
faster-whisper (STT) → Hermes Agent over the Agent Client Protocol (the
"brain", with real tool access) → Piper TTS with a Hermes Hal voice model —
behind a single-file web UI centered on a CSS-rendered HAL eye.

It began in April 2026 as a cloud-backed MVP (Groq Whisper + Claude API,
forked from the `piclez/hal` Hugging Face Space), was rewired in July 2026 to
run with zero cloud keys, then expanded through the "Discovery One" upgrade
(background missions, a desktop Bridge, full-duplex voice) and a second wave
that gave HAL initiative and a body of "shipboard systems": chess, a Care
Ledger, local voice enrollment (the Crew Manifest), a viewscreen for visual
output, self-proposed missions, and vitals-triggered alerts.

## Architecture

A voice turn flows:

```
browser (eye press / space bar / duplex VAD / typed input)
  └─ WS /ws/conversation preferred | POST /api/talk and /api/say fallback
       ├─ faster-whisper → transcript
       ├─ run_turn_text() — command grammar first (permission/proposal
       │    replies, enrollment, ledger, missions, chess), else:
       │    hermes_bridge.ask_hermes()
       │       · offline preflight (cached TCP probes)
       │       · per-session lock — Hermes sessions are single-writer
       │       · ACP mode: one persistent hermes-acp process, session/prompt
       │         per turn; subprocess mode: one `hermes chat -Q` per turn
       │       · tool-permission requests: deny / ask-and-wait / auto-allow
       │         (yolo, or a trigger's "permissions": "allow")
       ├─ speakable() — strip markdown/tables/emoji for TTS
       └─ Piper → WAV, or sentence-by-sentence PCM streaming
```

| Component | Role |
|---|---|
| `main.py` | FastAPI app: HTTP + SSE + WebSocket endpoints, STT/TTS pipeline, per-cookie history, command-grammar dispatch (`run_turn_text`), boot ritual, latency telemetry |
| `hermes_bridge.py` | Brain bridge (ACP default, subprocess fallback), cookie→Hermes session map, SSE event fan-out with mission aliasing, commentary sinks, per-mission tool-auto-allow |
| `mission_control.py` | Background missions (own Hermes session each, steerable after completion), the trigger scheduler (interval/watch/daily/vitals), persisted trigger state |
| `ledger.py` | The Care Ledger — promises/deadlines/open loops in `data/ledger.json`, written by voice commands, the nightly sweep mission, and read by the morning briefing |
| `speaker_id.py` | The Crew Manifest — local voiceprint enrollment/identification (sherpa-onnx + CAM++), commander election for spoken permission approval |
| `chess_control.py` / `chess_engine.py` | A clean-room chess engine (no GPL deps) plus spoken/typed move parsing, narration, and per-session persistence |
| `static/index.html` | Entire frontend — vanilla JS, no build step; three responsive layouts (mobile eye, tablet, desktop Bridge) plus mission/chess/viewscreen/ledger panels |
| `tests/run.py` | Zero-dependency suite (720+ lines); `HAL_SKIP_MODELS=1` keeps it model-free and fast |
| `reflection/` | Documented experiment: LLM drafts skills from agent transcripts, promotion gated behind human confirmation |
| `AGENTS.md` | The HAL persona — proposing missions, the viewscreen convention, voice-identity tags — auto-injected because the agent runs with this directory as cwd |
| `Dockerfile`, `hal_prompt.py`, `download_model.py`, `.env.example` | Vestigial (pre-rewiring); the Dockerfile does not build a working image (documented in-file and in the README) |

Design decisions worth knowing before touching the code:

- **Sessions are three-layered**: browser cookie (`hal_session`) → Hermes/ACP
  session id (persisted map in `data/hermes_sessions.json`) → Hermes' own
  state in `~/.hermes/state.db`. Missions get a fourth, disposable layer: a
  synthetic UUID session that survives `STEERABLE_TTL` after completion for
  follow-up questions, then is reaped.
- **Concurrency is deliberate and keyed**: `hermes_bridge.KeyedLocks`
  refcounts holders *and* waiters (fixing an earlier eviction race) and is
  reused for history, chess, and WebSocket-speech locks — one correct
  primitive, several call sites.
- **Ownership is identity-checked, not just presence-checked.** Two
  independent bugs this cycle were the same shape: a stale turn's cleanup
  evicting state a newer turn had already replaced (`active_websockets`,
  fixed earlier; `_commentary_sinks`, fixed in `cc14cbf`). Both fixes pass
  the caller's own object and only clear if it still owns the slot — a
  pattern worth following for any future single-slot-per-session state.
- **Deny-by-default permissions, with two escape hatches, both scoped.**
  `HAL_PERMISSION_MODE=ask` routes a spoken/on-screen yes-or-no through a
  future; the commander-voice rule (Crew Manifest) restricts *spoken*
  approval once enrollment exists, while typed/on-screen approval stays
  open (a keyboard already implies physical presence). Trigger-level
  `"permissions": "allow"` auto-allows tool calls, but only for that
  mission's own synthetic session key (`_tool_allowed_cookies`) — verified
  by tracing `allow_tools_for(mission.session_id)` through to the ACP
  `request_permission` callback's `cookie_id` lookup; it cannot leak
  auto-allow to a browser session or another mission.
- **Failure paths speak in character** — timeout, offline, and error cases
  all produce spoken HAL lines instead of silence; mission cancellation
  during an in-flight turn is a first-class outcome, not an exception path
  (`Mission.status` flips to `"cancelled"` and both the success and
  exception branches of `run_mission` check it before overwriting).
- **Every new persisted file follows the same safe-write shape**: load
  defensively (`except (OSError, json.JSONDecodeError): return default`),
  mutate in memory, write to a `.tmp` sibling, `rename`/`replace` onto the
  real path. `ledger.json`, `speakers.json`, `trigger_state.json`, and each
  `data/chess/<session>.json` all do this — no partial-write corruption on a
  crash mid-save.

## Strengths

- Comments explain *constraints*, not mechanics (why the TTS lock exists,
  why a mission must be created before its proposal is forgotten, why
  `clear_commentary_sink` takes the caller's own sink object).
- Docs match the code. The README's env table, endpoint list, and Missions/
  Ledger/Crew Manifest/Viewscreen/Chess sections are accurate against the
  current `main.py`, `mission_control.py`, `ledger.py`, `speaker_id.py`, and
  `chess_control.py` — rare at any repo size, rarer after eight features
  landed in three days.
- The test suite grew with the features rather than lagging them: chess is
  pinned by perft counts (startpos, Kiwipete for castling, position 3 for en
  passant) plus mate/stalemate/threefold/fifty-move and voice-parsing cases;
  the Crew Manifest tests cover enrollment, threshold rejection, commander
  succession, and that only the commander's *voice* (not typed input) can
  approve; the mission-cap race for proposal approval added in the latest
  commit is regression-tested, not just fixed.
- The two lock-eviction bugs fixed this cycle were self-found and
  self-fixed in small, well-explained commits with accompanying tests — a
  good sign for a codebase's ongoing health, distinct from whether it has
  bugs at all.
- `ruff check .` and the full `tests/run.py` suite both pass clean on
  `HEAD` as of this review (69 test groups, zero failures).
- The reflection loop still treats LLM output as untrusted input
  (slug-validated skill names because the name becomes a directory) and is
  honest that its "sandbox review" is advisory, with a human gate as the
  real control.

## This pass: what was checked, and what I found

Scope: everything added since the last analysis (`0b5fc03`) —
mission proposals/"Initiative" (`c5284d8`, `37250a4`), the Care Ledger
(`71c7687`), the Crew Manifest (`53c9ffc`), the viewscreen (`e23bf31`),
chess (`9e5ec04`, `ad390d7`), speak-while-thinking commentary (`90832a4`,
`cc14cbf`), steerable missions with cancel/dismiss (`456055d`), daily
briefing/vitals triggers (`f38dde8`, `e90306e`), and the boot ritual /
latency sparkline (`8eaf95e`, `eb8003c`) — plus a re-check of every item
still open in the previous analysis.

**No new correctness or security bugs found.** Specific things I chased
that turned out fine:

- *Could the ledger's read-modify-write race the way session history once
  did?* No — `Ledger.add/complete/forget` are fully synchronous (no
  `await` between load and save), so within the single-threaded event loop
  they can't interleave with each other the way an `await ask_hermes(...)`
  held open the old history race. The module's own docstring already flags
  the real, unavoidable multi-writer case (the nightly sweep mission edits
  `ledger.json` with generic file tools, not through this class) as an
  accepted design tradeoff — there's no lock that can cover a shell-level
  file write from inside an agent's tool call anyway.
- *Could a trigger's `"permissions": "allow"` leak beyond its own mission?*
  No — traced end to end (see "Design decisions" above); the auto-allow set
  is keyed on the mission's own disposable session id.
- *Does `_resolve_proposal` still lose a proposal at the mission cap?* No —
  fixed in the commit at `HEAD` (`37250a4`) and regression-tested.
- Chess move parsing, session-id-as-filename validation
  (`_SESSION_ID_RE`), and checkmate detection (`san()` appending `#`,
  consumed by `chess_control._finish_if_over` via a string check rather
  than recomputing) all check out against the perft/mate test cases.

## Previously open items — now resolved

- **Trigger state was in-memory** (a restart re-armed intervals and
  re-baselined watchers). `e90306e`/`f38dde8` added
  `data/trigger_state.json` with load-on-start, save-on-change, and a
  catch-up fire for `"at"` triggers that were due while the server was
  down — the daily-briefing trigger now behaves as the README describes.

## Still open (honest list)

- **Interim-transcript re-transcription is still quadratic.** Each ~3s
  caption tick during a duplex utterance re-runs whisper over the *entire*
  buffered audio (`b"".join(audio_chunks)`), not just the new tail — fine
  for a normal spoken turn, wasteful for long dictation. Flagged as
  acceptable scope in the previous analysis; still true, still acceptable
  at current usage, worth a queue if duplex dictation of paragraphs becomes
  a real use case.
- **Voice yes/no for permissions and proposals arrives over whichever
  transport is free**; in duplex mode the mic only reopens once HAL
  finishes asking, so answering over him still needs barge-in or the
  on-screen buttons.
- **`vitals` battery checks are macOS-only** (`pmset -g batt`); on Linux or
  Windows `battery_below` silently never fires (`_battery_percent` returns
  `None` and the breach check short-circuits). This matches "None on
  desktops or parse failure" in the code comment, but it's worth knowing
  before writing a trigger that depends on it cross-platform — `disk_free_gb_below`
  is platform-independent (`shutil.disk_usage`) and unaffected.
- **Reflection loop retargeting is still open.** Failed missions journal to
  `data/missions/failed.jsonl`, ready for `reflection_loop.py --transcript`,
  but pointing the loop's default transcript source at Hermes' own store
  remains open — it needs knowledge of Hermes' transcript format that this
  repo doesn't have.
- **The Care Ledger is one shared file for the whole household**, not
  per-session like chess or history — correct for its purpose (Dave has one
  ledger regardless of which device he's talking from), but worth
  remembering if a future feature assumes ledger state is scoped to a
  browser session the way missions and chess games are.
