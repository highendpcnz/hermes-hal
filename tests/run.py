#!/usr/bin/env python
"""Zero-dependency tests for the pure-python parts of the HAL frontend.

Run with the Hermes venv (no pytest required):

    ~/.hermes/hermes-agent/venv/bin/python tests/run.py

HAL_SKIP_MODELS=1 is set below, so the STT/TTS models never load — the whole
suite finishes in seconds and touches no audio, network, or inference.
"""
import asyncio
import os
import re
import sys
import tempfile
import time
from pathlib import Path

os.environ["HAL_SKIP_MODELS"] = "1"
_tmp = tempfile.TemporaryDirectory(prefix="hal-tests-")
os.environ["HAL_DATA_DIR"] = _tmp.name

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
import hermes_bridge  # noqa: E402
import mission_control  # noqa: E402

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  ok  {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL  {name}  {detail}")


# --- speakable -------------------------------------------------------------

md = (
    "Run `ls` now.\n\n```py\nx=1\n```\n\n| a | b |\n| 1 | 2 |\n\n> quoted\n\n"
    "---\n\n1. one\n- two\n**bold** ~~struck~~ [link](http://example.com) \U0001F680"
)
out = main.speakable(md)
check("speakable strips inline code ticks", "`" not in out, out)
check("speakable replaces code fences", "x=1" not in out and "transcript" in out, out)
check("speakable collapses tables", "|" not in out, out)
check("speakable strips blockquote marker", ">" not in out and "quoted" in out, out)
check("speakable strips emphasis", "*" not in out and "~" not in out, out)
check("speakable keeps link text, drops target", "http" not in out and "link" in out, out)
check("speakable strips list markers", "1." not in out and "one" in out and "two" in out, out)
check("speakable strips emoji", "\U0001F680" not in out, out)
check("speakable never returns empty", main.speakable("---").strip() != "")
check("speakable truncates", len(main.speakable("word " * 2000)) <= main.MAX_SPOKEN_CHARS + 100)

# --- speech truncation -------------------------------------------------------

check("truncate passes short text through", main._truncate_speech("Hello, Dave.", 100) == "Hello, Dave.")
long = ("One sentence here. " * 20).strip()
cut = main._truncate_speech(long, 100)
check("truncate ends on a sentence", cut.endswith("sentence here.") and len(cut) <= 100, cut)
nospace = "x" * 300
check("truncate survives no boundaries", len(main._truncate_speech(nospace, 100)) == 100)
words = "word " * 100
wcut = main._truncate_speech(words, 52)
check("truncate falls back to word boundary", wcut.endswith("word") and len(wcut) <= 52, wcut)

# --- session ids and history -----------------------------------------------

check("session id accepts uuid-ish", main._valid_session_id("abc-123_X.z") == "abc-123_X.z")
check("session id rejects slash", main._valid_session_id("../etc") is None)
check("session id rejects empty", main._valid_session_id("") is None and main._valid_session_id(None) is None)
check("session id rejects overlong", main._valid_session_id("a" * 129) is None)
check("slash helper accepts command", main._looks_like_slash_command("/help"))
check("slash helper accepts arguments", main._looks_like_slash_command("/model gpt5"))
check("slash helper rejects absolute path", not main._looks_like_slash_command("/Users/dave/file.txt"))

try:
    main.session_file("../evil")
    check("session_file rejects traversal", False)
except ValueError:
    check("session_file rejects traversal", True)

sid = "test-hist"
main.save_history(sid, [{"role": "user", "content": "hi"}])
check("history roundtrip", main.load_history(sid) == [{"role": "user", "content": "hi"}])
main.session_file(sid).write_text("{not json")
check("history tolerates corrupt file", main.load_history(sid) == [])
main.session_file(sid).write_text('{"a": 1}')
check("history tolerates wrong shape", main.load_history(sid) == [])
check("history missing file", main.load_history("nope-xyz") == [])

# --- turn response headers ---------------------------------------------------

resp = main.Response()
main._apply_turn_headers(resp, "sid", False, "u" * 5000, "h" * 5000, {"infer": 12})
check(
    "transcript headers capped",
    len(resp.headers["X-Hal-Transcript"]) <= main.MAX_TRANSCRIPT_HEADER_CHARS * 3 + 16,
)
check("server timing header", "infer;dur=12" in resp.headers["Server-Timing"])
check("sample rate fallback without model", main.SAMPLE_RATE == 22050)

# --- CLI output cleanup ------------------------------------------------------

raw = "\x1b[31mred\x1b[0m\n\n\n\n" + str(Path.home()) + "/x\n" + "y" * 20000
cli = main._clean_cli_text(raw)
check("cli strips ansi", "\x1b" not in cli and "red" in cli)
check("cli shortens home dir", "~/x" in cli)
check("cli truncates", len(cli) <= main.MAX_CLI_OUTPUT_CHARS + 32 and "[truncated]" in cli)
check("cli empty placeholder", main._clean_cli_text("\x1b[2J") == "(no output)")

# --- hermes_bridge helpers ---------------------------------------------------

hosts = hermes_bridge._parse_check_hosts("1.1.1.1:443, example.com , [::1]:8080, bad:port")
check(
    "offline host parsing",
    ("1.1.1.1", 443) in hosts and ("example.com", 443) in hosts and ("::1", 8080) in hosts
    and len(hosts) == 3,
    repr(hosts),
)

smp = Path(_tmp.name) / "map-test.json"
sm = hermes_bridge.SessionMap(smp)
sm.set("c1", "h1")
sm.set("c2", "h2")
sm.drop("c1")
sm2 = hermes_bridge.SessionMap(smp)
check("session map persists", sm2.get("c2") == "h2" and sm2.get("c1") is None)

# --- keyed session locks -------------------------------------------------------
# Overlapping turns on one session must serialize, and eviction must not drop
# a lock that still has waiters (the old locked() check raced exactly there).


async def _exercise_keyed_locks():
    locks = hermes_bridge.KeyedLocks()
    active = 0
    max_active = 0

    async def turn():
        nonlocal active, max_active
        async with locks.hold("sess"):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.005)
            active -= 1

    await asyncio.gather(*(turn() for _ in range(4)))
    return max_active, locks


_max_active, _keyed = asyncio.run(_exercise_keyed_locks())
check("keyed locks serialize a session", _max_active == 1, str(_max_active))
check(
    "keyed locks evict only when idle",
    _keyed._locks == {} and _keyed._refs == {},
    repr((_keyed._locks, _keyed._refs)),
)

# --- pending permission registry ----------------------------------------------


async def _exercise_permissions():
    req_id, fut = hermes_bridge._register_permission("owner-1", "Run ls")
    found = hermes_bridge.pending_permission_for("owner-1")
    none_found = hermes_bridge.pending_permission_for("owner-2")
    wrong_owner = hermes_bridge.resolve_permission(req_id, True, "owner-2")
    unknown = hermes_bridge.resolve_permission("nope", True, "owner-1")
    right = hermes_bridge.resolve_permission(req_id, True, "owner-1")
    value = await fut
    double = hermes_bridge.resolve_permission(req_id, False, "owner-1")
    hermes_bridge._pending_permissions.pop(req_id, None)
    return found == req_id, none_found is None, wrong_owner, unknown, right, value, double


_found, _none, _wrong, _unknown, _right, _value, _double = asyncio.run(_exercise_permissions())
check("pending permission is discoverable by owner", _found and _none)
check("foreign session cannot resolve a permission", not _wrong and not _unknown)
check("owner resolves the permission", _right and _value is True)
check("a resolved permission cannot be re-answered", not _double)

# --- spoken permission answers -------------------------------------------------

check("voice allow matches", main._PERM_ALLOW_RE.match("Yes.") is not None)
check("voice allow with address", main._PERM_ALLOW_RE.match("HAL, go ahead") is not None)
check("voice deny matches", main._PERM_DENY_RE.match("no") is not None)
check("voice deny abort", main._PERM_DENY_RE.match("Hal, abort!") is not None)
check("ordinary speech is not an answer",
      main._PERM_ALLOW_RE.match("yes we should think about this") is None
      and main._PERM_DENY_RE.match("no idea what that means") is None)

# --- wake-word gate --------------------------------------------------------------

# The attention word is required by default (HAL_WAKE_REQUIRE_ATTENTION), so a
# bare name no longer wakes anything — that is the documented cost of not
# false-waking on televised dialogue. See _WAKE_RE's comment in main.py.
check("wake requires attention by default", main.WAKE_REQUIRE_ATTENTION)
check("wake matches hey-prefix", main._WAKE_RE.match("Hey HAL what's our status?") is not None)
check("wake matches hello-prefix", main._WAKE_RE.match("Hello HAL, open the log.") is not None)
check("wake accepts hell homophone with attention",
      main._WAKE_RE.match("Hey hell, open the log.") is not None)
check("wake accepts hall homophone with attention",
      main._WAKE_RE.match("OK hall, open the log.") is not None)
check("wake rejects a bare address", main._WAKE_RE.match("HAL, open the log.") is None)
check("wake rejects hello", main._WAKE_RE.match("Hello there") is None)
check("wake rejects embedded hal", main._WAKE_RE.match("Halt the presses") is None)
# The regression these exist to prevent: an ambient question that merely opens
# with a homophone of the name must never reach the agent.
check("wake rejects how-are-you", main._WAKE_RE.match("How are you doing?") is None)
check("wake rejects a hull sentence", main._WAKE_RE.match("Hull integrity is fine") is None)
check("wake rejects hell-of-a-day", main._WAKE_RE.match("Hell of a day") is None)
check("wake rejects a bare howl", main._WAKE_RE.match("Howl, open the log.") is None)
check(
    "wake keeps the remainder",
    main._WAKE_RE.match("Hey HAL, start mission scan logs").group(1).strip()
    == "start mission scan logs",
)
# Normalization runs before the gate, which is what lets the gate's own name
# list stay narrow while the measured homophones still work end to end.
check(
    "normalized homophone reaches the gate",
    main._WAKE_RE.match(main._normalize_hal_name("Hey how, run diagnostics.")) is not None,
)
check(
    "normalized ambient question still does not",
    main._WAKE_RE.match(main._normalize_hal_name("How are you doing?")) is None,
)
# Measured on the Pixel, 2026-09-06: HAL's own voice saying "Hey HAL, in one
# short sentence, what are you?" transcribes with NO comma after the name. An
# earlier version required punctuation here and so refused to wake on this
# project's own wake phrase. The attention word is the address marker.
check(
    "wake phrase survives whisper dropping the comma",
    main._WAKE_RE.match(
        main._normalize_hal_name("Hey how in one short sentence, what are you?")) is not None,
)
# ...but the attention word must not swallow ordinary how-questions.
for _q in ("Hey, how are you doing?", "hey how much does it weigh",
           "hello how far is the wall", "ok how long will that take"):
    check(f"attention word does not turn a question into a wake: {_q!r}",
          main._WAKE_RE.match(main._normalize_hal_name(_q)) is None)

# --- _normalize_hal_name ---------------------------------------------------------

check(
    "normalize fixes hell",
    main._normalize_hal_name("Hell, open the pod bay doors.") == "HAL, open the pod bay doors.",
)
check(
    "normalize fixes howl",
    main._normalize_hal_name("Howl, open the pod bay doors.") == "HAL, open the pod bay doors.",
)
check(
    "normalize fixes how with comma",
    main._normalize_hal_name("How, what time is it?") == "HAL, what time is it?",
)
check(
    "normalize fixes hull",
    main._normalize_hal_name("Hull, check the systems.") == "HAL, check the systems.",
)
check(
    "normalize fixes hey-prefixed homophone",
    main._normalize_hal_name("Hey howl, run diagnostics.") == "Hey HAL, run diagnostics.",
)
check(
    "normalize ignores mid-sentence how",
    main._normalize_hal_name("Tell me how to do it.") == "Tell me how to do it.",
)
check(
    "normalize ignores hello",
    main._normalize_hal_name("Hello there.") == "Hello there.",
)
check(
    "normalize ignores how-are-you (no pause boundary)",
    main._normalize_hal_name("How are you doing?") == "How are you doing?",
)
check(
    "normalize preserves already-correct HAL",
    main._normalize_hal_name("HAL, what's our status?") == "HAL, what's our status?",
)

