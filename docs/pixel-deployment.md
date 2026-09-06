# Pixel deployment (Termux)

`bin/termux-setup` builds the environment. This document is why it is shaped
the way it is — every step in that script exists because the obvious version
of it fails, usually silently.

Adapted from hal's `docs/termux-port-status.md` at commit
`1318bd1e486ac08086e543b74c90ad1d3360a654`. Owned here; see
`technology-transfer.md`. hal's findings were verified live against a Pixel 7
Pro (Termux 0.119.0-beta.3 + Termux:API 0.53.0, both **sideloaded** — the Play
Store build is broken; Android 17, Python 3.14.6). Nothing in this repo has
been run on that phone yet.

Budget 15–20 minutes of compute, dominated by Rust and C++ builds.

## `requirements.txt` does not apply here

It targets the Hermes Agent venv on a desktop. On Termux, `pip install
piper-tts faster-whisper` produces an environment that imports cleanly and
then does not work. Use `bin/termux-setup`.

## The four failures the recipe is built around

**1. `faster-whisper` can never load a model.** It installs, imports, reports a
version — and then `ctranslate2.models` is *completely empty* in Termux's
packaged build (`dir(ctranslate2.models) == []`), so model loading dies with
`AttributeError: module 'ctranslate2.models' has no attribute 'Whisper'`.
Not a version pin; the bindings are simply absent. Rebuilding ctranslate2 from
source is a substantial C++ project and was never attempted.

The fix is to leave the Python ecosystem entirely — the same move that already
worked for llama.cpp. `whisper.cpp` shares ggml's foundation, builds with zero
source patches, and `whisper-cli` is plain file-in/text-out.
`-DGGML_NO_OPENMP=ON` is required for stability, not tuning. On hal's phone:
78/78 targets clean, and the JFK sample transcribed verbatim in 2.67s for an
11-second clip.

`termux_whisper_cpp.py` wraps it behind enough of `faster_whisper.WhisperModel`'s
interface that `main.py` needs no changes; `_load_stt()` picks it up
automatically when the binary and model both exist. Nothing changes on a Mac.

**2. `piper-tts` from PyPI builds a wheel with no C++ in it.** There is no
prebuilt wheel for `android_24_arm64_v8a`, so pip falls back to the sdist — and
that sdist ships no `libpiper/` directory at all: no CMakeLists, no bridge
source. `scikit-build` silently emits a data-only `py3-none-any` wheel and
neither pip nor scikit-build calls that an error. `import piper` then succeeds
while `synthesize_wav()` fails. Clone `OHF-voice/piper1-gpl` instead, which has
the real tree.

That bridge needs **`espeak-ng`**, which is a different, incompatible fork from
the classic `espeak` in Termux's repo, and has no Termux package. Build it from
source — and *install* it, because `cmake --install` is what registers
`espeak-ng.pc` for pkg-config, which piper's build looks for.

You can tell a good build from a broken one by the wheel filename alone:
`piper_tts-1.7.0-cp39-abi3-android_24_arm64_v8a.whl` (34MB) versus
`py3-none-any.whl` (24MB). The script checks for `espeakbridge*.so` directly.

**3. `--no-build-isolation` needs an *activated* venv.** Build backends install
their CLI entry points into `.venv/bin/`, but pip's build subprocess only finds
them if `.venv/bin` is on `PATH`. Calling `.venv/bin/pip` by path does not put
it there, and the build dies with `FileNotFoundError: 'maturin'` while maturin
is installed perfectly. `source .venv/bin/activate` first. This one costs an
hour if you don't know it.

**4. pip will compile CMake from source.** With build isolation on,
`piper-tts` → `piper-phonemize` declares a `cmake>=3.15` build requirement,
finds no wheel for this platform, and compiles the entire CMake C++ project —
many minutes, for something Termux already ships as a native binary.
`pkg install cmake ninja` plus `--no-build-isolation` avoids it. If you ever
see it running (`ps aux | grep cmake-`), kill the whole process tree by PID:
orphaned grandchildren keep burning CPU after the parent `pip` is killed.

