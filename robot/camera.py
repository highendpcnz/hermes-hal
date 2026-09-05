"""Single-frame camera capture: the dev Mac's webcam, or the Pixel's own camera.

`capture_frame` (ffmpeg + avfoundation) was the original stand-in used to build
and test the vision tool loop before the real Android camera integration existed.
`capture_frame_termux` (Termux:API + ffmpeg) is that real integration: it shells
out to `termux-camera-photo`, hardware-verified against the Pixel 7 Pro's actual
back camera.
`capture_frame_app_process` is the wide-angle path. `termux-camera-photo` can
only address logical cameras (0 = back, 1 = front) and always shoots at 1x,
which pins it to the main lens; the ultra-wide is only reachable by asking
camera2 for a zoom ratio below 1.0, and camera2 needs a real Android runtime.
See docs/pixel-wide-angle.md.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile


class CameraCaptureError(RuntimeError):
    """Raised when a still frame cannot be captured."""


def capture_frame(
    *,
    device: str = "0",
    width: int = 640,
    height: int = 480,
    timeout: float = 5.0,
    ffmpeg_bin: str = "ffmpeg",
) -> tuple[bytes, int, int]:
    """Grab exactly one JPEG frame from an avfoundation video device.

    Returns ``(jpeg_bytes, width, height)``. Raises `CameraCaptureError` on any
    failure — missing binary, no camera, denied permission, or timeout — so
    callers can surface a clean tool-result error instead of crashing the turn.
    """
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "avfoundation",
        "-video_size",
        f"{width}x{height}",
        "-framerate",
        "30",
        "-i",
        f"{device}:none",
        "-frames:v",
        "1",
        "-q:v",
        "3",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ]
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise CameraCaptureError(f"ffmpeg not found: {ffmpeg_bin}") from error
    except subprocess.TimeoutExpired as error:
        raise CameraCaptureError("camera capture timed out") from error
    if completed.returncode != 0 or not completed.stdout:
        detail = completed.stderr.decode("utf-8", "replace").strip()[-300:]
        raise CameraCaptureError(f"ffmpeg capture failed: {detail or 'no output'}")
    return completed.stdout, width, height


def _rotate_and_scale(
    raw_path: Path,
    *,
    width: int,
    height: int,
    rotate: int,
    timeout: float,
    ffmpeg_bin: str,
) -> bytes:
    """Correct the chassis camera mount and shrink a raw capture, via ffmpeg.

    Shared by both Pixel backends: each one writes a full-size sensor JPEG to
    `raw_path`, and both need the same `transpose` + `scale` treatment before
    the bytes are worth base64-ing into a vision request.
    """
    if rotate not in (0, 1, 2, 3):
        raise CameraCaptureError(f"invalid rotate value: {rotate} (must be 0-3)")
    video_filter = f"scale={width}:{height}"
    if rotate:
        video_filter = f"transpose={rotate},{video_filter}"
    command = [
        ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(raw_path),
        "-vf",
        video_filter,
        "-q:v",
        "3",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "-",
    ]
    try:
        resized = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as error:
        raise CameraCaptureError(f"ffmpeg not found: {ffmpeg_bin}") from error
    except subprocess.TimeoutExpired as error:
        raise CameraCaptureError("camera resize timed out") from error
    if resized.returncode != 0 or not resized.stdout:
        detail = resized.stderr.decode("utf-8", "replace").strip()[-300:]
        raise CameraCaptureError(f"ffmpeg resize failed: {detail or 'no output'}")
    return resized.stdout


def capture_frame_termux(
    *,
    camera_id: str = "0",
    width: int = 640,
    height: int = 480,
    timeout: float = 8.0,
    termux_camera_bin: str = "termux-camera-photo",
    ffmpeg_bin: str = "ffmpeg",
    rotate: int = 2,
) -> tuple[bytes, int, int]:
    """Grab one JPEG frame from the Pixel's own camera via Termux:API.

    Returns ``(jpeg_bytes, width, height)``, matching `capture_frame` above.
    `termux-camera-photo -c <id> <file>` (camera 0 = back, 1 = front,
    confirmed via `termux-camera-info`) takes no resolution argument at all —
    it always captures at the sensor's maximum size, 4080x3072 / ~2.2MB on
    the Pixel 7 Pro's back camera, confirmed live. That's both wasteful to
    base64 and slow for the vision model to process, so the raw capture is
    piped through `ffmpeg` (already a dependency for `capture_frame` above,
    already installed on Termux — see docs/termux-port-status.md) to resize
    and recompress it down to `width`x`height`, hardware-verified to shrink
    a real capture from ~2.2MB to ~3KB. Raises `CameraCaptureError` on any
    failure — missing binary, no camera permission, or timeout — so callers
    can surface a clean tool-result error instead of crashing the turn.

    `termux-camera-photo` writes no EXIF orientation tag at all (confirmed
    via `ffprobe` on a live capture — the tag is simply absent, not just
    unread), so the raw buffer comes out in the sensor's native landscape
    layout regardless of how the phone is physically mounted on the chassis.
    `rotate` is an `ffmpeg` `transpose` value (0-3) applied before the
    resize/scale step to correct this. `2` (90° counter-clockwise) is
    confirmed live 2026-09-04 as correct for this chassis's actual camera
    mount — verified by capturing the same raw frame through all four
    transpose values and checking which one puts the floor at the bottom
    with objects resting on it under gravity, not the ceiling. Re-verify and
    override via `HAL_CAMERA_ROTATE` if the phone is ever remounted.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = Path(tmpdir) / "raw.jpg"
        capture_command = [termux_camera_bin, "-c", camera_id, str(raw_path)]
        try:
            captured = subprocess.run(capture_command, capture_output=True, timeout=timeout, check=False)
        except FileNotFoundError as error:
            raise CameraCaptureError(f"termux-camera-photo not found: {termux_camera_bin}") from error
        except subprocess.TimeoutExpired as error:
            raise CameraCaptureError("camera capture timed out") from error
        if captured.returncode != 0:
            detail = captured.stderr.decode("utf-8", "replace").strip()[-300:]
            raise CameraCaptureError(f"termux-camera-photo capture failed: {detail or 'no output'}")
        try:
            raw_size = raw_path.stat().st_size
        except OSError as error:
            raise CameraCaptureError("termux-camera-photo did not write an output file") from error
        if raw_size == 0:
            raise CameraCaptureError("termux-camera-photo produced an empty file")

        image_bytes = _rotate_and_scale(
            raw_path,
            width=width,
            height=height,
            rotate=rotate,
            timeout=timeout,
            ffmpeg_bin=ffmpeg_bin,
        )
    return image_bytes, width, height