# --- robot tools: safety gating (transferred from hal) ---------------------------

import robot_tools  # noqa: E402
import stopwords  # noqa: E402

# Motion is opt-in. This is the single most important default in the file: the
# CyberPi has never been driven from this repo and none of it is hardware-tested.
check("motion is disabled by default", robot_tools.MOTION_ENABLED is False)
check(
    "run_motion refuses while disabled",
    robot_tools.run_motion(lambda: (_ for _ in ()).throw(AssertionError("opened transport!")),
                           lambda c: None)["ok"] is False,
)
check(
    "drive refuses while disabled without opening a transport",
    robot_tools.drive_straight(
        lambda: (_ for _ in ()).throw(AssertionError("opened transport!")), 10, 10)["ok"] is False,
)

# One-use, exact-argument grants. Approving one motion must not authorize another.
_S = "session-under-test"
_ARGS = {"distance_cm": 20, "speed_pct": 10}
robot_tools.grant_motion_for(_S, "drive_straight", robot_tools._canonical(_ARGS),
                             expires_at=time.monotonic() + 60)
check(
    "a grant authorizes its exact motion",
    robot_tools.check_and_consume_motion_grant(_S, "drive_straight", _ARGS) is None,
)
check(
    "a grant is single-use",
    robot_tools.check_and_consume_motion_grant(_S, "drive_straight", _ARGS) is not None,
)

robot_tools.grant_motion_for(_S, "drive_straight", robot_tools._canonical(_ARGS),
                             expires_at=time.monotonic() + 60)
check(
    "a grant does not authorize a longer drive",
    robot_tools.check_and_consume_motion_grant(
        _S, "drive_straight", {"distance_cm": 50, "speed_pct": 10}) is not None,
)
robot_tools.grant_motion_for(_S, "drive_straight", robot_tools._canonical(_ARGS),
                             expires_at=time.monotonic() + 60)
check(
    "a grant does not authorize a different tool",
    robot_tools.check_and_consume_motion_grant(
        _S, "turn", {"angle_degrees": 20, "speed_pct": 10}) is not None,
)
robot_tools.grant_motion_for(_S, "drive_straight", robot_tools._canonical(_ARGS),
                             expires_at=time.monotonic() - 1)
check(
    "an expired grant does not authorize",
    robot_tools.check_and_consume_motion_grant(_S, "drive_straight", _ARGS) is not None,
)
check(
    "an unknown session has no grant",
    robot_tools.check_and_consume_motion_grant("nobody", "drive_straight", _ARGS) is not None,
)

# The approval prompt must describe exactly what will happen, and must refuse
# to describe anything outside the bounds robot/safety.py enforces.
_ok = robot_tools.authorized_motion_request(
    {"motion": "drive_straight", "distance_cm": -20, "speed_pct": 10})
check("authorization describes the motion in words",
      _ok is not None and _ok[2] == "drive backward 20 cm at 10% speed")
for _bad, _why in (
    ({"motion": "drive_straight", "distance_cm": 51, "speed_pct": 10}, "over distance limit"),
    ({"motion": "drive_straight", "distance_cm": 10, "speed_pct": 31}, "over speed limit"),
    ({"motion": "drive_straight", "distance_cm": 10, "speed_pct": 0}, "zero speed"),
    ({"motion": "turn", "angle_degrees": 181, "speed_pct": 10}, "over turn limit"),
    ({"motion": "turn", "angle_degrees": True, "speed_pct": 10}, "bool is not an angle"),
    ({"motion": "fly", "speed_pct": 10}, "unknown motion"),
):
    check(f"authorization refuses: {_why}", robot_tools.authorized_motion_request(_bad) is None)

# The bounds the prompt promises are the bounds the hardware layer enforces.
from robot.safety import MotionLimits  # noqa: E402
_lim = MotionLimits()
check("prompt bounds match MotionLimits distance", _lim.max_distance_cm == 50)
check("prompt bounds match MotionLimits speed", _lim.max_speed_pct == 30)
check("prompt bounds match MotionLimits turn", _lim.max_turn_degrees == 180)

# --- crawl autonomy gates ------------------------------------------------------

from robot.crawl import CrawlController, CrawlLimits, CrawlSafetyError  # noqa: E402

_lim = CrawlLimits()
check("crawl segment cap is 15cm", _lim.max_segment_cm == 15)
check("crawl speed cap is 10%", _lim.max_speed_pct == 10)
check("crawl total budget is 200cm", _lim.max_total_distance_cm == 200)
check("crawl episode window is 5 minutes", _lim.max_duration_seconds == 300.0)
check("crawl demands more clearance than the base interlock",
      _lim.min_obstacle_cm > MotionLimits().min_obstacle_cm)


def _crawl_refuses(fn):
    """True when fn() is rejected by a crawl safety gate."""
    try:
        fn()
    except CrawlSafetyError:
        return True
    return False


# The limits object refuses to be constructed weaker than the base interlock.
try:
    CrawlLimits(min_obstacle_cm=1.0)
    _weakened = True
except CrawlSafetyError:
    _weakened = False
check("crawl limits cannot be built weaker than the base interlock", not _weakened)

_c = CrawlController(CrawlLimits())
check("crawl refuses to drive before it is armed",
      _crawl_refuses(lambda: _c.prepare_drive(5, 10)))
_c.arm()
check("crawl refuses to drive with no capture",
      _crawl_refuses(lambda: _c.prepare_drive(5, 10)))
_c.record_capture("frame-1")
check("crawl refuses an assessment of a different frame",
      _crawl_refuses(lambda: _c.record_assessment("frame-2", "clear", 1.0)))
_c.record_assessment("frame-1", "blocked", 1.0)
check("crawl refuses to drive on a blocked assessment",
      _crawl_refuses(lambda: _c.prepare_drive(5, 10)))
_c.record_assessment("frame-1", "clear", 0.5)
check("crawl refuses a low-confidence clear",
      _crawl_refuses(lambda: _c.prepare_drive(5, 10)))
_c.record_assessment("frame-1", "clear", 0.95)
check("crawl refuses an over-long segment",
      _crawl_refuses(lambda: _c.prepare_drive(_lim.max_segment_cm + 1, 10)))
check("crawl refuses an over-fast segment",
      _crawl_refuses(lambda: _c.prepare_drive(5, _lim.max_speed_pct + 1)))
_c.prepare_drive(5, 10)
check("crawl accepts a clear, in-bounds segment", True)
_c.consume_drive(5)
check("crawl spends the assessment after a step",
      _crawl_refuses(lambda: _c.prepare_drive(5, 10)))
check("crawl decrements the budget", _c.state.remaining_cm == _lim.max_total_distance_cm - 5)

# A stale frame must not authorise a step, however good the assessment.
_clock = [1000.0]
_c2 = CrawlController(CrawlLimits(), clock=lambda: _clock[0])
_c2.arm()
_c2.record_capture("f")
_c2.record_assessment("f", "clear", 1.0)
_clock[0] += CrawlLimits().max_frame_age_seconds + 1
check("crawl refuses a stale camera frame",
      _crawl_refuses(lambda: _c2.prepare_drive(5, 10)))

# The episode expires on its own.
_clock2 = [1000.0]
_c3 = CrawlController(CrawlLimits(), clock=lambda: _clock2[0])
_c3.arm()
_clock2[0] += CrawlLimits().max_duration_seconds + 1
check("crawl disarms itself when the episode expires", not _c3.is_active())

# Both default off, and crawl_arm refuses unless BOTH are on — sight is what
# separates crawling from repeated blind driving.
check("crawl is unreachable with motion off",
      not robot_tools.MOTION_ENABLED and not robot_tools.CAMERA_ENABLED)

# A refused crawl step must report ok:false. It did not: crawl_status carried
# its own "ok": True and dict union let it overwrite the refusal, so a rejected
# step came back as a success. Found on the device, 2026-09-06.
_refused = robot_tools.crawl_step("no-such-session", "x", "clear", 1.0, 5, 10)
check("a refused crawl step reports failure", _refused.get("ok") is False)
check("a refused crawl step still says why", bool(_refused.get("error")))
check("crawl state fields never carry an ok",
      "ok" not in robot_tools._crawl_state("no-such-session"))

# --- stop intercept (transferred from hal) ---------------------------------------

for _said in ("Stop!", "stop", "HAL, stop", "stop the robot", "halt", "emergency stop"):
    check(f"stop fires on {_said!r}", stopwords.is_stop_command(_said))
# A stop must not fire on discussion of stopping — but the budget is deliberately
# the opposite of farewell's: a false positive only stops a robot that did not
# need stopping, so this leans toward firing.
for _said in ("what does stop mean", "don't stop", "stop talking about that"):
    check(f"stop ignores {_said!r}", not stopwords.is_stop_command(_said))

# --- farewell (transferred from hal) ---------------------------------------------

import farewell  # noqa: E402

for _said in ("That'll be all, HAL.", "Hey HAL, goodbye.", "That's all, how.",
              "We're done for tonight.", "Thank you HAL, that is all.", "Dismissed."):
    check(f"farewell fires on {_said!r}", farewell.is_farewell(_said))
# A false positive destroys history; a false negative costs one repeat. These
# are the shapes that must never fire.
for _said in ("that's all the power we have left", "How do you say goodbye in French?",
              "Don't say goodbye.", "What does 'that will be all' mean?",
              "Tell me about the end of the mission.", "I'm done with the first task, now scan."):
    check(f"farewell ignores {_said!r}", not farewell.is_farewell(_said))

# --- termux voice loop (transferred from hal) ------------------------------------

import termux_voice  # noqa: E402

check("termux gate requires attention by default", termux_voice.WAKE_REQUIRE_ATTENTION)
check("termux gate wakes on hey-hal", termux_voice._heard_wake_word("hey HAL, status report"))
# The measured base.en homophones — "HAL, open the pod bay doors, HAL... hey HAL"
# came back as "How? Open the pod bay doors, huh? Hey, how?" on the target phone.
check("termux gate wakes on hey-how", termux_voice._heard_wake_word("Hey, how? Drive forward."))
check("termux gate wakes on hi-huh", termux_voice._heard_wake_word("Hi huh, run diagnostics"))
check("termux gate ignores ambient how", not termux_voice._heard_wake_word("how are you"))
check("termux gate ignores a bare address",
      not termux_voice._heard_wake_word("open the pod bay doors, HAL"))
check("termux gate ignores unrelated speech",
      not termux_voice._heard_wake_word("I don't know how we shall halt it"))
check("empty wake word disables the gate", termux_voice._heard_wake_word("anything", ""))
check("a non-hal wake word is matched literally only",
      termux_voice._heard_wake_word("hey computer, status", "computer")
      and not termux_voice._heard_wake_word("hey how, status", "computer"))
check("silence gate default is a real dBFS threshold", termux_voice.SILENCE_PEAK_DBFS < 0)
check("playback waits past the clip", termux_voice.PLAYBACK_SETTLE_SECONDS > 0)
check("recorder is given time to finalise", termux_voice.RECORD_FINALISE_SECONDS > 0)