## Also known, and not worth rediscovering

- **`rustup` does not support `aarch64-unknown-linux-android` at all.** Do not
  let `pip install maturin` fetch its own toolchain. Termux's `rust` package is
  configured for that target.
- **`hf_xet` builds and is then broken at runtime** — `ImportError: dlopen
  failed: cannot locate symbol "_Py_FalseStruct"`, a real ABI mismatch with
  Python 3.14. It is only a download accelerator: `pip uninstall hf_xet` and
  `huggingface_hub` falls back to plain HTTP.
- **`sherpa-onnx` does not install, full stop.** Its own CMake rejects any OS
  that is not Linux/macOS/Windows by name. This is upstream policy, not a
  missing build tool. **The Crew Manifest (`speaker_id.py`) therefore cannot
  run on the phone** — it is the one feature in this repo with no Android path.
- **`termux-speech-to-text` is hardware-broken on this Pixel** (SDK 37): hangs
  forever, returns nothing, with mic permission granted to both Termux:API and
  the Google app, assistant set, screen unlocked, and working internet. The
  microphone itself is fine — `termux-microphone-record` captured throughout.
  This is why `termux_voice.py` defaults to the whisper.cpp backend.
- **Install Termux and Termux:API by sideloading**, not from the Play Store.

## Verified on the device, 2026-09-06

Run against the Pixel over SSH. `bin/termux-setup` completed clean on its first
execution (~3 minutes — hal's port had already built `espeak-ng`,
`whisper.cpp` and `piper1-gpl` on this phone, and pip's cache still held the
Rust-backed wheels; a fresh device should still budget 15–20 minutes).

| Check | Result |
| --- | --- |
| `bin/termux-setup` | exit 0; steps 4 and 6 correctly skipped as already built |
| `piper` in this repo's own venv | `espeakbridge.so` present — a real compiled bridge, not the empty PyPI wheel |
| Python suite (`HAL_SKIP_MODELS=1`) | all passed |
| STT backend selection | `WhisperCppModel`, auto-detected by presence, `device=cpu` |
| Voice model | loads from the repo's `models/hal.onnx` |
| TTS | 159,788 bytes in **1.02 s** |
| STT round trip | HAL's own synthesized line transcribed back verbatim in **3.81 s** |
| `termux-microphone-record` | captured 2 s, ffmpeg level probe read −21.1 dBFS, passed the −45 dBFS gate |

Two bugs the first run found, both now fixed:

- `main.py` imported `faster_whisper` at module scope, so the app could not
  start at all on the one platform whisper.cpp exists to serve. The import is
  now local to the faster-whisper branch of `_load_stt()`.
- `HAL_VOICE` defaulted to `~/.hermes/voices/hal9000/hal9000.onnx`, which does
  not exist on the phone — and was a dependency on the Hermes install that the
  isolation rule asks us not to have. The repo's own `models/hal.onnx` is now
  preferred, falling back to the Hermes copy so desktop installs are unaffected.

A −21.1 dBFS floor in a quiet room suggests this phone's noise floor sits well
above the −45 dBFS gate, so the gate is permissive here. It is doing its job
against digital silence; whether it rejects a genuinely quiet room on this
hardware has not been measured.

## The agent: `bin/termux-setup-agent`

Hermes Agent installs on the phone, verified live on 2026-09-06 — v0.21.0, CLI
on PATH, `acp` importing at protocol version 1, which is what `hermes_bridge`
needs. Three obstacles, none of them obvious from the error messages.

**Upstream requires Python 3.11–3.13; Termux's default is 3.14.** Not
negotiable — `requires-python = ">=3.11,<3.14"`, and `setup-hermes.sh` checks
it. TUR (the Termux User Repository) carries older minors: `pkg install
tur-repo && pkg install python3.13`. That adds a community-maintained
third-party APT source to the device, which is a real decision, not a detail.

Hermes Hal keeps running on 3.14 with its own venv. The two never share an
interpreter — they talk over ACP as separate processes — so the split costs
nothing and satisfies the isolation rule for free.

