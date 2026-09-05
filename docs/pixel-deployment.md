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
works in that mode; entering genuine online mode is what motor control needs.

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

## What still has to be proven on the device

1. **No motion has been commanded from this repo.** Sensors are verified
   against the real chassis (above); the motor path is not. Enable
   `HAL_ROBOT_MOTION=1` only with a hand on the robot, and remember that a
   stop cannot interrupt a drive once it starts.
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
