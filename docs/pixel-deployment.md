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

## What still has to be proven on the device

1. **Hermes Agent itself does not exist on this phone** — no binary, no
   `~/.hermes`. Everything above is the voice stack answering with no brain
   behind it. This is now the single largest open item, and it is independent
   of every transfer: it could have been tested first. Verify Luna subscription
   access with an authenticated inference test before claiming that path works.
2. A full `HAL_TERMUX_LISTEN=1` conversation — wake phrase, turn, spoken
   sign-off — which needs (1) first.
3. Audible playback through `termux-media-player`. hal confirmed this on this
   handset; this repo's own path has not been listened to.
4. Battery: Android killed hal's Termux twice before a battery exemption was
   set, and that exemption was never verifiable from inside Termux
   (`dumpsys` is blocked). It only proves itself by surviving a long idle.