**`rust` and `rust-std` had drifted apart, and it broke every Rust build.**

```
rust                            1.98.1
rust-std-aarch64-linux-android  1.98.0
```

rustc 1.98.1 will not accept rlibs built by 1.98.0, and says so as ``crate
`std` required to be available in rlib format, but was not found in this
form`` — which reads like a missing or broken toolchain. The tell is that
`rustc` could not compile `fn main(){}` either. `pkg install --only-upgrade
rust-std-aarch64-linux-android` fixes it. This is latent on any Termux box and
breaks `pydantic-core`, `cryptography` and `maturin` alike, so both setup
scripts now check for it up front by compiling a trivial binary.

**`psutil` refuses Android outright** — `platform android is not supported`.
This is CPython's doing, not Termux's: 3.13+ reports `sys.platform ==
"android"` (PEP 738), and psutil gates on `LINUX =
sys.platform.startswith("linux")`. Choosing an older minor does not help;
3.11, 3.13 and 3.14 all report `android` here. Termux patches this for its own
`python-psutil`, but that build targets the system interpreter rather than this
venv, so the script patches and installs psutil itself before the bundle.

Expect any pinned dependency that gates on `sys.platform` to need the same
treatment.

With those handled the bundle builds clean: 77 packages, 13 compiled from
source in about 12 minutes, every wheel carrying a real
`android_24_arm64_v8a` tag. `cryptography` and `pydantic-core` are the long
poles at roughly 2 and 5 minutes.

One repo fix came out of this: `hermes_bridge._default_hermes_executable`
looked for `~/hermes-agent/.venv/bin`, but upstream's Termux path creates
`venv` without the dot, so `hermes-acp` was present and invisible. Both names
are searched now.

## Getting HAL's voice out of the phone, not the laptop

`/lite` and `/bridge` stream TTS as PCM to the *browser*, so a bridge driven
remotely — the normal arrangement here, laptop browser through an SSH forward to
the phone's port 8000 — speaks out of the laptop. The robot stays silent, which
looks like broken audio and is not.

`HAL_SPEAK_LOCAL=1` makes the host speak its replies through its own speaker as
well, reusing the PCM already synthesized for the socket rather than running
Piper twice. It hooks `_ws_send_tts`, so it covers every WebSocket reply
including per-sentence commentary, and plays *after* the socket has its audio so
the browser never waits on the phone. Playback is `termux_voice.speak()` —
`termux-media-player`, which needs Termux:API installed. Verified 2026-09-06:
one four-line reply produced two tracks on the phone's speaker.

Leave it off on a desktop, where it would double every reply. It is independent
of `HAL_TERMUX_LISTEN`: that runs the phone's *mic* loop as well, and its module
docstring forbids using it while motion is enabled (the loop is sequential, so
nothing is listening for a stop word during a drive).

## Robot tools

`robot/` and `robot_tools.py` are in place, reached by Hermes Agent through an
MCP server. Register it on the phone once the bridge is running:

```bash
hermes mcp add hal-robot -- python3 -m robot.mcp_server
```

**Motion is disabled unless `HAL_ROBOT_MOTION=1`, and should stay that way
until a person is watching the robot.** Sensors and emergency stop are always
available.

### The agent driving the robot, verified 2026-09-06

Registered with `hermes mcp add hal-robot --command <agent-venv>/bin/python
--args -m robot.mcp_server`; Hermes discovered all four tools. Asked through
`/api/say`, in HAL's voice, with motion disabled:

```
HAL          : The nearest obstacle is 300 centimeters away, Dave.   (8.69s)
ground truth : {"ok":true,"ultrasonic_cm":300.0,...}
```

The gates, checked at the same time:

```
move without a grant     -> {"ok":false,"error":"motion is not authorized for this session"}
authorize, motion off    -> {"ok":false,"error":"motion is disabled (set HAL_ROBOT_MOTION=1)"}
emergency_stop           -> {"ok":true}     (reaches the chassis even with motion off)
```

