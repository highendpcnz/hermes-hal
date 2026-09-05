"""On-device listen/speak loop for the Termux/Pixel deployment.

Adapted from hal's `termux_voice.py` at commit 1318bd1 (see
docs/technology-transfer.md). Owned here; hal is not consulted again.

The browser paths (`/lite`, `/bridge`) capture audio client-side and upload it
for main.py's `transcribe()` to decode. That needs a browser, a microphone
permission prompt and a screen. This loop needs none of them: it repeatedly
listens with the phone's own mic, answers through the same `run_turn()`
pipeline `/api/say` already uses, and plays the reply through the phone's own
speaker — entirely apart from the browser-audio endpoints, which keep working
unchanged.

Enabled only when HAL_TERMUX_LISTEN is truthy (see main.py's lifespan).

**This loop is sequential, and that is worth stating plainly.** One iteration
records a bounded clip, gates it on level, transcribes, runs the whole turn,
plays the reply, and only then listens again. Nothing is recording during
`run_turn`. Today that costs only responsiveness — this deployment exposes no
robot controls, so there is no motion an unheard word would need to
interrupt. If robotics is ever adopted here, this is the first thing that must
change: a recorder running concurrently with the turn whose transcripts feed a
deterministic stop matcher, or a hardware stop button, which is the honest
answer for a machine that moves. Do not add motion to this loop as it stands.

Gated by a wake word (HAL_WAKE_WORD, default "hal") — this loop otherwise
answers *anything* it hears near the phone with no addressing at all, which
during hal's bring-up genuinely picked up unrelated real conversation nearby
and answered it. Set HAL_WAKE_WORD="" to disable the gate and go back to that
always-answering behavior.

Two STT backends, selected by HAL_TERMUX_STT_BACKEND:

  "whispercpp" (default) — record a bounded clip with `termux-microphone-record`,
      hand the bytes to the caller-supplied transcribe(), which is main.py's
      whisper.cpp-backed one. Chosen as the default because the "android"
      backend below is hardware-confirmed broken on the target phone.
  "android" — the original `termux-speech-to-text` path. On the target Pixel
      (Android SDK 37) that command hangs forever and returns nothing at all:
      confirmed 2026-09-05 with no competing processes, run foregrounded,
      screen unlocked, mic permission granted to both Termux:API and the
      Google app, assistant set, and working internet. Raw
      `termux-microphone-record` captured fine throughout, so the microphone
      is not the problem — Android's recognizer service is. Kept because it
      needs no whisper build and may work on other devices/versions.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
import subprocess
import tempfile
import wave
from typing import Awaitable, Callable, Optional

import farewell

SESSION_ID = "termux-onboard"
LISTEN_TIMEOUT_SECONDS = 30.0
# termux-media-player's `play` returns as soon as playback *starts*, not when
# it ends — without a wait matched to the clip's real length, the loop would
# start listening again while HAL is still talking and hear its own voice.
PLAYBACK_SETTLE_SECONDS = 0.4
WAKE_WORD = os.environ.get("HAL_WAKE_WORD", "hal").strip()

STT_BACKEND = os.environ.get("HAL_TERMUX_STT_BACKEND", "whispercpp").strip().lower()
# One bounded capture per loop iteration. `termux-microphone-record` needs an
# explicit length; there is no streaming/VAD source to end a clip on silence.
CLIP_SECONDS = float(os.environ.get("HAL_TERMUX_CLIP_SECONDS", "6"))
# The recorder returns as soon as capture *starts* (it backgrounds an Android
# MediaRecorder), and the file is not complete the instant the duration
# elapses — read too early and you transcribe a truncated clip.
RECORD_FINALISE_SECONDS = 1.5
# whisper invents fluent speech from near-silence: a genuinely silent room
# produced "That's why you didn't harm me, look. You should work there."
# (measured 2026-09-04). A hallucination can contain a wake word, so an
# unmetered loop would wake itself on silence. Clips whose peak is below this
# never reach whisper at all. Raising HAL_STT_PROMPT's specificity makes this
# gate more important, not less — the bias prompt increases hallucination on
# quiet input.
SILENCE_PEAK_DBFS = float(os.environ.get("HAL_TERMUX_SILENCE_DBFS", "-45"))

# How base.en actually renders the spoken name "HAL" — measured on this
# hardware, not guessed. "HAL, open the pod bay doors, HAL... hey HAL" came
# back as "How? Open the pod bay doors, huh? Hey, how?" (2026-09-05).
# `main.py`'s browser-side gate accepts hal/hall/hell and explicitly rejects
# "how" because it "would wake on ordinary ambient questions" — which is
# correct for a match-anywhere rule. These looser variants are therefore only
# honoured in an *addressing position*: leading or trailing, and followed by
# punctuation. "How? Open the doors" and "Hey, how?" wake; "How are you" and
# "I don't know how" do not.
_WAKE_STRONG_VARIANTS = ("hall",)
_WAKE_ADDRESS_VARIANTS = ("hell", "howl", "how", "huh")

# Require an attention word ("hey HAL") rather than the bare name.
#
# A bare name cannot be made safe against a television. Confirmed live
# 2026-09-05: a film playing in the room produced the transcript "HAL, the
# real value of a conflict, the true value, is in the dead." — a textbook
# address, at the start of the utterance, indistinguishable from a real one
# by any text rule. HAL answered it. Given enough hours of dialogue, some
# line will always transcribe as the name.
#
# "hey <name>" is what every commercial assistant requires, for exactly this
# reason: two ordered tokens are vanishingly rare in ambient speech, while a
# common single word is not. It also *relaxes* the homophone problem — "hey
# how" is unmistakably an address, so the variants no longer need the
# positional restrictions a bare name did, which improves recall and
# precision at once.
#
# The cost is real and worth stating: "Open the pod bay doors, HAL" no longer
# wakes anything. Set HAL_WAKE_REQUIRE_ATTENTION=0 to take the bare name back,
# along with the televisions that come with it.
WAKE_REQUIRE_ATTENTION = os.environ.get(
    "HAL_WAKE_REQUIRE_ATTENTION", "1"
).strip().lower() not in {"0", "false", "no"}
_ATTENTION_WORDS = ("hey", "hi", "ok", "okay", "hello")


def _address_variant_pattern() -> str:
    return "|".join(re.escape(variant) for variant in _WAKE_ADDRESS_VARIANTS)


def _attention_pattern(wake_word: str) -> str:
    attention = "|".join(_ATTENTION_WORDS)
    names = [re.escape(wake_word)]
    if wake_word.lower() == "hal":
        names += [re.escape(v) for v in _WAKE_STRONG_VARIANTS + _WAKE_ADDRESS_VARIANTS]
    return rf"\b(?:{attention})\b[\s,]*\b(?:{'|'.join(names)})\b"


def _heard_wake_word(text: str, wake_word: str = WAKE_WORD) -> bool:
    """Whole-word, case-insensitive match — "hal" must not match inside
    "halt" or "shall". An empty wake_word disables the gate entirely (every
    utterance passes), matching how other HAL_* flags in this project treat
    an empty string as "off".

    When the wake word is the default "hal", also recover the homophones
    whisper actually produces for it (see the variant tables above). Any
    other configured wake word is matched literally only — the variants are
    specific to how "HAL" is misheard and mean nothing for another name."""

    if not wake_word:
        return True

    if WAKE_REQUIRE_ATTENTION:
        # "hey HAL" and its homophones, nothing else. No positional rules are
        # needed: the attention word is what makes it an address.
        return re.search(_attention_pattern(wake_word), text, re.IGNORECASE) is not None

    if re.search(rf"\b{re.escape(wake_word)}\b", text, re.IGNORECASE) is not None:
        return True
    if wake_word.lower() != "hal":
        return False

    for variant in _WAKE_STRONG_VARIANTS:
        if re.search(rf"\b{re.escape(variant)}\b", text, re.IGNORECASE):
            return True

    loose = _address_variant_pattern()
    # Leading address: "How? Open the pod bay doors" / "Hey, how, drive forward".
    # The trailing punctuation is what separates an address from a question
    # that merely opens with the same word ("How are you?").
    if re.search(rf"^\s*(?:hey|ok|okay)?[,\s]*(?:{loose})\s*[,.!?:]", text, re.IGNORECASE):
        return True
    # Trailing address: "..., huh?" / "Hey, how?". The comma (or a "hey")
    # is doing the work — "I don't know how" has neither and must not wake.
    if re.search(
        rf"(?:^|,)\s*(?:hey\s*,?\s*)?(?:{loose})\s*[,.!?:]*\s*$", text, re.IGNORECASE
    ):
        return True
    return False


async def listen_once(timeout: float = LISTEN_TIMEOUT_SECONDS) -> str:
    """One termux-speech-to-text call. Returns '' on silence/timeout/error —
    never raises, since a bad recognition should not kill the loop."""

    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            ["termux-speech-to-text"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError) as error:
        print(f"[termux-voice] listen failed: {error!r}")
        return ""
    if completed.returncode != 0:
        print(f"[termux-voice] termux-speech-to-text exited {completed.returncode}")
        return ""
    return completed.stdout.strip()


def _peak_dbfs(path: Path, ffmpeg_bin: str = "ffmpeg") -> float:
    """Peak level of a recorded clip, via ffmpeg's volumedetect. Returns
    -inf-ish (-999.0) when the level cannot be read, so an unreadable clip
    is treated as silence and skipped rather than sent to whisper."""

    try:
        completed = subprocess.run(
            [ffmpeg_bin, "-hide_banner", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"[termux-voice] level probe failed: {error!r}")
        return -999.0
    match = re.search(r"max_volume:\s*(-?[\d.]+) dB", completed.stderr)
    return float(match.group(1)) if match else -999.0


def record_clip(
    path: Path,
    seconds: float = CLIP_SECONDS,
    recorder_bin: str = "termux-microphone-record",
) -> bool:
    """Capture one bounded clip from the phone mic. True when a non-empty
    file was written.

    `-l <seconds>` makes Android's MediaRecorder stop itself, but the command
    returns immediately either way, so the wait here is what actually bounds
    the clip. A stray recording from a previous, interrupted iteration would
    make this call fail, so stop one defensively first (`-q` on an idle
    recorder is a harmless "No recording to stop")."""

    subprocess.run([recorder_bin, "-q"], capture_output=True, timeout=20, check=False)
    try:
        started = subprocess.run(
            [recorder_bin, "-f", str(path), "-e", "aac", "-r", "16000", "-c", "1",
             "-l", str(int(seconds))],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"[termux-voice] recorder failed to start: {error!r}")
        return False
    if started.returncode != 0:
        print(f"[termux-voice] recorder error: {started.stderr.strip() or started.stdout.strip()}")
        return False
    return True


async def listen_once_whisper(
    transcribe: Callable[[bytes], str],
    seconds: float = CLIP_SECONDS,
) -> str:
    """Record one clip and transcribe it with the caller's whisper-backed
    transcribe(). Returns '' on silence, capture failure, or a decode error —
    never raises, matching listen_once() above."""

    with tempfile.TemporaryDirectory() as tmpdir:
        clip = Path(tmpdir) / "clip.m4a"
        if not await asyncio.to_thread(record_clip, clip, seconds):
            return ""
        await asyncio.sleep(seconds + RECORD_FINALISE_SECONDS)
        if not clip.exists() or clip.stat().st_size == 0:
            print("[termux-voice] recorder produced no audio")
            return ""

        peak = await asyncio.to_thread(_peak_dbfs, clip)
        if peak < SILENCE_PEAK_DBFS:
            print(f"[termux-voice] silence ({peak:.1f} dBFS < {SILENCE_PEAK_DBFS}) — not decoding")
            return ""

        audio_bytes = clip.read_bytes()

    try:
        # transcribe() takes the encoded bytes as-is; its whisper.cpp wrapper
        # normalises them through ffmpeg, so no second conversion here.
        return (await asyncio.to_thread(transcribe, audio_bytes)).strip()
    except Exception as error:  # noqa: BLE001 - a bad decode must not kill the loop
        print(f"[termux-voice] transcribe failed: {error!r}")
        return ""


def _wav_duration_seconds(wav_bytes: bytes) -> float:
    import io

    with wave.open(io.BytesIO(wav_bytes)) as wav_file:
        frames = wav_file.getnframes()
        rate = wav_file.getframerate()
    return frames / rate if rate else 0.0


async def speak(wav_bytes: bytes) -> None:
    """Play synthesized speech through the phone's speaker and block until
    it should be done, so the loop does not immediately re-listen over it."""

    duration = _wav_duration_seconds(wav_bytes)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        handle.write(wav_bytes)
        path = Path(handle.name)
    try:
        result = await asyncio.to_thread(
            subprocess.run,
            ["termux-media-player", "play", str(path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"[termux-voice] playback failed: {result.stderr.strip()}")
            return
        await asyncio.sleep(duration + PLAYBACK_SETTLE_SECONDS)
    finally:
        path.unlink(missing_ok=True)


RunTurn = Callable[..., Awaitable[tuple[str, bytes, dict]]]


async def listen_loop(
    run_turn: RunTurn,
    transcribe: Optional[Callable[[bytes], str]] = None,
    clear_session: Optional[Callable[[str], None]] = None,
) -> None:
    """Forever: listen with the phone's mic, answer via run_turn(), speak
    the reply. Runs as a background task started from main.py's lifespan;
    a stray exception from one turn must not end the loop.

    `transcribe` is main.py's whisper.cpp-backed transcriber. It is injected
    rather than imported so this module stays free of a main.py import cycle,
    the same way `run_turn` already is. Without it the whisper backend cannot
    run and the loop falls back to the Android recognizer."""

    use_whisper = STT_BACKEND == "whispercpp" and transcribe is not None
    if STT_BACKEND == "whispercpp" and transcribe is None:
        print("[termux-voice] whispercpp backend requested but no transcribe() given — "
              "falling back to the Android recognizer")

    print("[termux-voice] on-device listen loop starting")
    print(f"[termux-voice] stt backend: {'whisper.cpp' if use_whisper else 'android'}")
    if use_whisper:
        print(f"[termux-voice] clip {CLIP_SECONDS}s, silence gate {SILENCE_PEAK_DBFS} dBFS")
    if WAKE_WORD and WAKE_REQUIRE_ATTENTION:
        print(f"[termux-voice] wake phrase required: 'hey {WAKE_WORD}' "
              f"(attention word: {'/'.join(_ATTENTION_WORDS)})")
    elif WAKE_WORD:
        print(f"[termux-voice] wake word required: {WAKE_WORD!r} (bare name — "
              "will false-wake on televised dialogue)")
    else:
        print("[termux-voice] no wake word set — answering everything heard nearby")
    while True:
        try:
            if use_whisper:
                user_text = await listen_once_whisper(transcribe)
            else:
                user_text = await listen_once()
            if not user_text:
                continue
            if not _heard_wake_word(user_text):
                print(f"[termux-voice] ignored (no wake word): {user_text!r}")
                continue
            print(f"[termux-voice] heard: {user_text!r}")
            # Checked before the turn, acted on after it: the sign-off still
            # goes to the model, so HAL answers it in his own voice rather
            # than with a canned line, and only then is the session dropped.
            # Clearing first would delete the history the reply is drawn from.
            ending = clear_session is not None and farewell.is_farewell(user_text)
            hal_text, wav, _timings = await run_turn(SESSION_ID, user_text)
            print(f"[termux-voice] replying: {hal_text!r}")
            await speak(wav)
            if ending:
                clear_session(SESSION_ID)
                print("[termux-voice] farewell — session cleared, next wake starts fresh")
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - a bad turn must not kill the loop
            print(f"[termux-voice] loop error: {error!r}")
            await asyncio.sleep(1.0)