# --- whisper.cpp STT backend (transferred from hal) ------------------------------

import termux_whisper_cpp  # noqa: E402

try:
    termux_whisper_cpp.WhisperCppModel("/nonexistent/ggml-base.en.bin")
    _missing_model_raised = False
except termux_whisper_cpp.WhisperCppError:
    _missing_model_raised = True
check("whisper.cpp rejects a missing model file", _missing_model_raised)
check(
    "whisper.cpp mimics faster-whisper's device attribute",
    termux_whisper_cpp._DeviceInfo.device == "cpu",
)
check(
    "STT auto-detection is by binary presence, not a platform flag",
    not os.path.isfile(main.WHISPER_CPP_BIN) or main.STT is not None or main.SKIP_MODELS,
)
check("transcribe degrades to empty when STT is unavailable",
      main.STT is not None or main.transcribe(b"") == "")
check("stt_device reports n/a when STT is unavailable",
      main.STT is not None or main._stt_device() == "n/a")

# --- _wake_word_required ---------------------------------------------------------

check(
    "wake mode gates ambient duplex speech",
    main._wake_word_required(
        from_speech=True,
        wake_gated=True,
        manual_capture=False,
        enrollment_pending=False,
    ),
)
check(
    "manual push-to-talk bypasses wake mode",
    not main._wake_word_required(
        from_speech=True,
        wake_gated=True,
        manual_capture=True,
        enrollment_pending=False,
    ),
)
check(
    "voice enrollment bypasses wake mode",
    not main._wake_word_required(
        from_speech=True,
        wake_gated=True,
        manual_capture=False,
        enrollment_pending=True,
    ),
)

# --- SSE event aliasing (missions) -------------------------------------------

q = hermes_bridge.register_event_queue("browser-1")
hermes_bridge.alias_events("mission-sess", "browser-1")
hermes_bridge.publish_event("mission-sess", {"type": "tool_call", "title": "probe"})
_aliased = q.get_nowait() if not q.empty() else ""
check("aliased publish reaches target queue", "probe" in _aliased, _aliased)
check("aliased publish is tagged with its mission", '"mission_session": "mission-sess"' in _aliased.replace('":"', '": "'), _aliased)
hermes_bridge.unalias_events("mission-sess")
hermes_bridge.publish_event("mission-sess", {"type": "tool_call"})
check("unaliased publish goes nowhere", q.empty())
hermes_bridge.publish_event("browser-1", {"type": "mission_update"})
check("direct publish still works", not q.empty())
hermes_bridge.unregister_event_queue("browser-1", q)

# --- mission triggers ---------------------------------------------------------

check("mission trigger typed", main._mission_request("/mission fix the CI") == "fix the CI")
check("mission trigger typed empty", main._mission_request("/mission") == "")
check(
    "mission trigger voice",
    main._mission_request("HAL, start mission clean the pod bay.") == "clean the pod bay",
)
check(
    "mission trigger voice no comma",
    main._mission_request("hal start mission scan logs") == "scan logs",
)
check("mission trigger negative", main._mission_request("Hal, what's a mission?") is None)
check("plain text is not a mission", main._mission_request("open the pod bay doors") is None)

# --- mission persistence --------------------------------------------------------

mdir = Path(_tmp.name) / "missions-data"
mgr = mission_control.MissionManager(mdir)
mission = mission_control.Mission(id="m1", title="probe pod", cookie_id="c1", session_id="s1")
mgr._save(mission)
check("mission write leaves no tmp file", not list(mgr.missions_dir.glob("*.tmp")))
done = mission_control.Mission(
    id="m2", title="done", cookie_id="c2", session_id="s2", status="completed", result="ok"
)
mgr._save(done)
(mgr.missions_dir / "corrupt.json").write_text("{not json")
mgr2 = mission_control.MissionManager(mdir)
check("active mission is failed after restart", mgr2.missions["m1"].status == "failed")
check("completed mission survives restart", mgr2.missions["m2"].status == "completed")
check("corrupt mission file tolerated", len(mgr2.missions) == 2)
check(
    "list_missions filters by cookie",
    [m["id"] for m in mgr2.list_missions("c1")] == ["m1"],
)

# --- mission execution: cap, brain notes, failure journal ----------------------

import json  # noqa: E402
import mission_control as mc  # noqa: E402


async def _exercise_missions():
    orig_ask = hermes_bridge.ask_hermes

    async def fake_ask(text, sid):
        if "explode" in text:
            raise RuntimeError("boom")
        await asyncio.sleep(0.01)
        return "All wrapped up."

    hermes_bridge.ask_hermes = fake_ask
    try:
        mgr3 = mc.MissionManager(Path(_tmp.name) / "missions-exec")
        for i in range(mc.MAX_ACTIVE_MISSIONS):
            mgr3.create_mission("ck", f"m{i}", "do the thing")
        capped = False
        try:
            mgr3.create_mission("ck", "over-cap", "do the thing")
        except mc.MissionLimitError:
            capped = True
        mgr3.create_mission("ck2", "explode", "explode now")
        await asyncio.gather(*list(mgr3._tasks))
        notes = mgr3.drain_notes("ck")
        drained = mgr3.drain_notes("ck")
        fail_notes = mgr3.drain_notes("ck2")
        return mgr3, capped, notes, drained, fail_notes
    finally:
        hermes_bridge.ask_hermes = orig_ask


_mgr3, _capped, _notes, _drained, _fail_notes = asyncio.run(_exercise_missions())
check("mission cap enforced", _capped)
check(
    "missions complete with results and finish times",
    all(m.status == "completed" and m.finished_at for m in _mgr3.missions.values()
        if m.cookie_id == "ck"),
)
check(
    "completed missions queue brain notes",
    len(_notes) == mc.MAX_ACTIVE_MISSIONS and all("All wrapped up." in n for n in _notes),
    repr(_notes),
)
check("notes drain once", _drained == [])
check("failed mission queues a failure note", len(_fail_notes) == 1 and "failed" in _fail_notes[0])
_failed_journal = (_mgr3.missions_dir / "failed.jsonl").read_text().splitlines()
check(
    "failed mission journaled for reflection",
    len(_failed_journal) == 1 and json.loads(_failed_journal[0])["title"] == "explode",
)

# --- mission triggers -----------------------------------------------------------


async def _exercise_triggers():
    orig_ask = hermes_bridge.ask_hermes

    async def fake_ask(text, sid):
        return "trigger done"

    hermes_bridge.ask_hermes = fake_ask
    try:
        tdir = Path(_tmp.name) / "missions-trig"
        mgr4 = mc.MissionManager(tdir)
        watched = tdir / "watched"
        watched.mkdir()
        (tdir / "triggers.json").write_text(json.dumps([
            {"title": "hourly", "prompt": "check systems", "every_minutes": 1},
            {"title": "no prompt — skipped"},
            {"title": "watcher", "prompt": "scan downloads", "watch": str(watched / "*")},
            {"title": "off", "prompt": "x", "every_minutes": 1, "enabled": False},
        ]))
        state: dict = {}
        mgr4._scheduler_tick(state, now=1000.0)          # arms interval, baselines watch
        after_arm = len(mgr4.missions)
        mgr4._scheduler_tick(state, now=1000.0 + 61)     # interval fires
        (watched / "new.txt").write_text("hello")
        mgr4._scheduler_tick(state, now=1000.0 + 62)     # watcher fires
        mgr4._scheduler_tick(state, now=1000.0 + 63)     # nothing new — no refire
        await asyncio.gather(*list(mgr4._tasks))
        return mgr4, after_arm
    finally:
        hermes_bridge.ask_hermes = orig_ask


_mgr4, _after_arm = asyncio.run(_exercise_triggers())
_titles = sorted(m.title for m in _mgr4.missions.values())
check("triggers arm without a boot storm", _after_arm == 0)
check("interval and watch triggers fire exactly once", _titles == ["hourly", "watcher"], repr(_titles))
check(
    "trigger missions belong to the trigger cookie",
    all(m.cookie_id == mc.TRIGGER_COOKIE for m in _mgr4.missions.values()),
)
_watch_mission = next(m for m in _mgr4.missions.values() if m.title == "watcher")
check("watch trigger annotates its prompt", "(Trigger: files changed under" in _watch_mission.prompt)
check(
    "trigger missions visible to every session",
    len(_mgr4.list_missions("any-cookie")) == 2,
)
_tdir2 = Path(_tmp.name) / "missions-trig2"
_mgr5 = mc.MissionManager(_tdir2)
(_tdir2 / "triggers.json").write_text("{not valid json")
check("garbage triggers.json tolerated", _mgr5._load_triggers() == [])
check("absent triggers.json tolerated", mc.MissionManager(Path(_tmp.name) / "missions-trig3")._load_triggers() == [])

# --- daily "at" triggers, persisted state, per-trigger permissions ---------------

check("at parses valid time", mc._parse_at("07:30") == (7, 30))
check("at parses midnight", mc._parse_at("0:00") == (0, 0))
check("at rejects junk", all(mc._parse_at(v) is None for v in ("25:00", "7:60", "0730", 730, None, "a:b")))


async def _exercise_at_triggers():
    orig_ask = hermes_bridge.ask_hermes
    allow_seen: list[bool] = []

    async def fake_ask(text, sid):
        allow_seen.append(sid in hermes_bridge._tool_allowed_cookies)
        return "briefed"

    hermes_bridge.ask_hermes = fake_ask
    try:
        import time as _time
        tdir = Path(_tmp.name) / "missions-at"
        mgr = mc.MissionManager(tdir)
        (tdir / "triggers.json").write_text(json.dumps([
            {"title": "briefing", "prompt": "brief me", "at": "07:30", "permissions": "allow"},
        ]))
        base = _time.mktime((2026, 3, 10, 6, 0, 0, 0, 0, -1))  # a 06:00 local
        state: dict = {}
        mgr._scheduler_tick(state, now=base)                     # first sight: arm, no fire
        armed_quiet = len(mgr.missions) == 0
        mgr._scheduler_tick(state, now=base + 2 * 3600)          # 08:00 — 07:30 passed → fire
        fired = len(mgr.missions) == 1
        mgr._scheduler_tick(state, now=base + 3 * 3600)          # 09:00 — same day, no refire
        no_refire = len(mgr.missions) == 1
        # Server "down" across the next day's 07:30 — the 11:00 tick catches up.
        mgr._scheduler_tick(state, now=base + 86400 + 5 * 3600)
        caught_up = len(mgr.missions) == 2
        await asyncio.gather(*list(mgr._tasks))
        return mgr, armed_quiet, fired, no_refire, caught_up, allow_seen
    finally:
        hermes_bridge.ask_hermes = orig_ask


_mgr_at, _armed_quiet, _fired, _no_refire, _caught_up, _allow_seen = asyncio.run(_exercise_at_triggers())
check("at trigger arms on first sight", _armed_quiet)
check("at trigger fires once when its time passes", _fired and _no_refire)
check("at trigger catches up after downtime, once", _caught_up)
check(
    "trigger permissions allow tools during the mission only",
    _allow_seen == [True, True] and not hermes_bridge._tool_allowed_cookies,
    repr((_allow_seen, hermes_bridge._tool_allowed_cookies)),
)
check(
    "at mission records allow_tools",
    all(m.allow_tools for m in _mgr_at.missions.values()),
)

_state_mgr = mc.MissionManager(Path(_tmp.name) / "missions-state")
_state_mgr._save_trigger_state({"briefing": {"last_at": 123.0}})
check(
    "trigger state persists across managers",
    mc.MissionManager(Path(_tmp.name) / "missions-state")._load_trigger_state()
    == {"briefing": {"last_at": 123.0}},
)
(Path(_tmp.name) / "missions-state" / "trigger_state.json").write_text("{bad")
check("corrupt trigger state tolerated", _state_mgr._load_trigger_state() == {})