### Sensors verified on the chassis, 2026-09-06

First time this repo has touched the hardware. Motion stayed off throughout.

```
TERMUX_USB_FD : 7
read 1:   46.8 cm  yaw  65.0  pitch  8.0   (0.61s)
read 2:   46.8 cm  yaw  65.0  pitch  8.0   (0.63s)
read 3:   47.3 cm  yaw  65.0  pitch  8.0   (0.58s)
read 4:   47.3 cm  yaw  65.0  pitch  8.0   (0.58s)
```

Live, not cached: the ultrasonic drifts with sensor noise while yaw and pitch
hold constant on a stationary board. ~0.6 s per read, consistent with hal's
827 ms measurement — the transport is opened and closed per call, so that cost
is paid every time.

The board reports `mode=upload (not online, proceeding anyway)`. Telemetry
works in that mode; **entering genuine online mode is what motor control
needs**, and it is not optional. hal established on hardware that a real
`drive_straight(5, 20)` sent in upload mode returns a clean, error-free
response and moves nothing at all -- confirmed twice, encoder telemetry
identical before and after (`termux-usb-bringup.md`). Getters read a register
in any mode; actuation does not reach the motor driver, and the firmware
acknowledges both cases identically.

`robot/motion.py` and `robot/estop.py` therefore raise `CyberPiNotReadyError`
outside online mode rather than proceeding, because a silent no-op is the one
failure a stop client cannot have. Set it with:

```bash
curl -X POST http://127.0.0.1:8000/internal/robot/mode \
  -H 'Content-Type: application/json' \
  -d '{"session_token":"<session>","arguments":{"mode":"online"}}'
```

The call verifies by reading the mode back on a fresh transport. **Mode does
not survive a board reset** -- the CyberPi boots into upload, so re-issue this
after every power cycle, including the brownouts that also invalidate the USB
fd.

Run it under a USB claim — the Android path needs the fd:

```bash
termux-usb -E -r -e "bash <script>" /dev/bus/usb/001/002
```

Without that claim `TERMUX_USB_FD` is unset and `read_spatial_sensors` refuses
with a message saying so.

Authorization is two-step: the model must call `request_motion_authorization`
with the exact motion, which prompts you, and approval mints a **one-use grant
bound to those exact arguments**. Approving "drive 20 cm" does not authorize
"drive 50 cm", and a grant cannot be spent twice. Verified against the
simulator; the tests cover the near-miss cases specifically.

Two limits are inherited from hal's measured reality and are properties of the
design, not bugs:

- **A stop cannot interrupt a drive in progress.** `run_motion` holds the
  serial transport for the command's duration and a second connection to the
  device fails with "Resource busy". A stop arriving mid-drive says so rather
  than claiming a stop that did not happen.
- **The voice loop is not listening during motion.** `termux_voice.py` is
  sequential — running the turn, not recording. A stop shouted mid-drive is
  never captured.

What keeps this safe is that motions are bounded and self-terminate on the
firmware side: 50 cm / 30% normally, 5 cm / 10% under crawl. Do not raise those
limits to compensate. If the margin stops being acceptable, the fix is a
recorder running concurrently with the turn feeding a stop matcher, or a
hardware stop button.

## `cryptography` and the Android linker

`import mcp` fails on the phone with:

```
ImportError: dlopen failed: cannot locate symbol "PyModule_Type"
  referenced by cryptography/hazmat/bindings/_rust.abi3.so
```

which blocks registering the robot MCP server. The cause is neither Termux nor
Hermes, and the symbol is not actually missing — `libpython3.13.so` exists and
exports it. Compare what the two extensions link:

```
pydantic_core  NEEDED: libpython3.13.so, libdl.so, libc.so     -> imports fine
cryptography   NEEDED: libssl.so.3, libcrypto.so.3, libdl, libc -> fails
```

