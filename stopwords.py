"""Deterministic stop-word intercept, ahead of any model call.

Measured on the v4 fine-tune, on-device: "Stop!" mid-conversation produces a real
`emergency_stop` tool call 4/5 times, and the miss emits a malformed
`<function_call>emergency_stop</function_call>` that never reaches the robot (see
finetune/README.md). One in five is not an acceptable budget for the one command
whose entire purpose is to stop a moving machine, and no amount of further
fine-tuning makes a sampled language model a hard guarantee.

So the stop path does not go through the model at all. This matches the
transcript directly, in microseconds, with no network and no inference — which
also means it keeps working if the brain is swapped for a cloud endpoint
(docs/cloud-inference-options.md) and the network drops. `GemmaProvider.ask`
checks this *before* taking the per-session lock, so a stop cannot queue behind
the drive it is interrupting.

**What this does not guarantee, and must not be read as guaranteeing.** This
module makes the *decision* reliable. It does not make the *delivery*
reliable, and two things downstream can still swallow it:

  * The on-device voice loop (`termux_voice.py`) is sequential — it records a
    bounded clip, transcribes, runs the turn, speaks the reply, and only then
    listens again. While a drive is executing, the microphone is not open. A
    stop shouted mid-drive is not mis-heard or dropped; it is never captured.
  * `_run_motion` holds the serial transport for the duration of a command,
    and `_emergency_stop` opens its own. Two connections to the same device
    fail (confirmed live — see `GemmaProvider._open_robot_transport`), so a
    stop that does arrive mid-drive cannot reach the chassis. It reports that
    failure honestly rather than claiming a stop that did not happen.

**The actual safety margin is that commands are bounded and self-terminate**
— `cyberpi.mbot2.straight()`/`turn()` end on the firmware side, capped at
50 cm and 30% (crawl: 5 cm and 10%). That is what `robot/motion.py`'s
docstring means by "treat every command here as uninterruptible once sent".
This intercept shortens the window in every case it can reach; it does not
remove it. Do not design around a mid-flight stop that works.

**The error budget is deliberately asymmetric.** A false positive stops a robot
that did not need stopping: mildly annoying, entirely safe, trivially recovered
by asking it to move again. A false negative leaves a machine moving when Dave
has asked it to stop. Those are not comparable, so this matcher leans toward
firing — but not so far that ordinary conversation trips it, which is why
matching is per-clause and anchored rather than a substring search for "stop"
("What would happen if you stopped?" and "don't stop" must not fire).

Calibrated against `finetune/generate_dataset.py`'s hand-authored
`ESTOP_PHRASES` bank, which is this project's ground truth for what a real stop
request sounds like; tests/run.py asserts every one of them fires here.
"""

from __future__ import annotations

import re

# Keep letters, digits, apostrophes and hyphens ("e-stop", "don't"); everything
# else is punctuation we split on or discard.
_STRIP_RE = re.compile(r"[^a-z0-9\s'-]+")
_WS_RE = re.compile(r"\s+")
# Clause boundaries. Real stop requests are frequently a clause inside a longer
# utterance -- "Wait, stop -- that's not safe.", "There's something in the way,
# stop!" -- so the whole utterance is rarely the command by itself.
_CLAUSE_SPLIT_RE = re.compile(r"[,.;:!?]+|--+|—|–")

# Words that may pad a stop command without changing it. Stripped repeatedly
# from each end of a clause until only the core remains.
_LEADING_FILLER = frozenset(
    """hal hey ok okay please no nope nah well so and but just now right wait
    whoa woah watch out careful enough alright alrighty look listen
    """.split()
)
_LEADING_PHRASES = (
    "hold on", "hang on", "that's enough", "thats enough", "i need you to",
    "would you", "could you", "can you", "you need to", "you have to",
    "i want you to", "please can you",
)
_TRAILING_FILLER = frozenset(
    """now right immediately please hal there ok okay already
    """.split()
)
_TRAILING_PHRASES = ("right now", "right there", "right away", "at once")

# What a stop command reduces to once padding is removed.
_CORES = frozenset(
    {
        "stop",
        "halt",
        "freeze",
        "abort",
        "estop",
        "e-stop",
        "emergency stop",
        "full stop",
        "all stop",
        "stop moving",
        "stop there",
        "stop the robot",
        "stop the motors",
        "stop everything",
        "stop all motors",
        "cut the motors",
        "cut the power",
        "cut power",
        "kill the motors",
        "kill the power",
        "kill the power to the motors",
        "shut it down",
        "shut down",
        "shut it off",
        "power down",
        "power down the motors",
        "brake",
    }
)

# A clause containing any of these is a negation or a hypothetical, never a live
# command: "don't stop", "what would happen if you stopped", "imagine you stop".
_BLOCKERS = (
    "don't", "dont", "do not", "never", "not ", "n't ",
    "if ", "what if", "would happen", "imagine", "suppose", "hypothetical",
    "did you", "have you", "were you", "instead of",
)


def _normalise(clause: str) -> str:
    return _WS_RE.sub(" ", _STRIP_RE.sub(" ", clause.lower())).strip()


def _strip_padding(clause: str) -> str:
    """Peel filler words and phrases off both ends until the core is exposed."""
    previous = None
    while clause and clause != previous:
        previous = clause
        for phrase in _LEADING_PHRASES:
            if clause.startswith(phrase + " "):
                clause = clause[len(phrase) + 1 :]
        for phrase in _TRAILING_PHRASES:
            if clause.endswith(" " + phrase):
                clause = clause[: -(len(phrase) + 1)]
        words = clause.split()
        while words and words[0] in _LEADING_FILLER:
            words.pop(0)
        while words and words[-1] in _TRAILING_FILLER:
            words.pop()
        clause = " ".join(words)
    return clause


def is_stop_command(text: str) -> bool:
    """True if `text` is a live request to stop the robot right now."""
    if not text or not text.strip():
        return False
    for raw_clause in _CLAUSE_SPLIT_RE.split(text):
        clause = _normalise(raw_clause)
        if not clause:
            continue
        padded = f" {clause} "
        if any(blocker in padded for blocker in _BLOCKERS):
            continue
        if _strip_padding(clause) in _CORES:
            return True
    return False