# --- queued trigger announcements -------------------------------------------------

main.active_websockets.clear()
main._pending_announcements.clear()
_trig_mission = mission_control.Mission(
    id="t-ann", title="briefing", cookie_id=mc.TRIGGER_COOKIE, session_id="s-ann",
    status="completed", result="Sky is clear, Dave.",
)
asyncio.run(main.on_mission_complete(_trig_mission))
check(
    "trigger report queues when the Bridge is empty",
    len(main._pending_announcements) == 1 and "Sky is clear" in main._pending_announcements[0],
    repr(main._pending_announcements),
)
for _i in range(main.MAX_PENDING_ANNOUNCEMENTS + 5):
    main._pending_announcements.append(f"note {_i}")
    del main._pending_announcements[:-main.MAX_PENDING_ANNOUNCEMENTS]
check("announcement queue is capped", len(main._pending_announcements) == main.MAX_PENDING_ANNOUNCEMENTS)
_drained = main._drain_announcements()
check(
    "announcements drain once",
    len(_drained) == main.MAX_PENDING_ANNOUNCEMENTS and main._drain_announcements() == [],
)

# --- steerable missions: voice grammar --------------------------------------------

check("cancel matches bare form", main._cancel_request("HAL, cancel the mission.") == "")
check("cancel matches with title", main._cancel_request("Hal, abort mission downloads sweep") == "downloads sweep")
check("cancel matches stop-that", main._cancel_request("HAL, stop that mission!") == "")
check("cancel rejects plain talk", main._cancel_request("HAL, why would anyone cancel christmas?") is None)
check("followup voice form", main._followup_request("HAL, ask the mission: what did you change?") == "what did you change?")
check("followup typed form", main._followup_request("/ask what did you find") == "what did you find")
check("followup empty typed", main._followup_request("/ask") == "")
check("followup rejects plain talk", main._followup_request("I need to ask you something") is None)
check("status matches", main._MISSION_STATUS_RE.match("HAL, missions status") is not None)
check("status matches how-going", main._MISSION_STATUS_RE.match("Hal, how are the missions going?") is not None)
check("status rejects plain talk", main._MISSION_STATUS_RE.match("HAL, how are you?") is None)

check("elapsed seconds", main._spoken_elapsed(42) == "42 seconds")
check("elapsed minutes", main._spoken_elapsed(180) == "3 minutes")
check("elapsed hours", main._spoken_elapsed(3720) == "1 hour 2 minutes")

# --- steerable missions: lifecycle ------------------------------------------------


async def _exercise_steering():
    orig_ask = hermes_bridge.ask_hermes
    gate = asyncio.Event()
    prompts: list[tuple[str, str]] = []

    async def fake_ask(text, sid):
        prompts.append((sid, text))
        await gate.wait()
        return "I scanned the pod bay."

    hermes_bridge.ask_hermes = fake_ask
    main.ask_hermes = fake_ask
    try:
        mgr = mc.MissionManager(Path(_tmp.name) / "missions-steer")
        running = mgr.create_mission("st-ck", "pod bay scan", "scan it")
        await asyncio.sleep(0.01)  # let the mission task reach the gate
        # Cancel while in flight: turn returns, status must stay cancelled.
        cancelled = await mgr.cancel_mission(running.id, "st-ck")
        foreign = await mgr.cancel_mission(running.id, "other-ck")
        gate.set()
        await asyncio.gather(*list(mgr._tasks))
        after = mgr.missions[running.id]
        journal_absent = not (mgr.missions_dir / "failed.jsonl").exists()

        # A completed mission is steerable within the TTL…
        done = mc.Mission(
            id="st-done", title="probe", cookie_id="st-ck", session_id="st-sess",
            status="completed", result="ok", finished_at=mc.time.time(),
        )
        mgr.missions[done.id] = done
        steer = mgr.steerable_mission("st-ck")
        # …not after dismissal (the cancelled mission is steerable too — its
        # session survives — so both must go before steering runs dry).
        ok_dismiss = mgr.dismiss_mission(done.id, "st-ck")
        ok_dismiss = ok_dismiss and mgr.dismiss_mission(running.id, "st-ck")
        steer_after_dismiss = mgr.steerable_mission("st-ck")
        listed = [m["id"] for m in mgr.list_missions("st-ck")]
        # …and the reaper releases stale sessions.
        stale = mc.Mission(
            id="st-stale", title="old", cookie_id="st-ck", session_id="st-old",
            status="completed", result="ok", finished_at=mc.time.time() - mc.STEERABLE_TTL - 60,
        )
        mgr.missions[stale.id] = stale
        mgr._reap_sessions()
        return after, cancelled, foreign, journal_absent, steer, ok_dismiss, steer_after_dismiss, listed, stale
    finally:
        hermes_bridge.ask_hermes = orig_ask
        main.ask_hermes = orig_ask


(_after, _cancelled, _foreign, _journal_absent, _steer, _ok_dismiss,
 _steer_after_dismiss, _listed, _stale) = asyncio.run(_exercise_steering())
check("cancel interrupts an active mission", _cancelled is not None and _after.status == "cancelled")
check("cancelled result is not the turn's reply", _after.result == "Cancelled by Dave.")
check("foreign session cannot cancel", _foreign is None)
check("cancelled missions do not journal as failures", _journal_absent)
check("finished mission is steerable within TTL", _steer is not None and _steer.id == "st-done")
check("dismiss releases and hides the mission", _ok_dismiss and _steer_after_dismiss is None)
check("dismissed missions leave the board", "st-done" not in _listed)
check("reaper releases sessions past the TTL", _stale.session_dropped)

# --- steerable missions: turn routing ----------------------------------------------


async def _exercise_followup_turn():
    orig_ask = main.ask_hermes
    seen: list[tuple[str, str]] = []

    async def fake_ask(text, sid):
        seen.append((sid, text))
        return "I found nothing unusual, Dave."

    main.ask_hermes = fake_ask
    orig_manager = mission_control.manager
    try:
        mission_control.manager = mc.MissionManager(Path(_tmp.name) / "missions-turn")
        done = mc.Mission(
            id="ft-1", title="pod inspection", cookie_id="ft-ck", session_id="ft-sess",
            status="completed", result="ok", finished_at=mc.time.time(),
        )
        mission_control.manager.missions[done.id] = done
        reply, _t = await main.run_turn_text("ft-ck", "HAL, ask the mission: any anomalies?")
        empty_reply, _t2 = await main.run_turn_text("ft-ck", "/ask")
        no_target, _t3 = await main.run_turn_text("stranger-ck", "/ask anything?")
        status_line, _t4 = await main.run_turn_text("ft-ck", "HAL, missions status")
        return seen, reply, empty_reply, no_target, status_line
    finally:
        main.ask_hermes = orig_ask
        mission_control.manager = orig_manager


_seen, _reply, _empty_reply, _no_target, _status_line = asyncio.run(_exercise_followup_turn())
check(
    "followup routes into the mission session",
    len(_seen) == 1 and _seen[0][0] == "ft-sess"
    and "pod inspection" in _seen[0][1] and "any anomalies?" in _seen[0][1],
    repr(_seen),
)
check("followup answer comes back", "nothing unusual" in _reply)
check("empty followup asks for the question", "What shall I ask" in _empty_reply)
check(
    "followup without a steerable mission declines",
    "no recent mission" in _no_target,
    _no_target,
)
check(
    "status readout speaks the last mission",
    "pod inspection" in _status_line and "completed" in _status_line,
    _status_line,
)

# --- mission prompt context ------------------------------------------------------

_mp = main._mission_prompt("fix the CI", [
    {"role": "user", "content": "the ci is red"},
    {"role": "assistant", "content": "I see it, Dave."},
])
check(
    "mission prompt carries conversation context",
    "Mission: fix the CI" in _mp and "Dave: the ci is red" in _mp and "HAL: I see it, Dave." in _mp,
)
check("mission prompt tolerates empty history", "(no prior conversation)" in main._mission_prompt("x", []))

# --- running commentary: sentence assembly + chunk sink ---------------------------

_asm = main.SentenceAssembler()
check("assembler holds a partial sentence", _asm.feed("I am checking the ") == [])
check(
    "assembler emits on the boundary",
    _asm.feed("logs now. This may take") == ["I am checking the logs now."],
)
check("assembler flushes the tail", _asm.flush() == "This may take")
_asm2 = main.SentenceAssembler()
check(
    "assembler treats newlines as boundaries",
    _asm2.feed("First line\nSecond line. And") == ["First line", "Second line."],
)
_asm3 = main.SentenceAssembler()
check("assembler holds inside a code fence", _asm3.feed("Look: ```py\nx = 1. y = 2.") == [])
check(
    "assembler releases the closed fence whole",
    _asm3.feed("\n``` Done now.") == ["Look: ```py\nx = 1. y = 2.\n``` Done now."],
)
_asm4 = main.SentenceAssembler()
check(
    "assembler splits multiple sentences in one chunk",
    _asm4.feed("One. Two! Three? Four") == ["One.", "Two!", "Three?"],
)
_asm5 = main.SentenceAssembler()
_early_phrase = "I have checked every local voice component on the bridge, "
check(
    "assembler emits a sufficiently long phrase at a natural pause",
    _asm5.feed(_early_phrase)
    == ["I have checked every local voice component on the bridge,"],
)
_phrase_tail = "and the remaining systems are still responding normally."
check(
    "assembler completes the sentence after an early phrase",
    _asm5.feed(_phrase_tail)
    == ["and the remaining systems are still responding normally."],
)
_asm5_whole = main.SentenceAssembler()
check(
    "assembler keeps an already-complete sentence in one chunk",
    _asm5_whole.feed(_early_phrase + _phrase_tail)
    == [(_early_phrase + _phrase_tail).strip()],
)
check(
    "phrase streaming preserves the complete reply text",
    " ".join([
        "I have checked every local voice component on the bridge,",
        "and the remaining systems are still responding normally.",
    ])
    == (_early_phrase + _phrase_tail).strip(),
)
_asm6 = main.SentenceAssembler()
check("assembler does not split a short reply at a comma", _asm6.feed("Ready, ") == [])
check("assembler keeps the short reply whole", _asm6.feed("Dave.") == ["Ready, Dave."])
_asm7 = main.SentenceAssembler()
check(
    "phrase streaming still holds an open code fence",
    _asm7.feed(
        "I will keep this complete code sample out of the spoken reply: "
        "```txt\nalpha, beta; gamma:"
    )
    == [],
)
check(
    "phrase streaming releases a closed code fence whole",
    _asm7.feed("\n``` Finished.") == [
        "I will keep this complete code sample out of the spoken reply: "
        "```txt\nalpha, beta; gamma:\n``` Finished."
    ],
)
_asm8 = main.SentenceAssembler()
_bounded = _asm8.feed("word " * 50)
check(
    "assembler bounds long unpunctuated commentary at a word boundary",
    bool(_bounded)
    and all(len(part) <= main._COMMENTARY_PHRASE_MAX_CHARS for part in _bounded)
    and all(part.endswith("word") for part in _bounded),
    repr(_bounded),
)
_asm9 = main.SentenceAssembler()
check(
    "phrase streaming holds a comma inside inline code",
    _asm9.feed(
        "I have verified every relevant bridge value, including `alpha, beta"
    )
    == [],
)
check(
    "phrase streaming releases complete inline code at a later pause",
    _asm9.feed("`, and the remaining values are stable, ")
    == [
        "I have verified every relevant bridge value, including "
        "`alpha, beta`,"
    ],
)
_asm10 = main.SentenceAssembler()
check(
    "phrase streaming holds punctuation inside a Markdown link",
    _asm10.feed(
        "I have checked the detailed bridge reference "
        "[in the manual](https://example.test/alpha,beta"
    )
    == [],
)
check(
    "phrase streaming releases a complete Markdown link at a later pause",
    _asm10.feed("), and the documented behavior is correct, ")
    == [
        "I have checked the detailed bridge reference "
        "[in the manual](https://example.test/alpha,beta),"
    ],
)
_asm11 = main.SentenceAssembler()
check(
    "phrase streaming holds punctuation inside emphasis",
    _asm11.feed(
        "I have checked the detailed bridge state **including alpha, beta"
    )
    == [],
)
check(
    "phrase streaming releases closed emphasis at a later pause",
    _asm11.feed("**, and the remaining values are stable, ")
    == [
        "I have checked the detailed bridge state "
        "**including alpha, beta**,"
    ],
)
_asm12 = main.SentenceAssembler()
check(
    "phrase streaming holds punctuation inside strikethrough",
    _asm12.feed(
        "I have checked the detailed bridge state ~~including alpha, beta"
    )
    == [],
)
check(
    "phrase streaming releases closed strikethrough at a later pause",
    _asm12.feed("~~, and the remaining values are stable, ")
    == [
        "I have checked the detailed bridge state "
        "~~including alpha, beta~~,"
    ],
)
_asm13 = main.SentenceAssembler()
check(
    "phrase streaming holds a partial table row",
    _asm13.feed(
        "| Component | Detailed state, including the latest measurement"
    )
    == [],
)
check(
    "phrase streaming releases a complete table with following prose",
    _asm13.feed(" |\nThe remaining bridge systems are stable, ")
    == [
        "| Component | Detailed state, including the latest measurement |\n"
        "The remaining bridge systems are stable,"
    ],
)