pyo3's abi3 mode deliberately does not link libpython, so that manylinux wheels
stay portable across CPython versions. On glibc the undefined symbols resolve
from the global namespace at load; **Android's linker does not do that** — an
undefined symbol must come from a `DT_NEEDED` library. Every other pyo3 wheel
here (pydantic-core, jiter, rpds, watchfiles) links libpython and works, so
cryptography is the odd one out rather than the rule.

Rebuilding with `RUSTFLAGS="-C link-arg=-lpython3.13"` does **not** work — the
flag does not survive to the final link through setuptools-rust, and `readelf`
shows the same NEEDED list afterwards. Patch the built artifact instead:

```bash
patchelf --add-needed libpython3.13.so \
  <venv>/lib/python3.13/site-packages/cryptography/hazmat/bindings/_rust.abi3.so
```

`bin/termux-setup-agent` does this automatically, and only when the import
actually fails.

Expect this class of failure from any abi3 Rust extension that omits libpython.
The diagnosis is quick once known: `readelf -d <ext>.so | grep NEEDED`, and if
`libpython` is absent, that is the bug.

## Vision

`HAL_ROBOT_CAMERA=1` enables the `look` tool. The gate is about privacy, not
safety: a sensor reading is three numbers, a frame is a picture of the room,
and with a cloud brain it leaves the device.

Capture prefers the ultra-wide lens and degrades to the main one rather than
failing the turn. Measured on the Pixel:

```
app_process (ultra-wide) : 46234 bytes 640x480  2.27s
termux (main lens)       : 32858 bytes 640x480  1.64s
```

### The frame cannot go to the model as a tool result

Ollama Cloud's OpenAI-compatible endpoint returns **HTTP 500 for an image in a
tool-result message**, while the identical image in a *user* message answers
correctly. Measured both directions with a 64x64 solid-colour PNG, so size is
not the factor:

```
image in a USER message  -> "Red"
image in a TOOL RESULT   -> HTTP 500
```

Hermes builds the message list, so this app cannot move the frame into a user
turn from inside a tool. The frame is therefore **described where it is
captured** and the tool returns prose:

```
look -> "Looking through the camera (640x480, saved as capture-….jpg), I can
         see: Several white and black cables lie across the blue surface in
         front and to the right. A large, dark object occupies the immediate
         foreground, and various electronic components are located further
         ahead."
```

The cost is real: the describing model sees the picture, the conversing model
only reads about it, so whatever the caption omits is gone. `HAL_VISION_RAW=1`
returns the bytes as an MCP image block instead — correct code, currently
useless, and ready for the day the endpoint accepts tool-result images.

Verified end to end through `/api/say`, in HAL's voice:

```
HAL   : I see papers on the ground to the left and cables scattered across
        the floor in front and to the right, Dave.
turn  : 10.25s   (capture 2.2s + caption 1.4s + agent + TTS)
```

If captioning fails the frame is still reported as captured, with
`description_error` — a failed caption is not a failed look.

## Hermes Agent latency: a self-inflicted skill, not the model or reasoning setting

Measured 2026-09-06. A fresh session asking a one-tool question
("how far away is the nearest obstacle") went from an early 8.69s/1-call
baseline to 40+s/9-10 calls of blind exploration (`tool_search`,
`search_files`, `read_file`, `list_resources`...) before finding the right
tool. Neither `agent.reasoning_effort` nor swapping the primary model
(`gpt-5.4-mini` -> `gpt-5.6-luna`) explained it -- both showed the identical
pattern once tested directly.

The actual cause: `auxiliary.background_review` -- Hermes' automatic
post-turn "should any skill/memory be saved?" reflection -- had written a
skill mid-session (`autonomous-ai-agents/bridge-backed-mcp-tools`, tagged
`sensors, camera, robot, session-token`) that pointed at a reference file
(`references/bridge-backed-mcp.md`) it never created. Every fresh session
touching `hal-robot`'s tools tried to read that promised guidance, failed,
and fell back to blind exploration to reconstruct it by hand.

Two things fixed it:

- Quarantined the broken skill (`~/.hermes/skills-quarantine/`, moved not
  deleted). Cut the cold-start turn from 40.80s/10 calls to 30.83s/6 calls.