DEFAULT_APP_PROCESS = "/system/bin/app_process64"
# app_process will happily start without a usable boot image, but it then falls
# back to interpreting the whole framework and takes minutes per run instead of
# ~1.3s. The live image is the one ART regenerates under apexdata; the copy in
# /system/framework is stale whenever the ART APEX has been updated.
DEFAULT_BOOT_IMAGE = "/data/misc/apexdata/com.android.art/dalvik-cache/boot.art"
DEFAULT_CAPTURE_JAR = str(Path(__file__).with_name("halcapture.jar"))


def _boot_image_present(boot_image: str) -> bool:
    """Is there a usable ART boot image at this `-Ximage:` path?

    `-Ximage:` takes an ABI-less path and ART appends the architecture
    directory itself, so the literal path usually does not exist on disk.
    """
    path = Path(boot_image)
    if path.exists():
        return True
    return any((path.parent / abi / path.name).exists() for abi in ("arm64", "arm", "x86_64", "x86"))


def capture_frame_app_process(
    *,
    camera_id: str = "0",
    zoom: str = "widest",
    width: int = 640,
    height: int = 480,
    capture_width: int = 1280,
    capture_height: int = 960,
    timeout: float = 25.0,
    jar_path: str = DEFAULT_CAPTURE_JAR,
    app_process_bin: str = DEFAULT_APP_PROCESS,
    boot_image: str = DEFAULT_BOOT_IMAGE,
    ffmpeg_bin: str = "ffmpeg",
    rotate: int = 2,
) -> tuple[bytes, int, int]:
    """Grab one JPEG from the Pixel's camera at a chosen zoom ratio.

    Returns ``(jpeg_bytes, width, height)``, matching the other backends.

    This is the only way to reach the ultra-wide lens. The three rear lenses
    are physical ids behind logical camera 0 and cannot be opened directly;
    `CONTROL_ZOOM_RATIO` below 1.0 is what makes the framework hand the request
    to the ultra-wide sensor, and only camera2 exposes it. `termux-camera-photo`
    has no zoom flag, and a plain native camera2 NDK binary aborts inside
    Android's own libbinder when cameraserver calls back into a process that is
    not a real app. Running the capture under `app_process` — the same trick
    scrcpy uses — gives a genuine Android runtime where all of that works.

    `zoom` is either ``"widest"`` (the minimum the camera reports, 0.556 on the
    Pixel 7 Pro, which is the ultra-wide) or an explicit ratio like ``"1.0"``.
    Verified live 2026-09-05: 0.556 reports physical id 3 / 1.95mm (ultra-wide),
    1.0 reports id 2 / 6.81mm (main), 10.0 reports id 4 / 19mm (telephoto).

    Raises `CameraCaptureError` on any failure so callers can fall back to
    `capture_frame_termux` rather than lose vision entirely.
    """
    jar = Path(jar_path)
    if not jar.is_file():
        raise CameraCaptureError(f"capture jar not found: {jar_path}")
    # ART refuses to load a dex it could write to ("Writable dex file ... is
    # not allowed"), and the resulting failure is far from the cause.
    if jar.stat().st_mode & 0o222:
        raise CameraCaptureError(f"capture jar must not be writable (chmod 444 {jar_path})")
    if not Path(app_process_bin).exists():
        raise CameraCaptureError(f"app_process not found: {app_process_bin}")
    if not _boot_image_present(boot_image):
        raise CameraCaptureError(f"ART boot image not found: {boot_image}")

    with tempfile.TemporaryDirectory() as tmpdir:
        raw_path = Path(tmpdir) / "raw.jpg"
        command = [
            app_process_bin,
            f"-Ximage:{boot_image}",
            "/",
            "HalCapture",
            "-c",
            camera_id,
            "-z",
            zoom,
            "-w",
            str(capture_width),
            "-h",
            str(capture_height),
            str(raw_path),
        ]
        env = dict(os.environ, CLASSPATH=str(jar))
        try:
            captured = subprocess.run(
                command, capture_output=True, timeout=timeout, check=False, env=env
            )
        except FileNotFoundError as error:
            raise CameraCaptureError(f"app_process not found: {app_process_bin}") from error
        except subprocess.TimeoutExpired as error:
            raise CameraCaptureError("camera capture timed out") from error
        if captured.returncode != 0:
            detail = captured.stderr.decode("utf-8", "replace").strip()[-300:]
            raise CameraCaptureError(f"app_process capture failed: {detail or 'no output'}")
        try:
            raw_size = raw_path.stat().st_size
        except OSError as error:
            raise CameraCaptureError("app_process capture did not write an output file") from error
        if raw_size == 0:
            raise CameraCaptureError("app_process capture produced an empty file")

        image_bytes = _rotate_and_scale(
            raw_path,
            width=width,
            height=height,
            rotate=rotate,
            timeout=timeout,
            ffmpeg_bin=ffmpeg_bin,
        )
    return image_bytes, width, height


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture one still frame without motion")
    parser.add_argument("--backend", choices=("ffmpeg", "termux", "app_process"), default="ffmpeg")
    parser.add_argument("--device", default="0", help="ffmpeg backend: avfoundation device index")
    parser.add_argument("--camera-id", default="0", help="termux backend: camera id (0=back, 1=front)")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=480)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--ffmpeg-bin", default="ffmpeg")
    parser.add_argument("--termux-camera-bin", default="termux-camera-photo")
    parser.add_argument(
        "--rotate",
        type=int,
        default=2,
        choices=(0, 1, 2, 3),
        help="termux backend: ffmpeg transpose value to correct the chassis camera mount (default 2, confirmed live for the current mount)",
    )
    parser.add_argument(
        "--zoom",
        default="widest",
        help="app_process backend: zoom ratio, or 'widest' for the ultra-wide lens (default)",
    )
    parser.add_argument("--capture-jar", default=DEFAULT_CAPTURE_JAR)
    parser.add_argument("--app-process-bin", default=DEFAULT_APP_PROCESS)
    parser.add_argument("--boot-image", default=DEFAULT_BOOT_IMAGE)
    parser.add_argument("--out", type=Path, default=Path("capture.jpg"))
    args = parser.parse_args(argv)

    try:
        if args.backend == "app_process":
            image_bytes, width, height = capture_frame_app_process(
                camera_id=args.camera_id,
                zoom=args.zoom,
                width=args.width,
                height=args.height,
                timeout=max(args.timeout, 25.0),
                jar_path=args.capture_jar,
                app_process_bin=args.app_process_bin,
                boot_image=args.boot_image,
                ffmpeg_bin=args.ffmpeg_bin,
                rotate=args.rotate,
            )
        elif args.backend == "termux":
            image_bytes, width, height = capture_frame_termux(
                camera_id=args.camera_id,
                width=args.width,
                height=args.height,
                timeout=args.timeout,
                termux_camera_bin=args.termux_camera_bin,
                ffmpeg_bin=args.ffmpeg_bin,
                rotate=args.rotate,
            )
        else:
            image_bytes, width, height = capture_frame(
                device=args.device,
                width=args.width,
                height=args.height,
                timeout=args.timeout,
                ffmpeg_bin=args.ffmpeg_bin,
            )
    except CameraCaptureError as error:
        print(f"capture failed: {error}", file=sys.stderr)
        return 1
    args.out.write_bytes(image_bytes)
    print(f"wrote {len(image_bytes)} bytes ({width}x{height}) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