check(
    "websocket frames preserve a client turn id",
    main._ws_frame("turn_done", 7) == {"type": "turn_done", "turn_id": 7},
)
check(
    "out-of-band websocket frames omit turn id",
    main._ws_frame("tts_done") == {"type": "tts_done"},
)


def _exercise_phrase_commentary_turn():
    original_run_turn = main.run_turn_text
    original_send_tts = main._ws_send_tts
    original_commentary = main.COMMENTARY
    reply = _early_phrase + _phrase_tail

    class _Socket:
        def __init__(self):
            self.frames: list[dict] = []
            self.spoken: list[str] = []

        async def send_json(self, payload):
            self.frames.append(payload)

    async def fake_run_turn(session_id, _user_text, _speaker):
        sink = hermes_bridge._commentary_sinks[session_id]
        sink(_early_phrase)
        sink(_phrase_tail)
        return reply, {"infer": 1}

    async def fake_send_tts(websocket, text, _turn_id=None):
        websocket.spoken.append(text)

    async def drive():
        websocket = _Socket()
        await main._ws_run_turn(
            websocket,
            "phrase-turn",
            "status",
            turn_id=42,
        )
        return websocket

    main.run_turn_text = fake_run_turn
    main._ws_send_tts = fake_send_tts
    main.COMMENTARY = True
    try:
        return asyncio.run(drive()), reply
    finally:
        main.run_turn_text = original_run_turn
        main._ws_send_tts = original_send_tts
        main.COMMENTARY = original_commentary


_phrase_socket, _phrase_reply = _exercise_phrase_commentary_turn()
_phrase_frames = [
    frame["text"]
    for frame in _phrase_socket.frames
    if frame.get("type") == "commentary"
]
_final_frames = [
    frame["text"]
    for frame in _phrase_socket.frames
    if frame.get("type") == "transcript" and frame.get("role") == "hal"
]
check(
    "commentary turn speaks the early phrase and remaining sentence",
    _phrase_frames == [
        "I have checked every local voice component on the bridge,",
        "and the remaining systems are still responding normally.",
    ],
    repr(_phrase_frames),
)
check(
    "commentary keeps the final transcript exact",
    _final_frames == [_phrase_reply],
    repr(_final_frames),
)
check(
    "commentary frames retain the client turn id",
    all(frame.get("turn_id") == 42 for frame in _phrase_socket.frames),
    repr(_phrase_socket.frames),
)


def _exercise_commentary_sink():
    client = hermes_bridge._HALClient(None)
    hermes_bridge._acp_to_cookie["acp-comm"] = "cookie-comm"
    heard: list[str] = []
    hermes_bridge.set_commentary_sink("cookie-comm", heard.append)

    class _Chunk:
        session_update = "agent_message_chunk"

        class content:
            text = "Hello, Dave. "

    async def drive():
        client.begin("acp-comm")
        await client.session_update("acp-comm", _Chunk())
        await client.session_update("acp-comm", _Chunk())
        hermes_bridge.clear_commentary_sink("cookie-comm")
        await client.session_update("acp-comm", _Chunk())
        return client.finish("acp-comm")

    reply = asyncio.run(drive())
    hermes_bridge._acp_to_cookie.pop("acp-comm", None)
    return heard, reply


_heard, _reply = _exercise_commentary_sink()
check(
    "commentary sink hears chunks while registered",
    _heard == ["Hello, Dave. ", "Hello, Dave. "],
    repr(_heard),
)
check(
    "buffered reply is unaffected by the sink",
    _reply == "Hello, Dave. Hello, Dave. Hello, Dave.",
    repr(_reply),
)


def _exercise_available_commands_update():
    client = hermes_bridge._HALClient(None)

    class _Input:
        hint = "model name"

    class _InputWrapper:
        root = _Input()

    class _Command:
        name = "model"
        description = "Switch model"
        input = _InputWrapper()

    class _CommandsUpdate:
        session_update = "available_commands_update"
        available_commands = [_Command()]

    async def drive():
        await client.session_update("acp-commands", _CommandsUpdate())
        return client.commands("acp-commands")

    return asyncio.run(drive())


_advertised_commands = _exercise_available_commands_update()
check(
    "ACP available-command metadata is retained for the composer",
    _advertised_commands == [{
        "name": "model",
        "description": "Switch model",
        "input_hint": "model name",
        "source": "hermes",
    }],
    repr(_advertised_commands),
)


def _exercise_exact_slash_routing():
    original_ask = main.ask_hermes
    original_drain = main.mission_control.manager.drain_notes
    original_daily = main.ledger.manager.daily_note
    prompts: list[str] = []
    notes_touched: list[bool] = []

    async def fake_ask(text, _session_id):
        prompts.append(text)
        return "Available commands."

    def fake_drain(_session_id):
        notes_touched.append(True)
        return ["A completed mission report."]

    def fake_daily():
        notes_touched.append(True)
        return "A ledger note."

    main.ask_hermes = fake_ask
    main.mission_control.manager.drain_notes = fake_drain
    main.ledger.manager.daily_note = fake_daily
    try:
        asyncio.run(main.run_turn_text("slash-routing", "/help"))
    finally:
        main.ask_hermes = original_ask
        main.mission_control.manager.drain_notes = original_drain
        main.ledger.manager.daily_note = original_daily
    return prompts, notes_touched


_slash_prompts, _slash_notes_touched = _exercise_exact_slash_routing()
check(
    "slash command reaches Hermes as the exact ACP prompt",
    _slash_prompts == ["/help"],
    repr(_slash_prompts),
)
check(
    "slash command does not consume pending conversation notes",
    _slash_notes_touched == [],
    repr(_slash_notes_touched),
)

_sink_a, _sink_b = (lambda t: None), (lambda t: None)
hermes_bridge.set_commentary_sink("race-ck", _sink_a)
hermes_bridge.set_commentary_sink("race-ck", _sink_b)  # barge-in turn replaces
hermes_bridge.clear_commentary_sink("race-ck", _sink_a)  # old turn's finally
check(
    "stale sink clear leaves the newer turn's sink",
    hermes_bridge._commentary_sinks.get("race-ck") is _sink_b,
)
hermes_bridge.clear_commentary_sink("race-ck", _sink_b)
check("owner clear removes the sink", "race-ck" not in hermes_bridge._commentary_sinks)

# --- bridge polish: boot ritual, latency ring, vitals triggers ----------------------

check(
    "boot ritual line reads like HAL",
    "Boot sequence complete" in main._boot_ritual_line()
    and "functional" in main._boot_ritual_line(),
)

main._recent_timings.clear()
for _i in range(50):
    main._record_timing({"infer": 100 + _i, "turn": 200 + _i})
check("latency ring is bounded", len(main._recent_timings) == 40)
check(
    "latency ring keeps the newest",
    main._recent_timings[-1]["infer"] == 149 and main._recent_timings[-1]["turn"] == 249,
)
main._record_timing({"stt": 41, "infer": 149, "total": 191})
check(
    "latency ring keeps stage detail",
    main._recent_timings[-1]["stt"] == 41 and main._recent_timings[-1]["total"] == 191,
)
main._record_timing({})
check("empty timings are not recorded", main._recent_timings[-1]["infer"] == 149)
main._recent_timings.clear()

main._STT_LOCK.acquire()
try:
    _interim_started = time.perf_counter()
    _interim_text = main.transcribe(b"", beam_size=1, wait_for_lock=False)
    _interim_elapsed = time.perf_counter() - _interim_started
finally:
    main._STT_LOCK.release()
check(
    "interim STT never queues behind a busy final decode",
    _interim_text == "" and _interim_elapsed < 0.05,
)

check("vitals parse valid", mc._parse_vitals({"disk_free_gb_below": 20}) == {"disk_free_gb_below": 20.0})
check("vitals parse rejects junk", mc._parse_vitals({"disk_free_gb_below": "lots"}) is None)
check("vitals parse rejects non-dict", mc._parse_vitals("20") is None)


async def _exercise_vitals():
    orig_ask = hermes_bridge.ask_hermes
    orig_disk = mc._disk_free_gb
    orig_batt = mc._battery_percent

    async def fake_ask(text, sid):
        return "vitals checked"

    hermes_bridge.ask_hermes = fake_ask
    disk_value = [50.0]
    mc._disk_free_gb = lambda: disk_value[0]
    mc._battery_percent = lambda: None
    try:
        tdir = Path(_tmp.name) / "missions-vitals"
        mgr = mc.MissionManager(tdir)
        (tdir / "triggers.json").write_text(json.dumps([
            {"title": "vitals", "prompt": "check", "vitals": {"disk_free_gb_below": 20}},
        ]))
        state: dict = {}
        mgr._scheduler_tick(state, now=1000.0)            # healthy — no fire
        healthy = len(mgr.missions)
        disk_value[0] = 8.0
        mgr._scheduler_tick(state, now=1030.0)            # crossed — fires
        crossed = len(mgr.missions)
        mgr._scheduler_tick(state, now=1060.0)            # still bad — no refire
        held = len(mgr.missions)
        mgr._scheduler_tick(state, now=1060.0 + mc.VITALS_REALERT + 1)  # re-alert
        realerted = len(mgr.missions)
        disk_value[0] = 50.0
        mgr._scheduler_tick(state, now=2000.0 + mc.VITALS_REALERT)      # recovered
        disk_value[0] = 8.0
        mgr._scheduler_tick(state, now=2030.0 + mc.VITALS_REALERT)      # crosses again
        recrossed = len(mgr.missions)
        await asyncio.gather(*list(mgr._tasks))
        annotated = any("(Trigger: disk free" in m.prompt for m in mgr.missions.values())
        return healthy, crossed, held, realerted, recrossed, annotated
    finally:
        hermes_bridge.ask_hermes = orig_ask
        mc._disk_free_gb = orig_disk
        mc._battery_percent = orig_batt