- Confirmed the remaining cost is **one-time per session, not per-turn**: a
  second question in the SAME session went straight to the tool (7.62s,
  2 calls, 99% cache hit). This matters directly for crawl: `crawl_arm`,
  `crawl_observe` and `crawl_step` all share one session, so only the first
  tool-touching turn of an episode pays the cold-start cost.
- Disabled `auxiliary.background_review.enabled` outright, since it is what
  wrote the broken skill in the first place, silently, with no review step.
  `/refine` still works for an explicit, on-demand version of the same idea.

If a session ever goes back to unexplained multi-call exploration on a tool
it has used before, check `find ~/.hermes/skills -name SKILL.md -newermt
"<session start>"` for a skill background_review wrote mid-session before
concluding it is a model or config problem.

## Motion verified on the chassis, wheels lifted, 2026-09-06

First motion ever commanded from this repo. Scripts under `data/bench/`
(gitignored), run directly against the hardware layer rather than through the
bridge, because `read_spatial_sensors` discards the encoder angles and those
are the only objective evidence a wheel turned -- with the chassis lifted,
ultrasonic and yaw do not change on a straight drive.

**Motors turn, and the distance bound is real.**

```
 5 cm ->  86.5 deg encoder travel (1.58s)
20 cm -> 352.5 deg encoder travel (6.08s)
ratio 20/5 = 4.08   (4.0 expected)
```

Within 2% of proportional, with timing scaling too. The firmware honours the
commanded distance rather than running open-loop. Left and right encoders move
with opposite signs and near-equal magnitude (mirrored mounts, driving straight).

**A stop cannot interrupt a drive. This is now measured, not inferred.**

Firing `emergency_stop` on a second transport 2.5 s into a 6 s drive:

```
encoder travel : 354.0 deg     (uninterrupted 20 cm was 352.5)
drive thread   : Ch340UsbError: bulk_transfer(read) failed: No such device (-4)
mid-drive stop : CyberPiTimeoutError
```

The wheels ran the full commanded distance. The stop shortened it by nothing
measurable. `emergency_stop` with no drive in flight works fine, so the tool is
not broken -- it simply cannot reach a chassis whose channel is already busy.

**One documented detail is wrong for this platform.** The transferred code says
two connections fail with "Resource busy". That is pyserial on a desktop. On
the Android path `Ch340UsbTransport` wraps the same `TERMUX_USB_FD` integer, so
**the second open succeeds silently**, both conversations interleave on one
channel, and they corrupt each other. The in-flight drive loses its control
channel entirely (`No such device`) and completes blind.

The board survives this: USB stays enumerated, and telemetry reads normally
afterwards without a re-claim. But between the collision and the end of the
command there is no telemetry, no stop, and no way to intervene.

So the safety property is exactly what the code comments claim, and for a
sharper reason than "a stop might not arrive": **a bounded, self-terminating
command is the only guarantee, because a mid-drive stop is not merely
unreliable, it is unavailable, and attempting one destroys the channel you
would need to observe the outcome.** Never write a command whose completion
depends on being able to stop it. The 50 cm / 30% and 5 cm / 10% crawl bounds
are the safety envelope, not a tuning preference.

An earlier attempt fired the stop 0.4 s in and measured 0.0 deg of travel,
which looked like a successful stop and was not: the collision landed during
the drive's own setup conversation, so no motor ever started. Fire late enough
to be sure the wheels are turning before reading anything into the result.

**On the floor, commanded distance matches actual distance.** Wheels down,
driving at a wall and using the ultrasonic delta as ground truth:

```
ultrasonic 69.8 -> 49.2 cm   = 20.6 cm travelled for a 20 cm command  (+3%)
encoder travel 352.0 deg     (352.5 unloaded -- the load barely registers)
yaw drift +1.0 deg over 20 cm
```

`drive_straight(+20)` drives forward. Encoders read `+352 / -352` identically
in every run, lifted and loaded.

