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

## What still has to be proven on the device

None of this repo has run on the phone. In rough order:

1. `bin/termux-setup` completes — it has never been executed, only derived.
2. `HAL_SKIP_MODELS=1 python tests/run.py` passes there.
3. Piper synthesizes audibly through `termux-media-player play`.
4. `whisper-cli` decodes a clip recorded by `termux-microphone-record`.
5. `HAL_TERMUX_LISTEN=1` — the wake phrase, a real turn, and the sign-off.
6. **Hermes Agent itself runs on Termux.** This is the largest untested
   assumption in this document: hal's port proved the *voice* stack on Android
   against a local Gemma brain, and says nothing about this repo's agent
   backend. Verify Luna subscription access with an authenticated inference
   test before claiming that path works.
7. Battery: Android killed hal's Termux twice before a battery exemption was
   set, and that exemption was never verifiable from inside Termux
   (`dumpsys` is blocked). It only proves itself by surviving a long idle.

Steps 1–5 are the ones this transfer was for. Step 6 is independent of it and
could be tested first, on its own.