(_healthy, _crossed, _held, _realerted, _recrossed, _annotated) = asyncio.run(_exercise_vitals())
check("vitals quiet while healthy", _healthy == 0)
check("vitals fire on crossing", _crossed == 1)
check("vitals hold while still breached", _held == 1)
check("vitals re-alert after the cooldown", _realerted == 2)
check("vitals re-fire after recovery", _recrossed == 3)
check("vitals prompts carry the breach details", _annotated)

# --- the initiative: proposal marker, lifecycle, voice answer -----------------------

_prop_text = main._extract_proposal(
    "prop-sess",
    "The registrar expires soon, Dave. Shall I handle it?\n"
    "PROPOSE_MISSION: Renew the domain ::: Renew dave.example at the registrar.",
)
check(
    "proposal marker is stripped from speech",
    _prop_text == "The registrar expires soon, Dave. Shall I handle it?",
    _prop_text,
)
_prop = main._pending_proposal("prop-sess")
check(
    "proposal is registered pending",
    _prop is not None and _prop["title"] == "Renew the domain"
    and "registrar" in _prop["prompt"],
)
check(
    "reply without marker is untouched",
    main._extract_proposal("prop-sess2", "All quiet, Dave.") == "All quiet, Dave."
    and main._pending_proposal("prop-sess2") is None,
)
_prop["created_at"] -= main.PROPOSAL_TTL + 1
check("proposals expire", main._pending_proposal("prop-sess") is None)


async def _exercise_proposals():
    orig_ask = hermes_bridge.ask_hermes
    orig_manager = mission_control.manager

    async def fake_ask(text, sid):
        return "done"

    hermes_bridge.ask_hermes = fake_ask
    try:
        mission_control.manager = mc.MissionManager(Path(_tmp.name) / "missions-prop")
        main._register_proposal("prop-yes", "Scan the hull", "scan it", source="brain")
        approved = main._proposal_reply("prop-yes", "go ahead", speaker=None)
        created = [m.title for m in mission_control.manager.missions.values()]
        main._register_proposal("prop-no", "Vent the pod bay", "vent", source="brain")
        declined = main._proposal_reply("prop-no", "no", speaker=None)
        not_created = [m.title for m in mission_control.manager.missions.values()]
        silent = main._proposal_reply("prop-none", "yes", speaker=None)
        # Approval at the mission cap must keep the proposal pending — the
        # cap is per session, and no await runs between creations, so the
        # fake missions stay "active" until gathered below.
        for i in range(mc.MAX_ACTIVE_MISSIONS):
            main._register_proposal("cap-sess", f"filler {i}", "fill", source="brain")
            main._proposal_reply("cap-sess", "yes", speaker=None)
        main._register_proposal("cap-sess", "One too many", "overflow", source="brain")
        capped_reply = main._proposal_reply("cap-sess", "yes", speaker=None)
        still_pending = main._pending_proposal("cap-sess") is not None
        main._pending_proposals.clear()
        await asyncio.gather(*list(mission_control.manager._tasks))
        return approved, created, declined, not_created, silent, capped_reply, still_pending
    finally:
        hermes_bridge.ask_hermes = orig_ask
        mission_control.manager = orig_manager


(_approved, _created, _declined, _not_created, _silent,
 _capped_reply, _still_pending) = asyncio.run(_exercise_proposals())
check(
    "approval at the mission cap keeps the proposal pending",
    _capped_reply is not None and "as many missions" in _capped_reply and _still_pending,
    repr((_capped_reply, _still_pending)),
)
check(
    "voice yes approves the proposal into a mission",
    _approved is not None and "Mission underway" in _approved and _created == ["Scan the hull"],
    repr((_approved, _created)),
)
check(
    "voice no declines without creating",
    _declined is not None and "leave it" in _declined and "Vent the pod bay" not in _not_created,
)
check("no pending proposal means no answer", _silent is None)


def _exercise_daily_initiative():
    import ledger as ledger_mod_local
    orig_ledger = ledger_mod_local.manager
    ldir = Path(_tmp.name) / "ledger-initiative"
    ldir.mkdir(exist_ok=True)
    ledger_mod_local.manager = ledger_mod_local.Ledger(ldir)
    ledger_mod_local.manager.add("renew the domain", due="2020-01-01")
    try:
        main._INITIATIVE_STATE.unlink(missing_ok=True)
        first = main._daily_initiative("init-sess")
        pending = main._pending_proposal("init-sess")
        again = main._daily_initiative("init-sess-2")
        return first, pending, again
    finally:
        ledger_mod_local.manager = orig_ledger
        main._pending_proposals.pop("init-sess", None)


_first_offer, _init_pending, _again = _exercise_daily_initiative()
check(
    "overdue ledger prompts a daily offer",
    _first_offer is not None and "renew the domain" in _first_offer
    and _init_pending is not None,
    repr(_first_offer),
)
check("the initiative offers once per day", _again is None)

# --- care ledger: commands, storage, daily note -------------------------------------

import ledger as ledger_mod  # noqa: E402

check("ledger add voice form", main._ledger_add_request("HAL, remember to renew the domain.") == "to renew the domain")
check("ledger add that-form", main._ledger_add_request("Hal, remember that Frank owes me a report") == "Frank owes me a report")
check("ledger add typed", main._ledger_add_request("/remember buy coffee") == "buy coffee")
check("ledger add negative", main._ledger_add_request("HAL, do you remember the mission?") is None)
check("ledger query matches", main._LEDGER_QUERY_RE.match("HAL, what's on my ledger?") is not None)
check("ledger query loops form", main._LEDGER_QUERY_RE.match("Hal, what are my open loops?") is not None)
check("ledger done bare", main._LEDGER_DONE_RE.match("HAL, that's done.") is not None)
check("ledger done named", main._LEDGER_DONE_RE.match("HAL, mark the domain as done").group(1) == "the domain")
check("ledger forget bare", main._LEDGER_FORGET_RE.match("HAL, forget that.") is not None)
check("ledger forget about", main._LEDGER_FORGET_RE.match("Hal, forget the one about coffee").group(1) == "coffee")
check("voice-forget still wins", main._FORGET_VOICE_RE.match("HAL, forget Frank's voice") is not None)

_ldir = Path(_tmp.name) / "ledger-data"
_ldir.mkdir()
_led = ledger_mod.Ledger(_ldir)
_led.add("renew the domain", due="2020-01-01")
_led.add("call Frank about the server")
_led.add("draft the report")
check("ledger orders due first", _led.open_entries()[0]["text"] == "renew the domain")
check("ledger due_today catches overdue", [e["text"] for e in _led.due_today()] == ["renew the domain"])
_sum = _led.spoken_summary()
check("ledger summary speaks counts and dues", "3 items" in _sum and "Due now: renew the domain" in _sum, _sum)
# The spoken budget is a total, not a per-section one: more overdue items
# than MAX_SPOKEN_ITEMS must silence the "Open:" section entirely, not slice
# it from the end (a bare negative slice did exactly that).
_lbudget = ledger_mod.Ledger(_ldir / "budget")
(_ldir / "budget").mkdir()
for _i in range(ledger_mod.MAX_SPOKEN_ITEMS + 1):
    _lbudget.add(f"overdue {_i}", due="2020-01-01")
for _i in range(3):
    _lbudget.add(f"undated {_i}")
_bsum = _lbudget.spoken_summary()
check("ledger summary drops open items once dues fill the budget", "Open:" not in _bsum, _bsum)
check("ledger summary caps spoken items at the budget",
      _bsum.count(";") + 1 <= ledger_mod.MAX_SPOKEN_ITEMS, _bsum)
check("ledger summary still reports the true total", "10 items" in _bsum, _bsum)

check("ledger complete by query", _led.complete("domain")["text"] == "renew the domain")
check("ledger complete keeps the record", any(e["status"] == "done" for e in _led._load()))
check("ledger forget latest", _led.forget(None)["text"] == "draft the report")
check("ledger forget by query", _led.forget("frank")["text"] == "call Frank about the server")
check("ledger empty summary", "clear" in ledger_mod.Ledger(Path(_tmp.name) / "ledger-empty").spoken_summary())
(_ldir / "ledger.json").write_text("{broken")
check("ledger tolerates corrupt file", ledger_mod.Ledger(_ldir).open_entries() == [])

_led2 = ledger_mod.Ledger(Path(_tmp.name) / "ledger-note")
(Path(_tmp.name) / "ledger-note").mkdir(exist_ok=True)
_led2.add("file taxes", due="2020-01-01")
_first_note = _led2.daily_note()
check("ledger daily note mentions due items", _first_note is not None and "file taxes" in _first_note)
check("ledger daily note fires once per day", _led2.daily_note() is None)

# --- crew manifest: enrollment grammar, profiles, permission gating ----------------

import numpy as _np  # noqa: E402
import speaker_id  # noqa: E402

check("enroll matches this-is", main._enroll_request("HAL, this is Frank.") == "Frank")
check("enroll matches my-name-is", main._enroll_request("Hal, my name is Dr. Chandra") == "Dr. Chandra")
check("enroll survives HAL misheard as Hell", main._enroll_request("Hell, this is Sam.") == "Sam")
check("enroll rejects adjectives", main._enroll_request("HAL, this is ridiculous!") is None)
check("enroll rejects stopword names", main._enroll_request("HAL, this is Important.") is None)
check("enroll rejects long phrases", main._enroll_request("HAL, this is a real problem for us") is None)
check("forget-voice matches", main._FORGET_VOICE_RE.match("HAL, forget Frank's voice.") is not None)

_smgr = speaker_id.SpeakerID(Path(_tmp.name) / "speakers")
(Path(_tmp.name) / "speakers").mkdir(exist_ok=True)
_fake_vectors = {
    b"dave-audio": _np.array([1.0, 0.0, 0.0], dtype=_np.float32),
    b"frank-audio": _np.array([0.0, 1.0, 0.0], dtype=_np.float32),
    b"davish-audio": _np.array([0.9, 0.1, 0.0], dtype=_np.float32),
    b"noise": _np.array([0.3, 0.3, 0.906], dtype=_np.float32),
}
_smgr.embed = lambda audio: _fake_vectors.get(audio)  # inject: no model in tests

check("enroll stores a profile", _smgr.enroll("dave", b"dave-audio") and _smgr.enrolled())
check("first enrollment takes command", _smgr.commander() == "Dave")
check("second enrollment does not", _smgr.enroll("Frank", b"frank-audio") and _smgr.commander() == "Dave")
_who, _score = _smgr.identify(b"davish-audio")
check("identify matches the near voice", _who == "Dave" and _score > 0.8, repr((_who, _score)))
_who, _score = _smgr.identify(b"noise")
check("identify rejects below threshold", _who is None, repr((_who, _score)))
_smgr2 = speaker_id.SpeakerID(Path(_tmp.name) / "speakers")
check("profiles persist across managers", _smgr2.commander() == "Dave" and _smgr2.names() == ["Dave", "Frank"])
check("forget reassigns command", _smgr.forget("Dave") and _smgr.commander() == "Frank")
check("forget unknown is false", not _smgr.forget("Nobody"))


async def _exercise_voice_gating():
    orig = speaker_id.manager
    speaker_id.manager = _smgr  # commander is Frank now
    try:
        req_id, fut = hermes_bridge._register_permission("gate-sess", "Run rm")
        refused = main._permission_reply("gate-sess", "go ahead", speaker="")
        refused_named = main._permission_reply("gate-sess", "yes", speaker="Dave")
        typed_ok = main._permission_reply("gate-sess", "yes", speaker=None)
        value = await fut
        hermes_bridge._pending_permissions.pop(req_id, None)
        return refused, refused_named, typed_ok, value
    finally:
        speaker_id.manager = orig


