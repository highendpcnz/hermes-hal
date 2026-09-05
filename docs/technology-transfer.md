# Independent Hermes Hal runtime

Technology transfer is one-way: hal → hermes-hal. Copies become owned here;
there are no cross-repository imports, symlinks, shared environments or automatic
synchronization. Do not modify hal to implement a Hermes Hal feature.

## First increment: lightweight interface

The launcher selects HAL_UI=lite. /lite always opens the lightweight page;
/bridge preserves access to the graphical interface. Direct uvicorn launches
retain the previous default unless HAL_UI=lite is set explicitly.

The lightweight page has no framework, WebGL, remote fonts or animation loop.
It reuses Hermes Hal's existing text, recorded-audio and permission endpoints.
Stop playback affects browser audio only; it does not cancel agent work or stop
robot motors. No robot controls are exposed in this increment.

This reduces browser rendering work, not backend model memory. The Python voice
dependencies still need Android adaptation and hardware validation. Microphone
capture requires localhost or HTTPS. Open localhost on the Pixel itself.

## Second increment: ASR/TTS learnings

Copied from `hal` at commit `1318bd1e486ac08086e543b74c90ad1d3360a654` and now
owned here. These were adopted because each one encodes a *measurement* made on
the target Pixel, not a preference — re-deriving them would mean repeating the
same hardware failures.

| Source (hal) | Destination | Adaptation |
| --- | --- | --- |
| `termux_whisper_cpp.py` | `termux_whisper_cpp.py` | Docstring rewritten for this repo; references to `docs/termux-port-status.md` and `robot/camera.py` dropped. No logic change. |
| `brain/farewell.py` | `farewell.py` | Flattened out of the `brain` package this repo does not have; `stopwords.py` comparison dropped, the inverted error budget it explained kept and restated. |
| `termux_voice.py` | `termux_voice.py` | `from brain import farewell` → `import farewell`. The motion-safety section was rewritten: this deployment exposes no robot controls, so the sequential loop currently costs only responsiveness — with an explicit instruction not to add motion to the loop as it stands. |
| `main.py` STT backend selection | `main.py` `_load_stt()` | Copied as-is: whisper.cpp tried first, auto-detected by binary presence. |
| `main.py` STT degradation | `main.py` boot + `transcribe()` | Copied: a failed STT load disables audio rather than taking the app down; `transcribe()` returns `""`, `_stt_device()` reports `n/a`. |
| `main.py` `_WAKE_RE` | `main.py` `_WAKE_RE` | Copied, replacing this repo's own gate. See below. |
| `ORT_DISABLE_TELEMETRY` default | `main.py` | Copied. Set before Piper/faster-whisper import ONNX. |

### What each one is actually worth

- **whisper.cpp backend.** `faster_whisper`/`ctranslate2` import cleanly on
  Termux and can never load a model — `ctranslate2.models` is empty in the
  packaged build. `sherpa-onnx` does not build there at all. whisper.cpp builds
  with no source patches. Without this, the Pixel has no ASR.
- **Degrade, don't die.** A platform with no working STT engine should still
  serve every text surface and the permission flow. Previously a failed load
  took the whole app down at import time.
- **Silence gate before decode.** Whisper invents fluent speech from
  near-silence — a genuinely silent room produced *"That's why you didn't harm
  me, look. You should work there."* A hallucination can contain a wake word,
  so an unmetered loop wakes *itself*. Clips below −45 dBFS peak never reach
  whisper.
- **Attention word required.** A bare name cannot be made safe against a
  television: a film playing in the room produced `"HAL, the real value of a
  conflict, the true value, is in the dead."` — a textbook address, and HAL
  answered it. Two ordered tokens are vanishingly rare in ambient speech.
  This *replaced* a gate in this repo that had been widened to accept bare
  `how`/`hull`/`howl`, which woke on "How are you doing?" and "Hull integrity
  is fine". `HAL_WAKE_REQUIRE_ATTENTION=0` takes the bare name back, along with
  the televisions. The cost is real: "Open the pod bay doors, HAL" no longer
  wakes anything.
- **Timing constants that look arbitrary and are not.**
  `termux-microphone-record` returns when capture *starts*, so
  `RECORD_FINALISE_SECONDS` is what stops a truncated clip being decoded;
  `termux-media-player play` returns when playback *starts*, so
  `PLAYBACK_SETTLE_SECONDS` plus the WAV's real duration is what stops the loop
  hearing its own voice.
- **Deterministic farewell.** The loop runs one fixed session id, so without a
  spoken sign-off a conversation never ends and tomorrow carries tonight's
  context. It must not go through the model, so it keeps working offline.

This repo's `_normalize_hal_name` is kept and is *not* from hal. It repairs the
looser measured homophones in address position before the gate sees them, which
is what lets the gate's own name list stay at `hal|hall|hell`.

Not adopted: `brain/stopwords.py`, the emergency-stop precedence in
`_stop_reply`, and everything under `robot/`. They belong with robot controls,
and must be adopted *together with* them and their tests — not before.

## Third increment: the Termux build recipe

Copied from hal at the same commit `1318bd1e486ac08086e543b74c90ad1d3360a654`.

| Source (hal) | Destination | Adaptation |
| --- | --- | --- |
| `docs/termux-port-status.md` | `docs/pixel-deployment.md` | Reordered around the four failure modes rather than as a session log. Gemma/pyserial/robot steps dropped; `agent-client-protocol` added; the untested-on-this-repo status stated up front. |
| the same, as steps | `bin/termux-setup` | Newly written here — hal has the recipe only as prose. Idempotent, checks for `espeakbridge*.so` and `whisper-cli` rather than rebuilding blindly. |

This is a record of dead ends, which is why it was worth copying: `piper-tts`
from PyPI silently builds a wheel with no C++ in it, `faster-whisper` loads no
model on Termux, `--no-build-isolation` fails unless the venv is *activated*,
and pip will otherwise compile CMake itself from source. Each was hours.

`bin/termux-setup` ran clean on the Pixel on 2026-09-06, and the voice stack it
builds was verified end to end there — see `pixel-deployment.md` for the
measurements and for the two bugs that first run found.

## Transfer ledger


First increment: no source code copied; hal's USB bring-up record consulted at
commit `1318bd1e486ac08086e543b74c90ad1d3360a654`.

Second increment: the ASR/TTS table above.

Third increment: the Termux build recipe above. Remaining candidates:

- Android USB transport and the associated hardware tests.
- Robot command boundaries and safeguards, adopted together with their tests.

Each future copy must list source path, source commit, destination, adaptations,
and verification. Keep Hermes Agent sessions, credentials, working directory and
data separate from the other HAL deployment. Verify Luna subscription access with
an authenticated inference test before claiming that backend works on the Pixel.
