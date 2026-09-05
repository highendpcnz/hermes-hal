# Hermes Hal — voice frontend for Hermes Agent

`./run.sh` now opens a lightweight voice page by default: text entry, recorded
messages, reply playback, connection state and tool approvals. It loads no Three.js,
visual effects, remote fonts or animation loop. `/lite` always opens this page;
`/bridge` opens the existing graphical interface described below. Set `HAL_UI=bridge`
to restore the graphical launcher default. The light page records up to 60 seconds
per message and needs localhost or HTTPS for microphone access. Stop playback only
silences browser audio; it does not cancel an agent or stop motors.

The Python voice stack is still required; Android deployment and Luna subscription
inference have not been verified by this UI change. Technology may be copied from
the separate `hal` repository into this project, never in reverse, with no runtime
coupling. See [the transfer policy and ledger](docs/technology-transfer.md).

Push-to-talk web interface that makes Hermes Hal the face (and voice) of
[Hermes Agent CLI](https://github.com/NousResearch/Hermes-Agent). Hold the
red eye, speak, release — Hermes does the thinking (with full tool access),
and the reply comes back in Hermes Hal's voice.

On screens wider than 760px the eye sits in a **Bridge** layout — mission log,
telemetry bar, live waveform, mission cards, always-visible input — with an
optional full-duplex mode (always-on mic with voice detection, barge-in, live
interim captions, and an optional "HAL, …" wake-word gate with audio pre-roll
so the opening address is not clipped) and background
**missions** HAL reports on when they finish. With
`HAL_PERMISSION_MODE=ask`, HAL asks out loud before running a tool and waits
for your yes/no (or an on-screen Allow/Deny).

Forked from [piclez/hal](https://huggingface.co/spaces/piclez/hal) and rewired
to run fully local with zero cloud API keys:

| Stage | Original (HF Space)     | This fork                                          |
|-------|-------------------------|----------------------------------------------------|
| STT   | Groq Whisper API        | faster-whisper, local (`base.en` by default)       |
| Brain | Claude API              | **Hermes Agent CLI** — named sessions, tools, skills |
| TTS   | Piper (plain)           | [campwill/HAL-9000-Piper-TTS](https://huggingface.co/campwill/HAL-9000-Piper-TTS) with Hermes Hal text normalization and optional ffmpeg mastering |

## How it works

```
browser (hold-to-talk)
  └─ WS /ws/conversation (POST /api/talk fallback; webm/mp4 audio)
       ├─ faster-whisper  → transcript
       ├─ persistent hermes-acp process (Agent Client Protocol over stdio)
       │    · session/prompt per turn — no CLI startup cost (~4s/turn all-in)
       │    · cookie session ↔ ACP session map: data/hermes_sessions.json
       │    · ACP sessions persist in ~/.hermes/state.db → survive restarts
       │      of both the bridge and the agent (session/load)
       │    · persona injected via AGENTS.md in this directory (cwd rules)
       │    · agent stderr → data/acp.log
       └─ piper (hal9000.onnx) → WAV reply
```

The bridge speaks ACP through the official `agent-client-protocol` library
(included in `requirements.txt`, and in the Hermes venv through the
`hermes-agent[acp]` extra). Set
`HAL_BRIDGE=subprocess` to fall back to the original one-`hermes chat -Q`
-per-turn bridge if ACP ever misbehaves.

- **Persona** lives in [AGENTS.md](AGENTS.md) — Hermes auto-injects it because the
  agent runs with this directory as cwd. Edit it to change how Hermes Hal speaks or
  what he calls you. No global Hermes config is touched.
- **Sessions**: each browser session continues one Hermes session
  (`hermes sessions list --source hal-web`). Clear the `hal_session` cookie
  for a fresh one. Hermes' builtin memory still carries facts across sessions.
- **Tools**: it's real Hermes — "Hal, what's in my downloads folder?" works.
  Dangerous-command permission requests follow `HAL_PERMISSION_MODE`:
  - `deny` (default) — every request is rejected and Hermes Hal tells you the
    action was blocked.
  - `ask` — HAL asks ("Dave, I need your permission: …") and waits up to
    `HAL_PERMISSION_TIMEOUT` seconds. Answer by voice or text ("yes" /
    "go ahead" / "no" / "deny"), or with the Allow/Deny bar in the UI.
    Unanswered requests are denied.
  - `yolo` — auto-approve everything (`HAL_YOLO=1` is the legacy alias).
    Understand the real scope before setting this: it is not merely "my
    voice can run shell." Anything that reaches the API runs tools without
    asking. See Security below for what does and doesn't reach it.

## Run

```
./run.sh                # http://127.0.0.1:8000
```

Runs inside the Hermes venv — no separate environment, no `.env`, no API keys.
First start downloads the STT model (~75 MB) to the Hugging Face cache.
`run.sh` is plain bash and works on macOS and Linux.

Or just type `hermes-hal` anywhere — [bin/hermes-hal](bin/hermes-hal) starts the server if
needed, waits for it to become healthy, and opens the eye in your browser
(`hermes-hal --no-open` skips the tab). Install it once with:

```
ln -s "$(pwd)/bin/hermes-hal" ~/.local/bin/hermes-hal
```

## Tests

```
~/.hermes/hermes-agent/venv/bin/python tests/run.py
```

Zero-dependency (no pytest) checks of the pure-python parts — `speakable()`
markdown stripping, session-id validation, history persistence, mission
lifecycle/caps/triggers, the permission registry, CLI output cleanup, and
frontend/backend protocol-contract invariants. Sets `HAL_SKIP_MODELS=1`
internally so no models load; finishes in seconds. The same suite plus
`ruff check .` runs in CI on every push. The audio/inference pipeline is
still verified by running the server.

Browser tests cover the parts of `static/index.html` a Python suite can only
match as strings — chiefly the session-reset path, whose `confirm()` dialog
no other tool in this repo can drive:

```
npm run test:e2e
```

Playwright boots its own instance on port 8123 with `HAL_SKIP_MODELS=1`, so it
needs no models and no inference; it runs in CI alongside the Python
suite.

## Configuration (env vars, all optional)

**STT feels slow?** On CPU (no CUDA GPU — check `stt_device` in `/api/status`
after a restart to confirm), the two levers with real impact are model size
and beam width: `HAL_STT_MODEL=tiny.en` decodes noticeably faster than the
`base.en` default at some accuracy cost, and `HAL_STT_BEAM=1` (greedy) is
roughly 2x faster than the default beam of 5, also with a small accuracy
cost — try beam first, since it's cheaper on accuracy than dropping model
size. In full-duplex mode, interim captions already decode greedily
regardless of `HAL_STT_BEAM` so they hold the shared STT lock briefly; if
captions still aren't worth it to you, `HAL_INTERIM_STT=0` removes them
entirely and frees the whole STT pipeline for the real transcript.

The browser prefers the WebSocket for push-to-talk. It can therefore show
the final user transcript immediately after STT and speak natural commentary
phrases while Hermes is still working. `/api/talk` remains the compatibility
fallback when the socket or streaming audio support is unavailable.

| Var | Default | Purpose |
|-----|---------|---------|
| `HAL_PORT` / `HAL_HOST` | `8000` / `127.0.0.1` | bind address (keep loopback; there is no auth) |
| `HAL_ALLOWED_HOSTS` | `localhost,127.0.0.1` | Host-header allowlist (blocks DNS rebinding) **and** the `Origin` allowlist for state-changing requests (see Security); add your hostname/IP — or `*` — if you bind beyond loopback |
| `HAL_STT_MODEL` | `base.en` | any faster-whisper model; `tiny.en` = fastest, `small.en` = better accuracy, slower |
| `HAL_STT_DEVICE` | `auto` | `auto` uses a CUDA GPU if present, else CPU; force with `cpu`/`cuda` |
| `HAL_STT_COMPUTE_TYPE` | `auto` | quantization for the resolved device (int8 on CPU, float16 on GPU); override with e.g. `int8_float16` |
| `HAL_STT_CPU_THREADS` | `0` | CPU threads for decoding; `0` = ctranslate2 picks (usually all cores) |
| `HAL_VOICE` | `~/.hermes/voices/hal9000/hal9000.onnx` | Piper voice model |
| `HAL_BRIDGE` | `acp` | `acp` = persistent agent process; `subprocess` = one CLI call per turn |
| `HAL_PERMISSION_MODE` | `deny` | `deny` / `ask` / `yolo` — how ACP tool-permission requests are answered (see above) |
| `HAL_PERMISSION_TIMEOUT` | `30` | seconds an `ask` waits before the request is denied |
| `HAL_YOLO` | *(unset)* | legacy alias: `1` = `HAL_PERMISSION_MODE=yolo` |
| `HAL_MAX_ACTIVE_MISSIONS` | `3` | per-session cap on concurrently running background missions |
| `HAL_MISSION_STEERABLE_TTL` | `1800` | seconds a finished mission's session stays alive for `/ask` follow-ups |
| `HAL_CHESS_DEPTH` / `HAL_CHESS_TIME` | `3` / `4` | chess engine search depth and time budget (seconds) |
| `HAL_TRIGGERS_POLL` | `30` | seconds between `data/triggers.json` scans |
| `HAL_INTERIM_STT` | `1` | live interim captions while a duplex utterance records (always greedy-decoded, cheap); `0` disables |
| `HAL_COMMENTARY` | `1` | speak-while-thinking on WS turns: HAL voices each sentence as the agent produces it; `0` restores speak-at-end |
| `HAL_VIEWSCREEN_POLL` | `2` | seconds between scans of `data/viewscreen/` for new visuals |
| `HAL_VOICE_THRESHOLD` | `0.5` | cosine similarity a voiceprint match must reach (see Crew Manifest) |
| `HAL_BOOT_RITUAL` | `1` | speak a short self-test greeting to the first arrival after a server start |
| `HAL_VITALS_REALERT` | `21600` | seconds before an unrecovered vitals trigger re-alerts |
| `HAL_HERMES_ACP_BIN` | auto-detected from the Hermes install | ACP adapter path |
| `HAL_HERMES_BIN` | auto-detected from the Hermes install | Hermes CLI path (subprocess mode) |
| `HAL_HERMES_ARGS` | *(empty)* | extra CLI args in subprocess mode, e.g. `-m gpt-5.4` or `--yolo` |
| `HAL_AGENT_CWD` | this directory | agent working dir (must contain AGENTS.md for the persona) |
| `HAL_AGENT_TIMEOUT` | `180` | seconds before a turn is abandoned |
| `HAL_OFFLINE_PREFLIGHT` | `1` | check connectivity before remote inference; set `0` for a local-only provider |
| `HAL_OFFLINE_CHECK_HOSTS` | `1.1.1.1:443,api.openai.com:443,openrouter.ai:443` | comma-separated connectivity probes, checked in parallel |
| `HAL_OFFLINE_CHECK_TIMEOUT` / `HAL_OFFLINE_CHECK_TTL` | `0.4` / `30` | seconds for the offline preflight timeout and cache |
| `HAL_LATENCY_LOG` | `1` | log per-turn timing breakdowns on the server |
| `HAL_TTS_MASTERING` | `0` | set `1` to restore ffmpeg mastering at the cost of extra latency |
| `HAL_LENGTH_SCALE` etc. | `1.08 / 0.6 / 0.72` | voice pacing/timbre knobs |
| `HAL_DATA_DIR` | `./data` | transcript history + session map |
| `HAL_STT_PROMPT` | *(empty)* | optional whisper bias prompt (helps it spell "HAL"/"Hermes"; can hallucinate on silence) |
| `HAL_STT_BEAM` | `5` | whisper beam size; `1` (greedy) is ~2x faster with slightly lower accuracy |
| `HAL_MAX_UPLOAD_MB` | `25` | reject recordings larger than this (413) |
| `HAL_WAKE_REQUIRE_ATTENTION` | `1` | require an attention word ("hey HAL"), not the bare name; `0` takes the bare name back |
| `HAL_WHISPER_CPP_BIN` | `~/whisper.cpp/build/bin/whisper-cli` | whisper.cpp STT backend; used automatically when the binary and model both exist |
| `HAL_WHISPER_CPP_MODEL` | `~/whisper.cpp/models/ggml-<model>.bin` | ggml model for the above |
| `HAL_FFMPEG_BIN` | `ffmpeg` | ffmpeg used for audio normalization and level probing |
| `HAL_TERMUX_LISTEN` | `0` | run the on-device listen/speak loop (phone mic + speaker, no browser) |
| `HAL_TERMUX_STT_BACKEND` | `whispercpp` | `whispercpp` or `android` (`termux-speech-to-text`) |
| `HAL_TERMUX_CLIP_SECONDS` | `6` | length of one bounded capture in that loop |
| `HAL_TERMUX_SILENCE_DBFS` | `-45` | peak level below which a clip is never decoded |
| `HAL_WAKE_WORD` | `hal` | wake word for the on-device loop; empty disables the gate |
| `HAL_COOKIE_MAX_AGE_DAYS` | `180` | lifetime of the `hal_session` cookie |
| `HAL_SYSTEMS_TTL` | `20` | seconds to cache the `/api/systems` CLI surfaces |

## Hands-free on the phone

`HAL_TERMUX_LISTEN=1` starts an on-device listen/speak loop (`termux_voice.py`)
that uses the phone's own microphone and speaker and needs no browser at all.
It records a bounded clip, drops it if the peak is below the silence gate,
transcribes it through whisper.cpp, requires the wake phrase **"hey HAL"**, runs
the turn, and speaks the reply. **"That'll be all, HAL"** ends the conversation
and clears the session (`farewell.py`).

The wake phrase needs the attention word by design. A bare name cannot be made
safe against a television — a film playing in the room produced a textbook
address that HAL answered. `HAL_WAKE_REQUIRE_ATTENTION=0` takes the bare name
back if you want it. The same knob governs the browser duplex gate, so the two
cannot drift apart.

The loop is sequential: nothing is recording while a turn runs. That costs only
responsiveness today, because this deployment exposes no robot controls. Read
`termux_voice.py`'s docstring before adding any.

Setting the phone up is `bin/termux-setup`, with the reasoning in
[docs/pixel-deployment.md](docs/pixel-deployment.md). Do not `pip install -r
requirements.txt` there — it yields an environment that imports cleanly and
does not work.

On Termux, STT runs on whisper.cpp — `faster-whisper`'s ctranslate2 build there
can never load a Whisper model. It is selected automatically when the binary and
model are both present, so nothing changes on a Mac. If no STT backend loads at
all, audio endpoints return no-speech and `/api/health` reports `stt_device:
n/a`; every text surface keeps working.

## Endpoints

- `GET /` — the eye (hold it, or hold the space bar, to speak; press the eye
  while HAL is talking to barge in; `/` opens a typed command line)
- `POST /api/talk` — HTTP fallback: multipart audio in, WAV out (`X-User-Transcript` /
  `X-Hal-Transcript` response headers, truncated to 2000 chars; full text
  in `/api/history`). Add `?stream=1` for raw 16-bit mono PCM (`audio/L16`,
  rate in `X-Hal-Sample-Rate`) streamed sentence-by-sentence as Piper
  synthesizes — the browser starts playing after the first sentence.
- `POST /api/say` — `{"text": "..."}` in, WAV out; same pipeline minus the
  mic; supports the same `?stream=1`
- `POST /api/session/reset` — forget this browser session's Hermes thread and
  history, issue a fresh cookie (also a button in the Systems drawer)
- `GET /api/health` — includes ACP bridge liveness (`status: degraded` if the
  agent process is down)
- `GET /api/latency` — recent turn timings (the telemetry sparkline)
- `GET /api/status` / `GET /api/systems` / `GET /api/history` — what the
  Systems drawer reads; `/api/systems?refresh=1` bypasses its cache.
  `/api/history` also returns `events`: the journaled terminal
  tool/permission/mission events that let the Bridge log survive a reload
- `GET /api/missions` — this session's missions (plus trigger-created ones),
  newest first; feeds the Missions cards on the desktop Bridge
- `POST /api/missions/{id}/cancel` / `POST /api/missions/{id}/dismiss` —
  interrupt a running mission; drop a finished one from the board and
  release its session (the cards' CANCEL/DISMISS controls)
- `POST /api/permission/{request_id}` — `{"decision": "allow"|"deny"}`;
  answers a pending `ask`-mode tool-permission request (the Allow/Deny bar)
- `GET /api/events` — SSE stream of tool-call/permission/mission events for
  the eye; mission-owned events carry a `mission_session` tag
- `WS /ws/conversation` — preferred channel for push-to-talk, typed input, and
  duplex mode: client sends `start_speech` + binary audio + `end_speech` (or
  `text_input`, or `set_mode` to toggle the wake-word gate), server answers
  with `transcript` frames, `tts_start`, raw PCM, `tts_done` — plus
  `interim_transcript` while you're still speaking — or `turn_aborted` when
  there is nothing to say (`reason: no_wake_word` for gated ambient speech).
  User-owned frames carry a `turn_id`, so late audio or completion frames from
  a barged-in turn cannot disturb its replacement. HAL can also speak first on
  this channel (mission reports, permission prompts).

## Missions (background tasks)

Type `/mission <title>` in the Bridge input — or say "HAL, start mission
<title>" — to run a task in the background. Each mission gets its own Hermes
session seeded with the recent conversation for context; its tool calls
stream into the Mission Log and its card in the Missions panel (status,
elapsed time, tool count — click to expand the result). The record persists
in `data/missions/`, HAL announces a result summary over the live connection
when it finishes, and the full report is fed back into your session's brain
on the next turn — so "what did you find?" works. At most
`HAL_MAX_ACTIVE_MISSIONS` run per session at once.

Missions are steerable. "HAL, cancel the mission" (or a title: "HAL, cancel
mission downloads sweep"), or the card's CANCEL control, interrupts the
agent turn and marks the mission cancelled. "HAL, missions status" gets a
spoken readout of what's running, straight from the records — no inference.
And for 30 minutes after a mission finishes (`HAL_MISSION_STEERABLE_TTL`),
its Hermes session stays alive: "HAL, ask the mission: what exactly did you
change?" — or typed `/ask <question>` — routes the follow-up into the
session that did the work, which answers from its full context rather than
the truncated report. DISMISS drops the card and releases the session early.

Missions obey the same permission model as everything else: with `deny` they
can't run tools; `ask` routes their permission prompts to your Bridge; set
`yolo` only if you accept unattended tool access. Failed missions are also
journaled to `data/missions/failed.jsonl` — food for
`reflection/reflection_loop.py --transcript data/missions/failed.jsonl`.

### Triggers (HAL starts missions on his own)

Create `data/triggers.json` to have HAL open missions without being asked —
on a schedule or when files change:

```json
[
  {"title": "Morning briefing", "prompt": "Compile my morning briefing …", "at": "07:30", "permissions": "allow"},
  {"title": "Morning systems check", "prompt": "Check disk space, memory, and recent errors. Report anomalies.", "every_minutes": 480},
  {"title": "Downloads watcher", "prompt": "A new file arrived in Downloads. Identify it and suggest where it belongs.", "watch": "~/Downloads/*"}
]
```

(A fuller starting point, including the full-digest briefing prompt, is in
[docs/triggers.example.json](docs/triggers.example.json).)

`every_minutes` fires on an interval (armed at boot, no startup storm);
`watch` fires when the newest mtime under the glob advances; `at` fires once
a day at a local time — including a catch-up fire if the laptop was asleep
or the server down at the scheduled moment; `"vitals": {"disk_free_gb_below":
20, "battery_below": 15}` fires when a threshold is first crossed
(edge-triggered — re-alerts only after `HAL_VITALS_REALERT`, default 6h, if
it never recovers). `"enabled": false` disables an entry. Trigger state persists in `data/trigger_state.json`, so restarts
don't re-arm intervals or re-baseline watchers. The file is re-read every
`HAL_TRIGGERS_POLL` seconds, so edits apply without a restart. Trigger
missions report to every connected Bridge session and show in everyone's
Missions panel; if nobody is on the Bridge when one finishes, HAL holds the
report and greets the next session that arrives (first click or keypress —
browsers block speech before a gesture).

`"permissions": "allow"` on a trigger auto-approves that mission's
tool-permission requests (ACP mode) regardless of `HAL_PERMISSION_MODE` —
this is what lets a briefing run shell and web tools under the default
`deny`. You edit the trigger file, you grant the scope; leave it off for
triggers that don't need tools.

## The Initiative (HAL proposes missions)

HAL doesn't just accept missions — he offers them. Two sources:

- **The brain:** when the agent notices something worth doing in the
  background, it asks in prose and ends the reply with a
  `PROPOSE_MISSION: <title> ::: <instructions>` marker line (taught in
  [AGENTS.md](AGENTS.md)). The marker is stripped before speech and
  history; an amber proposal bar appears with Approve/Decline.
- **The ledger:** once a day, if items are due or overdue, HAL greets you
  with an offer to open a mission and clear them.

Answer by voice — the same yes/no grammar as tool permissions, including
the commander rule once voiceprints are enrolled — or with the buttons
(`POST /api/proposal/{id}`). One proposal pends at a time; unanswered ones
expire after 10 minutes. Approved proposals become ordinary missions,
subject to the usual mission cap and permission mode.

## The Care Ledger

HAL keeps a ledger of promises, deadlines, and open loops in
`data/ledger.json`. Three ways in and out:

- **Voice, instantly (no inference):** "HAL, remember to renew the domain"
  · "HAL, what's on my ledger?" / "what are my open loops?" · "HAL, that's
  done" / "mark the domain as done" · "HAL, forget that" / "forget the one
  about the domain". Typed `/remember <text>` works too. (Any "HAL,
  remember …" sentence lands on the ledger — reminiscing counts; "forget
  that" undoes.)
- **The nightly sweep** — a trigger mission (see
  [docs/triggers.example.json](docs/triggers.example.json)) whose prompt
  teaches the agent the schema: it reads the day's conversations and
  mission records, adds open loops conservatively, sets due dates, and
  retires what got resolved. One inference pass per day.
- **Back at you:** items due today ride into the first brain turn of the
  day as a system note (mentioned once, not nagging), and the morning
  briefing leads with them.

## The Crew Manifest (voiceprints)

"HAL, this is Frank" (or "HAL, my name is …") enrolls a voiceprint: HAL asks
for one full sentence, learns the voice, and from then on tags utterances
with the speaker's name so the persona addresses each person correctly.
**The first enrolled voice becomes the commander** — once that happens,
spoken approvals of tool-permission requests (`HAL_PERMISSION_MODE=ask`)
are only accepted from the commander's voice; anyone else saying "yes, go
ahead" is politely refused. Typed and on-screen approvals are unaffected (a
keyboard already implies physical access), and denials stay open to anyone.
"HAL, forget Frank's voice" removes a profile (command succession passes to
the earliest remaining enrollment).

Fully local, like STT and TTS: sherpa-onnx (Apache-2.0) with the 3D-Speaker
CAM++ model (Apache-2.0, ~28MB, auto-downloaded to `data/speaker/` on first
enrollment). Install the one dependency into the Hermes venv:

```
uv pip install --python ~/.hermes/hermes-agent/venv/bin/python sherpa-onnx
```

Without it, enrollment explains itself and everything else works as before.
Measured on this machine: same voice ≈ 0.78 cosine similarity, different
voices ≈ 0.12–0.29 (`HAL_VOICE_THRESHOLD`, default 0.5, splits them with
wide margin). Piper's own HAL voice self-scores ≈ 0.17, so HAL's speech
from the speakers can't false-accept as a crew member. Profiles live in
`data/speakers.json`; treat voice identity as a soft second factor, not
cryptography — a recording of the commander defeats it.

## The Viewscreen

Anything the agent writes into `data/viewscreen/` (PNG/JPEG/GIF/WebP/SVG/
HTML/PDF) appears in a Bridge panel within `HAL_VIEWSCREEN_POLL` seconds —
"On the viewscreen, Dave." One drop-folder gives every Hermes toolset
(image generation, screenshots, charts written by shell tools) a visual
output channel with zero per-tool integration; the persona file teaches HAL
the convention. ◀ ▶ flip through history (newest first), CLEAR empties the
folder, clicking an image opens it full-size. Agent HTML renders in a
sandboxed iframe (no scripts inside the Bridge). Endpoints:
`GET /api/viewscreen`, `POST /api/viewscreen/clear`; files served under
`/viewscreen/`.

## Chess

"HAL, let's play chess" (typed: `/chess`, or `/chess black` to give HAL
white) opens a game against a small clean-room engine built into this repo —
no GPL dependencies (python-chess and sunfish are both GPL), move generation
pinned by perft counts in the test suite. A board panel appears on the
desktop Bridge; play by clicking squares or by voice — "knight to f3",
"e2 to e4", "castle kingside", "queen takes on d5" — and HAL speaks his
replies in register. He reserves the film's line for delivering mate.

"HAL, I resign" (or the panel's RESIGN button) ends the game; NEW starts
another (shift-click to play black). One game per browser session, persisted
in `data/chess/`, so it survives restarts. `HAL_CHESS_DEPTH` (default 3) and
`HAL_CHESS_TIME` (seconds, default 4) set his strength; promotions from
board clicks auto-queen, spoken promotions can name the piece.

Endpoints: `GET /api/chess/state`, `POST /api/chess/new` `{"color":
"white"|"black"}`, `POST /api/chess/move` `{"move": "e2e4"}`,
`POST /api/chess/resign`.

## Autostart at login (optional)

```
cat > ~/Library/LaunchAgents/com.hal9000.frontend.plist <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.hal9000.frontend</string>
  <key>ProgramArguments</key><array>
    <string>/bin/zsh</string><string>REPLACE_WITH/hal/run.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
EOF
launchctl load ~/Library/LaunchAgents/com.hal9000.frontend.plist
```

## Vestigial files from the original Space

`Dockerfile`, `download_model.py`, `.env.example`, and `hal_prompt.py` are no
longer used by this fork (persona and user context now live in `AGENTS.md`).
They are kept for reference / upstream diffing. In particular the Dockerfile
predates the Hermes rewiring and **does not build a working image** — it
neither copies `hermes_bridge.py`/`mission_control.py` nor provides the
Hermes CLI or the HAL voice model.

## Security

There is no authentication. Three things stand between the agent and the
rest of the machine, and it is worth knowing exactly what each one covers:

1. **Loopback binding.** Nothing off-host reaches the API unless you
   deliberately bind wider.
2. **Host allowlist** (`HAL_ALLOWED_HOSTS`, via `TrustedHostMiddleware`).
   Stops DNS rebinding — a hostile page whose hostname resolves to
   127.0.0.1 gets its request rejected on the `Host` header.
3. **Origin allowlist** (same variable). Stops the plainer attack: any page
   you happen to be visiting can `fetch()` this API. `/api/talk` takes
   multipart, which is CORS-safelisted, so the browser sends it with **no
   preflight** — and the handler mints a session when the cookie is absent,
   so `SameSite=lax` withholding the cookie doesn't stop it either. The
   attacker can't read the reply, but the turn still runs. State-changing
   methods (`POST`/`PUT`/`PATCH`/`DELETE`) and the WebSocket handshake are
   now rejected with 403 unless `Origin` is absent (non-browser clients like
   curl and `bin/hermes-hal`) or its host is in the allowlist. `Origin: null` —
   a sandboxed iframe or `file://` page — is rejected.

Agent-written files under `data/viewscreen/` are served with
`Content-Security-Policy: sandbox` and `X-Content-Type-Options: nosniff`, so
an SVG or HTML file the agent produces cannot run scripts against this
origin even if opened directly. PDFs are exempt so the browser's viewer
works.

What none of this covers: anything with local shell access already, and a
`yolo` session driven from the machine's own browser. Treat
`HAL_PERMISSION_MODE=yolo` as "this box's browser can run commands."

## Notes

- English only (the HAL Piper voice is English-only by design).
- Audio replies are 22,050 Hz mono WAV — browsers play this natively.
- STT on push-to-talk audio is decent with `base.en`; if HAL mishears you,
  set `HAL_STT_MODEL=small.en`.