_refused, _refused_named, _typed_ok, _value = asyncio.run(_exercise_voice_gating())
check(
    "unknown voice cannot approve permissions",
    _refused is not None and "Only Frank" in _refused,
    repr(_refused),
)
check("non-commander voice cannot approve", _refused_named is not None and "Only Frank" in _refused_named)
check("typed approval still works", _typed_ok == "Very well, Dave. Proceeding." and _value is True)

# --- viewscreen: listing + broadcast ------------------------------------------------

import time as _time_mod  # noqa: E402

(main.VIEWSCREEN_DIR / "old_chart.png").write_bytes(b"png")
_now = _time_mod.time()
os.utime(main.VIEWSCREEN_DIR / "old_chart.png", (_now - 100, _now - 100))
(main.VIEWSCREEN_DIR / "fresh_page.html").write_text("<b>hi</b>")
(main.VIEWSCREEN_DIR / "notes.txt").write_text("not a visual")
_vs = main._viewscreen_items()
check(
    "viewscreen lists supported files newest first",
    [i["name"] for i in _vs] == ["fresh_page.html", "old_chart.png"],
    repr(_vs),
)
check("viewscreen ignores unsupported extensions", all(i["name"] != "notes.txt" for i in _vs))

_q1 = hermes_bridge.register_event_queue("vs-browser-1")
_q2 = hermes_bridge.register_event_queue("vs-browser-2")
hermes_bridge.publish_event_all({"type": "viewscreen", "name": "fresh_page.html", "count": 2})
check(
    "viewscreen broadcast reaches every session",
    not _q1.empty() and not _q2.empty() and "fresh_page" in _q1.get_nowait(),
)
hermes_bridge.unregister_event_queue("vs-browser-1", _q1)
hermes_bridge.unregister_event_queue("vs-browser-2", _q2)

# --- cross-origin defence ------------------------------------------------------------
# TrustedHostMiddleware stops DNS rebinding but not the plainer attack: any page
# you are visiting can fetch() /api/talk. Multipart is CORS-safelisted (no
# preflight) and the handler mints a session when the cookie is absent, so
# SameSite=lax does not help. Origin is the boundary.

check("same-origin request allowed",
      main._origin_allowed("http://127.0.0.1:8000", "same-origin"))
check("localhost origin allowed",
      main._origin_allowed("http://localhost:8000", "same-origin"))
check("foreign origin blocked",
      not main._origin_allowed("https://evil.example", None))
check("foreign origin blocked even on the right port",
      not main._origin_allowed("https://evil.example:8000", None))
check("Sec-Fetch-Site: cross-site blocked regardless of Origin",
      not main._origin_allowed("http://127.0.0.1:8000", "cross-site"))
check("opaque origin blocked (sandboxed iframe / data: URL)",
      not main._origin_allowed("null", None))
# curl, bin/hermes-hal and smoke.sh send no Origin; a browser always does on unsafe
# methods, so absence means no browser is being used as a confused deputy.
check("originless client still allowed (curl, bin/hermes-hal, smoke.sh)",
      main._origin_allowed(None, None))

_saved_hosts = list(main.ALLOWED_HOSTS)
main.ALLOWED_HOSTS[:] = ["*"]
check("wildcard host allowlist opts out of the origin check",
      main._origin_allowed("https://anything.example", None))
main.ALLOWED_HOSTS[:] = _saved_hosts
check("allowlist restored", main.ALLOWED_HOSTS == _saved_hosts)

check("GET is exempt (safe, and unreadable cross-origin anyway)",
      "GET" not in main._UNSAFE_METHODS and "POST" in main._UNSAFE_METHODS)


async def _exercise_cross_origin_asgi():
    """The middleware must reject at the ASGI layer, for POST *and* sockets."""
    sent: list[dict] = []

    async def _send(message):
        sent.append(message)

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _unreachable(scope, receive, send):  # the app behind the guard
        sent.append({"type": "REACHED_APP"})

    guard = main.SameOriginMiddleware(_unreachable)
    evil = [(b"origin", b"https://evil.example")]
    good = [(b"origin", b"http://127.0.0.1:8000")]

    await guard({"type": "http", "method": "POST", "path": "/api/talk", "headers": evil},
                _receive, _send)
    blocked_post = [m for m in sent if m.get("type") == "http.response.start"]
    reached = any(m.get("type") == "REACHED_APP" for m in sent)

    sent.clear()
    await guard({"type": "websocket", "path": "/ws/conversation", "headers": evil},
                _receive, _send)
    closed_ws = [m for m in sent if m.get("type") == "websocket.close"]

    sent.clear()
    await guard({"type": "http", "method": "POST", "path": "/api/talk", "headers": good},
                _receive, _send)
    same_origin_ok = any(m.get("type") == "REACHED_APP" for m in sent)

    sent.clear()
    await guard({"type": "http", "method": "GET", "path": "/api/health", "headers": evil},
                _receive, _send)
    get_ok = any(m.get("type") == "REACHED_APP" for m in sent)

    return blocked_post, reached, closed_ws, same_origin_ok, get_ok


_xo_post, _xo_reached, _xo_ws, _xo_same, _xo_get = asyncio.run(_exercise_cross_origin_asgi())
check("cross-origin POST is rejected with 403",
      len(_xo_post) == 1 and _xo_post[0]["status"] == 403, repr(_xo_post))
check("cross-origin POST never reaches the app", not _xo_reached)
check("cross-origin WebSocket handshake is closed",
      len(_xo_ws) == 1 and _xo_ws[0].get("code") == 1008, repr(_xo_ws))
check("same-origin POST passes through", _xo_same)
check("cross-origin GET passes through (safe method)", _xo_get)

# --- viewscreen: agent-written files stay inert -------------------------------------
# The panel sandboxes agent HTML in an iframe, but links images full-size —
# and a top-level SVG document runs its scripts with this app's origin and
# session cookie. The response headers, not the markup, are the boundary.

check("viewscreen svg is sandboxed",
      main._viewscreen_headers("chart.svg").get("Content-Security-Policy") == "sandbox")
check("viewscreen html is sandboxed",
      main._viewscreen_headers("page.html").get("Content-Security-Policy") == "sandbox")
check("viewscreen pdf keeps its viewer origin",
      "Content-Security-Policy" not in main._viewscreen_headers("report.pdf"))
check("viewscreen pdf carve-out is case-insensitive",
      "Content-Security-Policy" not in main._viewscreen_headers("REPORT.PDF"))
check("viewscreen responses refuse content sniffing",
      all(main._viewscreen_headers(n)["X-Content-Type-Options"] == "nosniff"
          for n in ("chart.svg", "report.pdf", "shot.png")))


