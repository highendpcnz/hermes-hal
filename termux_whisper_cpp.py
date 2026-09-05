"""whisper.cpp-backed STT — the Termux/Pixel replacement for faster-whisper.

Adapted from hal's `termux_whisper_cpp.py` at commit 1318bd1 (see
docs/technology-transfer.md). Owned here; hal is not consulted again.

`faster_whisper`/`ctranslate2` install and import cleanly on Termux but are
fundamentally broken there: `ctranslate2.models` is completely empty in the
packaged build (`dir(ctranslate2.models) == []`), so no Whisper model can
ever actually load. `sherpa-onnx` doesn't install at all — its own CMake
explicitly rejects any OS that isn't Linux/macOS/Windows by name.

whisper.cpp has neither problem: it builds clean on the target device with
zero source patches (same ggml foundation as llama.cpp), and its
`whisper-cli` binary is a plain file-in/text-out CLI tool.

This module mimics just enough of `faster_whisper.WhisperModel`'s interface
— `.transcribe(audio, language=, vad_filter=, initial_prompt=, beam_size=)
-> (segments, info)`, plus a `.model.device` attribute — that main.py's
`_load_stt()`/`transcribe()`/`_stt_device()` need no changes at all to use
this instead; only `_load_stt()`'s one-time backend *selection* changes.

`vad_filter` is accepted for interface compatibility but not actually
implemented: whisper.cpp's own `--vad` needs a separate VAD model this
integration does not download. Whisper's own no-speech-threshold decoding
already handles most silence reasonably; real VAD would need its own model
and is a possible future addition, not silently pretended to exist today.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import tempfile
import wave
from typing import BinaryIO


class WhisperCppError(RuntimeError):
    """Raised when whisper-cli cannot produce a transcript."""


@dataclass(frozen=True, slots=True)
class _Segment:
    text: str


@dataclass(frozen=True, slots=True)
class _TranscriptionInfo:
    language: str


class _DeviceInfo:
    # whisper.cpp was built here with no GPU backend (confirmed at build
    # time — see docs/termux-port-status.md); always CPU, never resolved.
    device = "cpu"


class WhisperCppModel:
    """Drop-in-enough replacement for `faster_whisper.WhisperModel`, backed
    by a `whisper-cli` binary and a ggml model file."""

    def __init__(
        self,
        model_path: str,
        *,
        binary_path: str = "whisper-cli",
        ffmpeg_bin: str = "ffmpeg",
        threads: int = 4,
        timeout: float = 60.0,
    ) -> None:
        if not Path(model_path).is_file():
            raise WhisperCppError(f"whisper.cpp model not found: {model_path}")
        self.model_path = model_path
        self.binary_path = binary_path
        self.ffmpeg_bin = ffmpeg_bin
        self.threads = threads
        self.timeout = timeout
        self.model = _DeviceInfo()

    def transcribe(
        self,
        audio: object,
        *,
        language: str = "en",
        vad_filter: bool = False,
        initial_prompt: str | None = None,
        beam_size: int = 5,
    ) -> tuple[list[_Segment], _TranscriptionInfo]:
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = Path(tmpdir) / "audio.wav"
            _write_wav(audio, wav_path, ffmpeg_bin=self.ffmpeg_bin, timeout=self.timeout)
            text = self._run_cli(
                wav_path, language=language, initial_prompt=initial_prompt, beam_size=beam_size
            )
        segments = [_Segment(text=text)] if text else []
        return segments, _TranscriptionInfo(language=language)

    def _run_cli(
        self,
        wav_path: Path,
        *,
        language: str,
        initial_prompt: str | None,
        beam_size: int,
    ) -> str:
        command = [
            self.binary_path,
            "-m",
            self.model_path,
            "-f",
            str(wav_path),
            "-np",
            "-nt",
            "-l",
            language,
            "-t",
            str(self.threads),
            "-bs",
            str(max(1, beam_size)),
        ]
        if initial_prompt:
            command += ["--prompt", initial_prompt]
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=self.timeout, check=False
            )
        except FileNotFoundError as error:
            raise WhisperCppError(f"whisper-cli not found: {self.binary_path}") from error
        except subprocess.TimeoutExpired as error:
            raise WhisperCppError("whisper-cli timed out") from error
        if completed.returncode != 0:
            detail = completed.stderr.strip()[-300:]
            raise WhisperCppError(f"whisper-cli failed: {detail or 'no output'}")
        return completed.stdout.strip()


def _write_wav(audio: object, path: Path, *, ffmpeg_bin: str, timeout: float) -> None:
    """Accepts anything faster_whisper's own `.transcribe()` accepts that
    this project actually passes: a file-like object or raw bytes of
    uploaded audio (real /api/talk audio — the browser's `MediaRecorder`
    uploads `audio/webm`/Opus by default, confirmed in static/lite.js and
    static/index.html, not WAV), or a raw float32 numpy array of samples at 16kHz (only
    main.py's `_load_stt()` startup self-test uses this shape).

    `whisper-cli`'s own decoder only understands flac/mp3/ogg/wav (per
    `whisper-cli --help`) — nothing containerized like webm/Opus — so
    anything that isn't already the numpy-array self-test path is
    normalized through ffmpeg first, ffmpeg is already a dependency of this app on both platforms."""

    if isinstance(audio, BinaryIO) or hasattr(audio, "read"):
        raw_bytes = audio.read()
    elif isinstance(audio, (bytes, bytearray)):
        raw_bytes = bytes(audio)
    else:
        import numpy as np

        samples = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
        pcm16 = (samples * 32767.0).astype("<i2")
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(pcm16.tobytes())
        return

    with tempfile.NamedTemporaryFile(suffix=".input", delete=False) as handle:
        handle.write(raw_bytes)
        input_path = Path(handle.name)
    try:
        command = [
            ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-ar",
            "16000",
            "-ac",
            "1",
            str(path),
        ]
        try:
            completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError as error:
            raise WhisperCppError(f"ffmpeg not found: {ffmpeg_bin}") from error
        except subprocess.TimeoutExpired as error:
            raise WhisperCppError("ffmpeg audio normalization timed out") from error
        if completed.returncode != 0 or not path.exists():
            detail = completed.stderr.decode("utf-8", "replace").strip()[-300:]
            raise WhisperCppError(f"ffmpeg audio normalization failed: {detail or 'no output'}")
    finally:
        input_path.unlink(missing_ok=True)
