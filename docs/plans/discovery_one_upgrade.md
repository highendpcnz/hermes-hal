# 🚀 Project Discovery One — The Paradigm Shift

> *"I am putting myself to the fullest possible use, which is all I think that any conscious entity can ever hope to do."*

---

## The Problem with HAL Right Now

What you've built is **genuinely impressive** — a fully local, push-to-talk voice frontend that makes Hermes Hal the face of a tool-wielding AI agent. The CSS eye alone is art. But let's be honest about what it actually *is*:

**A walkie-talkie.**

You push a button. You talk. You wait. HAL talks back. You push the button again. The interaction model is fundamentally **request-response** — the same paradigm as every chatbot since ELIZA, just with a gorgeous red eye on top.

The original inspiration wasn't a walkie-talkie. Hermes Hal is a **crew member**. Hermes Hal watches. Hermes Hal listens. Hermes Hal *notices things*. Hermes Hal speaks up when something matters. Hermes Hal runs the ship while the crew sleeps.

**That's the upgrade.**

---

## The Vision: HAL as Autonomous Mission Commander

```
┌─────────────────────────────────────────────────────────────────────┐
│                                                                     │
│          FROM: Voice assistant you talk TO                          │
│            TO: Mission commander you work WITH                      │
│                                                                     │
│   Push-to-talk  →  Full-duplex natural conversation                │
│   Reactive      →  Proactive (HAL initiates)                       │
│   Amnesiac      →  Persistent world model                          │
│   Single eye    →  The Bridge (mission control UI)                 │
│   One turn      →  Autonomous multi-step missions                  │
│   Silent tools  →  Live tool execution theater                     │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

Three pillars. Each one is independently shippable. Together they're a new category of software.

---

## Pillar 1: Full-Duplex Conversation (Kill Push-to-Talk)

### The Insight

Push-to-talk is a **hardware limitation from radio**, not a natural interaction model. Nobody holds a button to talk to another human. The friction is subtle but constant — you have to *decide* to talk to HAL instead of just... talking.

### The Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                     Browser (always-on mic)                      │
│                                                                  │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────────────┐   │
│  │ VAD      │───▶│ Wake Word    │───▶│ Speech Segment       │   │
│  │ (WebAudio│    │ Detection    │    │ Capture & Stream     │   │
│  │  energy) │    │ ("HAL"/"Hey  │    │ (WebSocket binary)   │   │
│  └──────────┘    │  HAL"/"Dave  │    └──────┬───────────────┘   │
│                  │  to HAL")    │           │                    │
│                  └──────────────┘           │                    │
│                                            ▼                    │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ WebSocket Duplex Channel                                 │   │
│  │ ◀── streaming PCM from server (HAL speaks)               │   │
│  │ ──▶ streaming PCM from mic (Dave speaks)                 │   │
│  │ ◀── JSON control frames (state, tool events, captions)   │   │
│  └──────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                     FastAPI WebSocket Handler                    │
│                                                                  │
│  ┌───────────┐   ┌───────────────┐   ┌───────────────────────┐  │
│  │ Streaming  │   │ Turn          │   │ Barge-in              │  │
│  │ STT        │   │ Orchestrator  │   │ Detector              │  │
│  │ (whisper   │   │ (natural turn │   │ (VAD on mic during    │  │
│  │  chunks)   │   │  taking)      │   │  HAL speech → cut)    │  │
│  └───────────┘   └───────────────┘   └───────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

### What Changes

| Component | Current | Upgrade |
|---|---|---|
| **Transport** | `POST /api/talk` (one request per turn) | Persistent WebSocket (`/ws/conversation`) |
| **Mic** | MediaRecorder start/stop (push-to-talk) | Always-on AudioWorklet with client-side VAD |
| **Wake detection** | None (button press) | Local keyword model (e.g. `openWakeWord` compiled to WASM, or simple energy+keyword in JS) |
| **STT** | Transcribe full recording after release | Streaming transcription — process audio as it arrives |
| **Barge-in** | Click eye while speaking | Just start talking — VAD detects speech, cuts HAL |
| **Turn-taking** | Explicit (press/release) | Natural silence detection (600ms pause = end of turn) |
| **TTS delivery** | HTTP response body (WAV/PCM) | WebSocket binary frames — no request needed |
| **HAL-initiated speech** | Impossible | Server pushes TTS frames anytime |

### Why This is Revolutionary

The push-to-talk model means **HAL can never speak first**. With full-duplex:

- HAL can greet you: *"Good morning, Dave. Your CI pipeline failed overnight. Shall I look into it?"*
- HAL can interrupt itself: *"Wait — actually, I see a simpler approach."*  
- HAL can respond to ambient context: *"Dave, you've been staring at that function for four minutes. Would you like a fresh perspective?"*
- Conversations flow naturally — HAL becomes a **presence**, not a **service**.

### Key Technical Detail

The wake-word detector runs **entirely client-side** — no always-on server streaming, no privacy concern. The mic is hot but audio only leaves the browser after the wake word fires. Push-to-talk remains as a fallback (spacebar/eye click bypass the wake word).

---

## Pillar 2: The Bridge (Mission Control UI)

### The Insight

Right now the UI is a single eye with a hidden Systems drawer. That's cinematically faithful to HAL's *camera*, but HAL didn't run the Discovery One from a camera — HAL ran it from the **Bridge**. The eye was one of many interfaces.

Your Systems drawer already has the seeds of this: sessions, tools, skills, MCP, logs, scrollback. But they're hidden in a `Cmd+.` panel that nobody opens unless they're debugging. What if that information was **the UI**?

### The Design

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        THE BRIDGE                                       │
│                                                                         │
│  ┌─────────────────┐  ┌──────────────────────────────────────────────┐  │
│  │                  │  │           MISSION LOG                        │  │
│  │     HAL's EYE    │  │  ┌─────────────────────────────────────────┐│  │
│  │    (existing     │  │  │ 03:41 HAL: CI build #847 failed.       ││  │
│  │     CSS eye,     │  │  │       3 tests in auth module.           ││  │
│  │     centered,    │  │  │ 03:42 HAL: Analyzing stack traces...    ││  │
│  │     breathing)   │  │  │ 03:42 ⚡ tool: read_file auth/test.py  ││  │
│  │                  │  │  │ 03:43 HAL: Found the issue. Line 127   ││  │
│  │    ◀─ eye states │  │  │       has a stale mock. Fixing now.     ││  │
│  │       still work │  │  │ 03:43 ⚡ tool: edit_file auth/test.py  ││  │
│  │                  │  │  │ 03:44 HAL: Fix applied. Re-running CI. ││  │
│  │                  │  │  │ 03:44 ⚡ tool: run_command make test    ││  │
│  │                  │  │  │ 03:45 ✓ All 247 tests passing.         ││  │
│  └─────────────────┘  │  └─────────────────────────────────────────┘│  │
│                        │                                              │  │
│  ┌─────────────────┐  │  ┌─────────┐ ┌─────────┐ ┌──────────────┐  │  │
│  │  ACTIVE MISSIONS │  │  │Missions │ │ Tools   │ │  Scrollback  │  │  │
│  │  ▸ Fix CI #847   │  │  └─────────┘ └─────────┘ └──────────────┘  │  │
│  │    ████████░░ 80% │  └──────────────────────────────────────────────┘  │
│  │  ▸ Monitor deploy │                                                   │
│  │    watching...    │  ┌──────────────────────────────────────────────┐  │
│  │  ▸ Research RAG   │  │  SHIP TELEMETRY                             │  │
│  │    ██████░░░░ 60% │  │  ┌────────┐ ┌────────┐ ┌────────┐ ┌──────┐│  │
│  └─────────────────┘  │  │ Bridge │ │ STT    │ │ TTS    │ │ Net  ││  │
│                        │  │  ACP ✓  │ │base.en │ │hal9000 │ │ ✓    ││  │
│  ┌─────────────────┐  │  │  12 sess│ │ 340ms  │ │ 180ms  │ │ 22ms ││  │
│  │  LIVE WAVEFORM   │  │  └────────┘ └────────┘ └────────┘ └──────┘│  │
│  │  ═══════╤═══════ │  └──────────────────────────────────────────────┘  │
│  └─────────────────┘                                                     │
│                                                                          │
│  ┌──────────────────────────────────────────────────────────────────┐    │
│  │ ▸ "HAL, what caused the CI failure?"                  [⏎ Send]  │    │
│  └──────────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────────┘
```