async def _exercise_viewscreen_static():
    """The headers must survive StaticFiles, not just the helper."""
    static = main._ViewscreenStatic(directory=str(main.VIEWSCREEN_DIR))
    scope = {"type": "http", "method": "GET", "headers": []}
    (main.VIEWSCREEN_DIR / "probe.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
    served = await static.get_response("probe.svg", scope)
    try:
        # StaticFiles signals a miss by raising, not returning — either way
        # the override must not turn it into a 200.
        missing = (await static.get_response("nope.svg", scope)).status_code
    except Exception as exc:
        missing = getattr(exc, "status_code", None)
    return served.headers, missing


_vs_headers, _vs_missing_status = asyncio.run(_exercise_viewscreen_static())
check("served viewscreen file carries the sandbox header",
      _vs_headers.get("content-security-policy") == "sandbox", dict(_vs_headers))
check("served viewscreen file carries nosniff",
      _vs_headers.get("x-content-type-options") == "nosniff", dict(_vs_headers))
check("viewscreen static still 404s a missing file", _vs_missing_status == 404, _vs_missing_status)

# --- chess engine (clean-room; perft pins move generation) -------------------------

import chess_engine as ce  # noqa: E402
import chess_control  # noqa: E402

check("chess fen roundtrip", ce.Board.from_fen(ce.START_FEN).to_fen() == ce.START_FEN)
check("chess perft startpos d3", ce.perft(ce.Board.start(), 3) == 8902)
_kiwi = ce.Board.from_fen("r3k2r/p1ppqpb1/bn2pnp1/3PN3/1p2P3/2N2Q1p/PPPBBPPP/R3K2R w KQkq - 0 1")
check("chess perft kiwipete d2 (castling)", ce.perft(_kiwi, 2) == 2039)
_pos3 = ce.Board.from_fen("8/2p5/3p4/KP5r/1R3p1k/8/4P1P1/8 w - - 0 1")
check("chess perft position-3 d3 (en passant)", ce.perft(_pos3, 3) == 2812)

_mate1 = ce.Board.from_fen("r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5Q2/PPPP1PPP/RNB1K1NR w KQkq - 0 1")
_mv = ce.best_move(_mate1, depth=3)
check(
    "chess engine finds mate in one",
    _mv is not None and ce.move_uci(_mv) == "f3f7" and ce.san(_mate1, _mv) == "Qxf7#",
)
_stale = ce.Board.from_fen("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
check("chess stalemate detected", not _stale.legal_moves() and not _stale.in_check())

# --- chess control: spoken/typed parsing --------------------------------------------

_cmgr = chess_control.ChessManager(Path(_tmp.name) / "chess-data")
_cgame, _cline = _cmgr.new_game("chess-ck", "w")
check("chess new game speaks", "Your move" in _cline and _cgame["status"] == "active")

_k, _m = _cmgr.resolve(_cgame, "Knight to f3", typed=False)
check("chess spoken piece move", _k == "move" and ce.move_uci(_m) == "g1f3")
_k, _m = _cmgr.resolve(_cgame, "e4", typed=False)
check("chess bare square is a pawn move", _k == "move" and ce.move_uci(_m) == "e2e4")
_k, _m = _cmgr.resolve(_cgame, "E two to E four.", typed=False)
check("chess spoken from-to with number words", _k == "move" and ce.move_uci(_m) == "e2e4")
_k, _m = _cmgr.resolve(_cgame, "Nf3", typed=True)
check("chess typed SAN", _k == "move" and ce.move_uci(_m) == "g1f3")
check("chess ignores plain talk", _cmgr.resolve(_cgame, "what's our status?", typed=False) is None)
check(
    "chess ignores square-like tokens in conversation",
    _cmgr.resolve(_cgame, "meet me at gate b4 after lunch", typed=False) is None,
)
_k, _m = _cmgr.resolve(_cgame, "HAL, e4.", typed=False)
check("chess bare square with address", _k == "move" and ce.move_uci(_m) == "e2e4")
check("chess typed plain talk ignored", _cmgr.resolve(_cgame, "check the logs", typed=True) is None)
_k, _m = _cmgr.resolve(_cgame, "knight to e4", typed=False)
check("chess illegal attempt flagged", _k == "illegal", repr((_k, _m)))

_twoknights = dict(_cgame, fen="k7/8/8/8/8/2N1N3/8/K7 w - - 0 1")
_k, _m = _cmgr.resolve(_twoknights, "knight to d5", typed=False)
check("chess ambiguous move detected", _k == "ambiguous" and len(_m) == 2)
_castled = dict(_cgame, fen="r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
_k, _m = _cmgr.resolve(_castled, "castle kingside", typed=False)
check("chess spoken castling", _k == "move" and ce.move_uci(_m) == "e1g1")

# --- chess control: game flow --------------------------------------------------------

_reply = _cmgr.advance("chess-ck", _cgame, (ce.parse_square("e2"), ce.parse_square("e4"), ""))
check(
    "chess advance plays both sides",
    len(_cgame["moves"]) == 2 and _cgame["status"] == "active" and _reply,
    repr((_cgame["moves"], _reply)),
)

_dave_mates, _ = _cmgr.new_game("chess-mate", "w")
_dave_mates["fen"] = "r1bqkbnr/pppp1ppp/2n5/4p3/2B1P3/5Q2/PPPP1PPP/RNB1K1NR w KQkq - 0 1"
_k, _m = _cmgr.resolve(_dave_mates, "queen takes f7", typed=False)
_verdict = _cmgr.advance("chess-mate", _dave_mates, _m)
check(
    "chess recognizes Dave's mate",
    "Checkmate" in _verdict and _dave_mates["outcome"] == "dave_wins",
    _verdict,
)

# Fool's mate: after 1.f3 e5, Dave plays g4 and HAL must find Qh4#.
_hal_mates, _ = _cmgr.new_game("chess-fools", "w")
_hal_mates["fen"] = "rnbqkbnr/pppp1ppp/8/4p3/8/5P2/PPPPP1PP/RNBQKBNR w KQkq - 0 2"
_verdict = _cmgr.advance("chess-fools", _hal_mates, (ce.parse_square("g2"), ce.parse_square("g4"), ""))
check(
    "chess HAL delivers the film line on mate",
    "I think you missed it" in _verdict and _hal_mates["outcome"] == "hal_wins",
    _verdict,
)

check("chess resign", _cmgr.resign("chess-ck") is not None)
check("chess resign twice is a no-op", _cmgr.resign("chess-ck") is None)
check("chess persists across managers",
      chess_control.ChessManager(Path(_tmp.name) / "chess-data").load("chess-ck")["status"] == "finished")

# --- chess main wiring: start/resign grammar ----------------------------------------

check("chess start voice", main._CHESS_START_RE.match("HAL, let's play chess.") is not None)
check("chess start play form", main._CHESS_START_RE.match("Hal, play a game of chess") is not None)
check("chess start rejects chat about chess",
      main._CHESS_START_RE.match("HAL, who invented chess?") is None)
check("chess resign voice", main._CHESS_RESIGN_RE.match("HAL, I resign.") is not None)

# --- session event journal -------------------------------------------------------

main._log_session_event("evt-sess", {"type": "tool_call_update", "status": "completed", "title": "Read file"})
main._log_session_event("evt-sess", {"type": "tool_call", "status": "pending", "title": "transient — skip"})
main._log_session_event("evt-sess", {"type": "mission_update", "mission": {"id": "m", "title": "t", "status": "completed", "prompt": "HUGE" * 999}})
_evs = main.load_events("evt-sess")
check("journal keeps terminal events only", len(_evs) == 2 and _evs[0]["title"] == "Read file", repr(_evs))
check("journaled mission events are slimmed", "prompt" not in _evs[1]["mission"] and _evs[1]["mission"]["title"] == "t")
check("journal missing session is empty", main.load_events("nope-evt") == [])
check("journal has timestamps", all(e.get("ts") for e in _evs))

# --- frontend duplex WS busy-state invariant ---------------------------------
# static/index.html has no JS test harness (zero-dependency Python suite), but
# a regression here is easy to reintroduce: if a duplex utterance starts
# recording (busy=true) without marking wsTurn, a socket drop mid-recording
# skips ws.onclose's unlock branch and wedges the UI (busy/isWsRecording stuck
# true forever, mic dead until reload). Assert the ordering statically instead.

_frontend_src = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text()


def _fn_body(name: str) -> str:
    start = _frontend_src.index(f"function {name}(")
    # crude brace match from the first '{' after the signature
    brace_start = _frontend_src.index("{", start)
    depth = 0
    for i in range(brace_start, len(_frontend_src)):
        if _frontend_src[i] == "{":
            depth += 1
        elif _frontend_src[i] == "}":
            depth -= 1
            if depth == 0:
                return _frontend_src[brace_start:i + 1]
    raise AssertionError(f"unbalanced braces scanning function {name}")


start_ws_recording = _fn_body("startWsRecording")
check(
    "startWsRecording marks wsTurn so a mid-recording disconnect can unlock",
    "wsTurn = true" in start_ws_recording,
    start_ws_recording,
)

# --- mission log entry shape -------------------------------------------------
# A direction may lay .mlog-entry out as a grid (01 does: 70px + 1fr). An
# unwrapped message is then an *anonymous* grid item, auto-placed into the next
# free cell — under the timestamp, one word wide. That shipped broken on the
# default direction, so pin the two-child shape: .mlog-ts then .mlog-body.
_mlog_body = _fn_body("appendToMissionLog")
_mlog_assignments = [
    line.strip() for line in _mlog_body.splitlines() if "el.innerHTML =" in line
]
check(
    "mission log builds every entry type",
    len(_mlog_assignments) == 4,
    repr(_mlog_assignments),
)
check(
    "every mission log entry wraps its message in .mlog-body",
    all("body(" in line for line in _mlog_assignments),
    repr([line for line in _mlog_assignments if "body(" not in line]),
)
check(
    "the grid direction places .mlog-body in column 2",
    ".mlog-body" in (_STATIC := Path(__file__).resolve().parent.parent / "static")
    .joinpath("bridge-option1.css").read_text(),
)

# The JS emits `mlog-user`; two direction stylesheets used to select `mlog-you`,
# so user entries silently lost their signal bullet. Neither name may drift.
for _css in sorted(_STATIC.glob("bridge-*.css")):
    check(
        f"{_css.name} has no dead .mlog-you selector",
        "mlog-you" not in _css.read_text(),
    )

ws_onclose_start = _frontend_src.index("ws.onclose = () => {")
ws_onclose_end = _frontend_src.index("};", ws_onclose_start)
ws_onclose_body = _frontend_src[ws_onclose_start:ws_onclose_end]
check(
    "ws.onclose stops a live duplex recorder before unlocking",
    "isWsRecording" in ws_onclose_body and "vadUtteranceChunks" in ws_onclose_body,
    ws_onclose_body,
)

# Frontend must speak the backend's protocol: these strings are the contract.
for token in (
    'id="permbar"',            # permission Allow/Deny UI exists
    "/api/permission/",        # …and posts decisions to the endpoint
    "permission_request",      # …driven by the SSE event
    'id="missions-panel"',     # mission cards panel exists
    "/api/missions",           # …seeded from the missions endpoint
    "mission_session",         # tool events attributed to mission cards
    "set_mode",                # wake-word toggle frame
    "interim_transcript",      # live caption frames handled
    "no_wake_word",            # gated utterances stay silent
    "announce_ready",          # queued trigger reports delivered post-gesture
    'data-act="cancel"',       # mission cards can cancel a running mission
    'data-act="dismiss"',      # …and dismiss a finished one
    "st-cancelled",            # cancelled status is styled
    'id="chess-panel"',        # chess board panel exists
    "/api/chess/",             # …and drives the chess endpoints
    "chess_update",            # board refreshes on SSE chess events
    "commentary",              # speak-while-thinking sentence frames handled
    "turn_done",               # …and the commentary turn's unlock signal
    'id="viewscreen-panel"',   # viewscreen panel exists
    "/api/viewscreen",         # …and lists/clears via the endpoints
    'id="propbar"',            # mission proposal bar exists
    "/api/proposal/",          # …and answers via the endpoint
    "mission_proposal",        # …driven by the SSE event
    'id="lat-spark"',          # latency sparkline canvas exists
    "/api/latency",            # …fed from the timings endpoint
    'role="combobox"',         # slash command inputs expose combobox semantics
    'role="listbox"',          # …with keyboard-addressable suggestion lists
    "/api/commands",           # …fed by the live backend command catalog
    "loadCommandCatalog",      # …and refreshed after ACP metadata updates
    "aria-activedescendant",   # active option remains announced while typing
):
    check(f"frontend wires {token}", token in _frontend_src)
check(
    "ws.onopen re-sends wake mode after reconnect",
    "ws.onopen" in _frontend_src and "sendWakeMode" in _frontend_src,
)
check(
    "tts_done defers to turn_done during commentary",
    "if (commentaryActive) return;" in _frontend_src,
)

# --- direction runtime contract ---------------------------------------------
# The visual-direction machinery spans three files that never import each
# other at runtime: frontend/directions.ts (the manifest registry), the inline
# pre-paint script in static/index.html (stylesheet pick), and
# frontend/hal-optic.ts (selector + scene boot). These string-level checks pin
# the contract they share so one file can't drift alone.

_frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
_directions_src = (_frontend_dir / "directions.ts").read_text()
_entry_src = (_frontend_dir / "hal-optic.ts").read_text()

# Direction 05 stages the memory extraction on session reset. The hook is
# optional by contract, so the risk is the *caller* silently dropping it —
# and a scene that hangs must never strand the reset behind it.
_reset_body = _fn_body("resetSession")
check(
    "resetSession awaits the direction's session-end hook",
    "playSessionEnd" in _reset_body,
    _reset_body,
)
check(
    "session-end hook is time-boxed so a hung scene can't strand the reset",
    "Promise.race" in _reset_body and "setTimeout" in _reset_body,
    _reset_body,
)
check(
    "session-end hook is optional (directions 01-04 don't implement it)",
    "playSessionEnd?." in _reset_body,
    _reset_body,
)
check(
    "the optic contract declares playSessionEnd optional",
    "playSessionEnd?:" in (Path(__file__).resolve().parent.parent
                           / "frontend" / "optic-api.ts").read_text(),
)

_manifests = re.findall(
    r'id:\s*"([a-z]+)".*?ready:\s*(true|false)', _directions_src, flags=re.S
)
check("directions.ts declares five manifests", len(_manifests) == 5, repr(_manifests))
_ready_ids = [mid for mid, ready in _manifests if ready == "true"]
_default_match = re.search(r'ACTIVE_DIRECTION:\s*BridgeDirectionId\s*=\s*"([a-z]+)"', _directions_src)
_default_id = _default_match.group(1) if _default_match else None
check("directions.ts declares a default direction", _default_id in _ready_ids, repr(_default_id))

_inline_ready = re.search(r"READY_DIRECTIONS\s*=\s*\[([^\]]*)\]", _frontend_src)
_inline_ids = re.findall(r'"([a-z]+)"', _inline_ready.group(1)) if _inline_ready else []
check(
    "inline pre-paint ready-list matches directions.ts",
    _inline_ids == _ready_ids,
    f"inline {_inline_ids} vs manifests {_ready_ids}",
)

for _mid, _ready in _manifests:
    check(f"direction stylesheet link for {_mid}", f'data-direction-style="{_mid}"' in _frontend_src)
    check(f"direction selector button for {_mid}", f'data-direction-id="{_mid}"' in _frontend_src)
    check(f"scene module wired for {_mid}", f'{_mid}: () => import("./optic-' in _entry_src)
    # Only the default direction's stylesheet parses enabled; the pre-paint
    # script enables the stored selection before first render.
    if _mid == _default_id:
        check(
            f"stylesheet for default direction {_mid} parses enabled",
            f'data-direction-style="{_mid}" />' in _frontend_src,
        )
    else:
        check(
            f"stylesheet for non-default direction {_mid} parses disabled",
            f'data-direction-style="{_mid}" disabled />' in _frontend_src,
        )

check(
    "storage key shared by inline script and entry",
    'localStorage.getItem("hal_direction")' in _frontend_src
    and 'DIRECTION_STORAGE_KEY = "hal_direction"' in _entry_src,
)
check(
    "not-ready selections are dropped, not persisted",
    "localStorage.removeItem(DIRECTION_STORAGE_KEY)" in _entry_src,
)

# ----------------------------------------------------------------------------

print()
if FAILURES:
    print(f"{len(FAILURES)} failure(s): {', '.join(FAILURES)}")
    sys.exit(1)
print("all tests passed")