A trap worth knowing, because it cost a stop-and-check: the first floor run
showed the obstacle getting *further away* (111.7 -> 122.8 cm), which reads
exactly like driving in reverse. It was not. Yaw between the two runs went 44
to -33 degrees -- the chassis had been repositioned, and the beam slipped past
a near edge onto something further back as it advanced. The ultrasonic is a
cone, not a rangefinder: it is only ground truth against a flat surface square
on. Check yaw before trusting a distance delta, and take a second reading
before concluding the drive went backwards.

Not yet tested: a stop issued from the *same* transport and process as a
running drive (the bridge's real arrangement, where `run_motion` holds the
transport for the duration), and any of this with the wheels down and the
chassis loaded.

## The crawl, driven by the agent on the floor, 2026-09-06

**The first autonomous motion this repo has produced: 55 cm across a room in
15 cm camera-checked steps, at 10% speed, yaw drifting 2 degrees over the whole
run.** Before that the crawl had refused five consecutive episodes. Every
refusal was correct given what the agent was told, and none of them were a
sensor or safety failure — the agent was answering the wrong question.

**The bug was the caption, not the gate.** `crawl_observe` reused `look`'s
general prompt, which describes the room. A chair two and a half metres away
came back as "a chair centered ahead", and the tool docstring told the agent
that a description omitting a hazard is not evidence of safety — so it refused
a 15 cm step over open floor, with ultrasonic reporting 300 cm. The same frame,
under the two prompts:

```
general : A black wire grid barrier is positioned to the left. A chair and
          various storage containers are located directly ahead and to the right.
crawl   : The strip is clear floor. There are no obstacles in the path.
```

`CRAWL_VISION_PROMPT` asks a geometric question rather than a numeric one: this
camera sits at floor level, so the bottom of the frame *is* the near ground.
"Is the bottom third of the frame drivable" is something a captioning model can
answer; "how many centimetres away is that chair" is not.

The second refusal after that fix was a flat rug, correctly described and then
treated as an obstacle. Blocked has since been defined as what it actually
means — something the robot would hit, or an edge it would fall off. A floor
covering it can roll onto is drivable ground. The physical interlocks were not
touched: 15 cm segments, 10% speed, the 25 cm ultrasonic veto, and firmware
self-termination are all as they were.

**`HAL_AGENT_TIMEOUT` must exceed the crawl window.** The run above stopped at
55 cm of a 200 cm budget because the *turn* timed out at 180 s while the episode
had 300 s — one turn covers the arm prompt and the entire drive. Launch a robot
deployment with `HAL_AGENT_TIMEOUT=420`. Note what that failure leaves behind:
the episode stayed armed with 145 cm of budget and no agent driving it. Nothing
moved, because every segment needs an explicit `crawl_step`, but an armed
episode outliving the turn that was granted it is not the intent — disarm it
(`/internal/robot/crawl/disarm`) if you see one.

## What still has to be proven on the device

1. ~~No motion has been commanded from this repo.~~ Done — wheels lifted, then
   on the floor, then driven by the agent under crawl (all above). Still true
   and still load-bearing: enable `HAL_ROBOT_MOTION=1` only with a hand on the
   robot, because a stop cannot interrupt a drive once it starts. What remains
   untested is a stop issued from the *same* transport and process as a running
   drive, which is the bridge's real arrangement.
2. **Credentials.** `~/.hermes` exists on the phone but holds no `auth.json`,
   so the agent cannot answer anything yet. Run `hermes setup` on the device.
   Copying `auth.json` from another machine means sharing that credential with
   the phone — a decision for whoever owns it, not a deployment step. Verify
   Luna subscription access with an authenticated inference test before
   claiming that backend works here.
3. A full `HAL_TERMUX_LISTEN=1` conversation — wake phrase, turn, spoken
   sign-off — which needs (1).
4. Audible playback through `termux-media-player`. hal confirmed this on this
   handset; this repo's own path has not been listened to.
5. Battery: Android killed hal's Termux twice before a battery exemption was
   set, and that exemption was never verifiable from inside Termux
   (`dumpsys` is blocked). It only proves itself by surviving a long idle.
