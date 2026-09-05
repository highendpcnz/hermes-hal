"""Deterministic end-of-conversation intercept for the on-device voice loop.

Adapted from hal's `brain/farewell.py` at commit 1318bd1 (see
docs/technology-transfer.md). Owned here; hal is not consulted again.

The hands-free loop (`termux_voice.py`) runs against a single fixed session id,
so without something like this a conversation never actually ends — every turn
accumulates into the same history forever, and tomorrow's "HAL, good morning"
still carries tonight's context. `/api/session/reset` exists but is HTTP-only,
which is no use to someone standing in front of the phone.

Matching is per-clause, anchored, with padding peeled off both ends, and it
never goes through the model — so it keeps working regardless of what the
agent is doing or whether the network is up.

**The error budget leans toward *not* firing, deliberately.** A false positive
destroys conversation history — context Dave may still have wanted — while a
false negative costs him only saying it again. So the farewell must be
essentially the whole clause, with nothing substantive attached. "That's all,
HAL" ends the conversation; "that's all the power we have left" does not, and
neither does asking what a goodbye is.

The wake word is stripped as filler, because a spoken sign-off almost always
carries it ("that'll be all, HAL") — including the homophones whisper actually
produces for the name, since this sees raw transcripts.
"""

from __future__ import annotations

import re

_STRIP_RE = re.compile(r"[^a-z0-9\s'-]+")
_WS_RE = re.compile(r"\s+")
_CLAUSE_SPLIT_RE = re.compile(r"[,.;:!?]+|--+|—|–")

# The name, plus the homophones base.en renders it as (see termux_voice.py).
# Stripped from either end so "that's all hal" and "how, that's all" both
# reduce to the bare core.
_NAME_FILLER = frozenset({"hal", "hall", "hell", "how", "howl", "huh"})

_LEADING_FILLER = frozenset(
    {"ok", "okay", "alright", "right", "well", "so", "and", "um", "uh",
     "please", "thanks", "now", "anyway", "anyhow"}
) | _NAME_FILLER
_LEADING_PHRASES = ("thank you", "that will do", "i think")
_TRAILING_FILLER = frozenset(
    {"then", "now", "please", "thanks", "tonight", "today", "goodnight"}
) | _NAME_FILLER
_TRAILING_PHRASES = ("for now", "thank you", "for tonight", "for today", "9000")

# A farewell must reduce to exactly one of these once padding is peeled.
_CORES = frozenset(
    {
        "that's all", "thats all", "that is all", "that's it", "thats it",
        "that's everything", "thats everything",
        "that'll be all", "thatll be all", "that will be all",
        "that would be all", "that'd be all",
        "goodbye", "good bye", "bye", "bye bye", "goodnight", "good night",
        "we're done", "were done", "we are done", "we're finished",
        "i'm done", "im done", "i am done",
        "dismissed", "you're dismissed", "youre dismissed",
        "over and out", "signing off", "sign off",
        "end of conversation", "conversation over", "conversation's over",
        "nothing else", "nothing more", "no more questions",
        "see you", "see you later", "talk later", "speak later",
        "stand down", "go to sleep", "sleep now", "rest now",
    }
)

# Contexts where a farewell word is being discussed, quoted or negated rather
# than used. Mirrors stopwords.py's blocker approach: cheaper and far more
# legible than trying to encode every safe sentence shape in the cores.
_BLOCKERS = (
    "don't", "dont", "do not", "never", "not ", "n't ",
    "if ", "what if", "would happen", "imagine", "suppose", "hypothetical",
    "did you", "have you", "were you", "instead of",
    "what does", "what's a", "whats a", "how do you say", "how do i say",
    "mean", "means", "meaning", "spell", "word for", "translate",
    "say ", "said ", "saying ", "tell ",
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


def is_farewell(text: str) -> bool:
    """True if `text` is Dave signing off — ending the conversation, not
    merely mentioning an ending."""
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