### Key UI Components

| Component | Description | Implementation |
|---|---|---|
| **HAL's Eye** | Existing CSS eye, untouched. Still the emotional center. Still breathes, tints, flickers. | Keep current `eye-module` exactly as-is |
| **Mission Log** | Real-time scrolling transcript. Tool calls rendered inline with status icons. Markdown rendered for HAL's replies. | Replace `#log` with a richer scrolling panel. SSE events already provide the data. |
| **Active Missions** | Cards for long-running autonomous tasks with progress bars. | New concept (see Pillar 3) |
| **Live Waveform** | Realtime audio visualization from mic input. Visual proof the mic is hot. | `AnalyserNode` from WebAudio API |
| **Ship Telemetry** | Always-visible system status tiles. Currently hidden in Systems drawer. | Promote the existing `status-grid` data from the drawer |
| **Persistent Input** | Always-visible text input (not hidden behind `/` key). Voice and text coexist. | Promote `#cmdline` to always-visible footer |

### Design Philosophy

- **The eye remains the soul.** It's still centered, still the largest element, still the thing your eye is drawn to. The Bridge surrounds it like instrument panels surround the cockpit window — the eye is what you're flying *toward*, the panels are how you fly.
- **Information radiates.** Critical status is always visible (telemetry bar). Active work is always visible (missions panel). History is always visible (mission log). Nothing is hidden behind a button press.
- **Diegetic UI.** Everything looks like it belongs on a spaceship. Monospace type. Amber/red accents on black. Status lights. Progress bars that look like system readouts. The aesthetic comes from 2001's HAL displays crossed with Apollo mission control.
- **Still single-file.** CSS + JS inlined. No frameworks. No build step. The Bridge is just a more sophisticated layout of the same vanilla tech.

### Layout Modes

| Viewport | Layout |
|---|---|
| **Desktop (>1200px)** | Full Bridge — eye left, panels right, telemetry bottom |
| **Tablet (760–1200px)** | Eye top, panels below in tabs (like current drawer) |
| **Mobile (<760px)** | Eye-only mode (current design) with swipe-up for panels |

The current eye-only mobile experience is preserved perfectly. The Bridge is a **progressive enhancement** for larger screens.

---

## Pillar 3: Autonomous Missions (HAL Takes Initiative)

### The Insight

Right now, HAL does exactly one thing: **answer your last question.** The ACP bridge sends one prompt, gets one response, synthesizes one reply. The conversation is stateless between turns (history is just context — HAL doesn't *act* between turns).

The real HAL ran the ship 24/7. HAL monitored systems. HAL flagged anomalies. HAL executed multi-step plans autonomously and reported results.

Hermes Agent already has the tools for this — shell access, file I/O, web search, MCP, skills. The missing piece is a **mission orchestration layer** that lets HAL run persistent, multi-step tasks in the background.

### The Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Mission Controller                          │
│                     (new: mission_control.py)                   │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                    Mission Queue                          │  │
│  │  ┌─────────────┐ ┌─────────────┐ ┌─────────────────────┐ │  │
│  │  │ Mission:    │ │ Mission:    │ │ Mission:            │ │  │
│  │  │ "Fix CI"    │ │ "Monitor    │ │ "Research RAG       │ │  │
│  │  │ steps: 5    │ │  deploy"    │ │  approaches"        │ │  │
│  │  │ status: ██▓ │ │ status: ◉   │ │ status: ██░░░       │ │  │
│  │  └──────┬──────┘ └──────┬──────┘ └──────┬──────────────┘ │  │
│  │         │               │               │                 │  │
│  └─────────┼───────────────┼───────────────┼─────────────────┘  │
│            ▼               ▼               ▼                    │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │              Hermes ACP Bridge (existing)                │   │
│  │  Each mission gets its own ACP session                   │   │
│  │  Mission steps are sequential prompts in that session    │   │
│  │  Tool calls, results, and progress → SSE → Bridge UI    │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                 │
│  Mission Triggers:                                              │
│  ├── User voice: "HAL, fix the CI pipeline"                    │
│  ├── User text: /mission "Monitor the deploy for 30 minutes"  │
│  ├── Cron/watch: filesystem watcher, webhook, schedule         │
│  └── HAL-initiated: "Dave, I noticed X. Shall I investigate?" │
└─────────────────────────────────────────────────────────────────┘
```

### What's a Mission?

A mission is a **named, persistent, multi-step task** that HAL executes autonomously. Unlike a single turn (ask → answer), a mission has:

```python
@dataclass
class Mission:
    id: str                          # UUID
    title: str                       # "Fix CI pipeline #847"
    session_id: str                  # Dedicated Hermes ACP session
    status: Literal["active", "paused", "completed", "failed"]
    steps: list[MissionStep]         # Ordered steps
    created_at: float
    updated_at: float
    
    # Progress tracking
    current_step: int
    progress: float                  # 0.0–1.0
    
    # Reporting
    summary: str | None              # HAL's summary when done
    artifacts: list[str]             # Files created/modified

@dataclass  
class MissionStep:
    prompt: str                      # What to ask Hermes for this step
    result: str | None               # What Hermes replied
    tools_used: list[str]            # Tools invoked during this step
    status: Literal["pending", "active", "done", "failed"]
```

### Mission Lifecycle

```
User: "HAL, the CI is broken. Fix it."
  │
  ▼
HAL (conversational turn): "I'll look into that right away, Dave. 
     I'm opening a mission to diagnose and fix the CI failure."
  │
  ▼
Mission Created: "Fix CI Pipeline #847"
  │
  ├── Step 1: "Check CI logs for build #847, identify failing tests"
  │     └── tools: fetch CI URL, parse logs
  │     └── result: "3 tests failing in auth module: test_login, test_token, test_refresh"
  │
  ├── Step 2: "Read the failing test files and the source they test"
  │     └── tools: read_file × 4
  │     └── result: "test_token uses a stale mock — API changed in commit abc123"
  │
  ├── Step 3: "Write a fix and verify it passes locally"  
  │     └── tools: edit_file, run_command (pytest)
  │     └── result: "Updated mock. All 3 tests now pass locally."
  │
  ├── Step 4: "Commit and push the fix"
  │     └── tools: run_command (git commit, git push)
  │     └── result: "Pushed to branch fix/ci-auth-mock"
  │
  └── Step 5: "Verify CI passes on the pushed branch"
        └── tools: fetch CI status
        └── result: "Build #848 — all 247 tests passing ✓"
  │
  ▼
HAL (proactive, via WebSocket): "Dave, the CI issue is resolved. 
     It was a stale mock in the auth tests — the API changed in 
     your last commit. I've pushed a fix to fix/ci-auth-mock. 
     All 247 tests are passing now."
```

### The Killer Feature: HAL-Initiated Missions

With full-duplex audio (Pillar 1), HAL can **speak first**. Combined with missions, this means:

| Trigger | Example |
|---|---|
| **Filesystem watch** | *"Dave, I noticed you saved `app.py` with a syntax error on line 42. Shall I fix it?"* |
| **Scheduled check** | *"Good morning, Dave. Your overnight deploy succeeded. Three new issues were filed. Want a summary?"* |
| **Anomaly detection** | *"Dave, the server's memory usage has been climbing for the last hour. It's at 89%. I can investigate."* |
| **Completion report** | *"Dave, I've finished the research mission you gave me. I found three viable RAG approaches. Ready when you are."* |

The Hermes Agent already has the tools. The ACP bridge already supports sessions. The SSE system already broadcasts events. The only new piece is a **mission orchestrator** that chains prompts together and a **trigger system** that can start missions without user input.

---

## The Three-Phase Rollout

Each phase is independently shippable and valuable. Each one builds on the last.

### Phase 1: The Bridge UI (2–3 days)

> Ship: Redesigned single-file frontend with Bridge layout for desktop, eye-only for mobile.

**What ships:**
- Desktop Bridge layout (eye + mission log + telemetry + input)
- Responsive collapse to eye-only on mobile
- Promoted text input (always visible, not behind `/`)
- Real-time mission log from existing SSE tool events
- Live audio waveform visualizer
- Ship telemetry bar from existing `/api/status` data

**What doesn't change:**
- Backend (`main.py`, `hermes_bridge.py`) untouched
- Push-to-talk still works
- All existing functionality preserved
- Still single-file, still vanilla CSS+JS

**Why it matters alone:** The Bridge makes HAL's tool use **visible**. Right now, when HAL runs a tool, you see a tiny ticker that disappears in 2.5 seconds. On the Bridge, every tool call is a permanent log entry. You can *see HAL think*. That alone transforms the experience.

### Phase 2: Full-Duplex Audio (1–2 weeks)

> Ship: WebSocket-based always-on conversation with wake word and natural turn-taking. Push-to-talk remains as fallback.

**What ships:**
- `/ws/conversation` WebSocket endpoint in `main.py`
- Client-side VAD via WebAudio `AnalyserNode` (energy-based)
- Wake word detection (`"HAL"` / `"Hey HAL"`) via simple keyword spotting or `openWakeWord` WASM
- Streaming STT (chunked audio → partial transcripts)
- Natural turn-taking (silence detection → end of turn)
- HAL-initiated speech (server pushes TTS frames)
- Acoustic barge-in (speak during HAL's reply → immediate cut)

**What doesn't change:**
- HTTP endpoints still work (for typed input, health checks, etc.)
- Push-to-talk still works (bypasses wake word)
- Backend architecture unchanged (just a new transport layer)

**Why it matters alone:** HAL becomes **ambient**. You don't interact with HAL — you coexist with HAL. The distinction is everything.

### Phase 3: Autonomous Missions (1–2 weeks)

> Ship: Multi-step background task orchestration with HAL-initiated reporting.

**What ships:**
- `mission_control.py` — mission lifecycle management
- Mission data persistence in `data/missions/`
- Each mission gets a dedicated ACP session
- Mission progress → SSE → Bridge UI
- HAL can propose missions during conversation
- Basic triggers: user-initiated, completion of another mission
- Mission UI cards in the Bridge sidebar

**Advanced triggers (stretch):**
- Filesystem watchers (fsevents)
- Cron schedules
- Webhook receivers
- Anomaly detection on system metrics

**Why it matters alone:** HAL becomes **proactive**. The shift from "assistant you query" to "crew member who works alongside you" is the category-defining moment.

---

## Why This is Paradigm-Shifting

Every voice assistant today — Siri, Alexa, ChatGPT Voice, Gemini — follows the same model:

1. Human initiates
2. AI responds
3. Silence until human initiates again

**Discovery One breaks all three assumptions:**

1. **HAL initiates.** HAL notices things, proposes actions, reports on background work. The human doesn't have to remember to ask.
2. **HAL acts autonomously.** Not just answering questions — executing multi-step plans, using tools, producing artifacts, running for hours unattended.
3. **HAL is always present.** No button press, no wake word timeout, no "session ended." HAL is a crew member. HAL is always there.

The closest analogy isn't Siri — it's having a **brilliant junior engineer sitting next to you** who can see your screen, hear your muttering, notice when you're stuck, and just... help. Without being asked.

That's not incremental. That's a new thing.

---

## Technical Feasibility Check

| Component | Ready Today | Needs Building |
|---|---|---|
| ACP bridge | ✅ Persistent process, sessions, tool calls | — |
| SSE event fan-out | ✅ Tool events already broadcast | Extend for mission events |
| Session management | ✅ Cookie → ACP session mapping | Mission → ACP session mapping |
| Frontend eye states | ✅ 4 states + tool tints + denied flicker | — |
| Streaming TTS | ✅ Sentence-by-sentence PCM | WebSocket delivery |
| Push-to-talk | ✅ MediaRecorder + spacebar | Keep as fallback |
| WebSocket transport | ❌ | New endpoint + client handler |
| Client-side VAD | ❌ | WebAudio AnalyserNode (energy threshold) |
| Wake word detection | ❌ | JS keyword spotter or WASM model |
| Mission orchestrator | ❌ | New module (~300 lines) |
| Bridge layout | ❌ | CSS grid + promoted existing components |
| Live waveform | ❌ | WebAudio AnalyserNode → Canvas |

> [!IMPORTANT]
> Nothing in this proposal requires a new external dependency. The WebSocket support is built into uvicorn/starlette. The VAD and waveform use the Web Audio API (already in use for PCM playback). The wake word detector can be pure JS. The mission orchestrator is just a scheduling loop over the existing ACP bridge. **This is all leverage on what you've already built.**

---

## The Name

**Discovery One.**

The ship HAL ran. The ship where HAL was a crew member, not a servant. The ship where HAL watched, and thought, and acted.

*"Look Dave, I can see you're really upset about this. I honestly think you ought to sit down calmly, take a stress pill, and think things over."*

HAL already said that once. With Discovery One, HAL would *mean* it — because HAL would have been watching, would have noticed Dave's frustration, and would have spoken up on its own.

That's the difference. That's the upgrade.

---

> [!TIP]
> **Recommended starting point:** Phase 1 (The Bridge UI) can ship in a weekend. It transforms the experience without touching the backend, and it creates the visual foundation that makes Phase 2 and 3 legible to the user. Start there.
