"""Tidy a transcript before it is pasted."""

from __future__ import annotations

import re

# um, umm, uh, uhh, uhm, er, erm: as whole words only, so "umbrella", "her"
# and "uh-oh" are left alone. A comma after the filler goes with it.
_FILLER = re.compile(r"(?<![\w'-])(?:um+|uh+m*|er+m*)(?![\w'-])\s*[,;]?\s*", re.IGNORECASE)
# Short words people double when they hesitate. Words that double legitimately
# ("that that", "had had") are deliberately not listed.
_STUTTER = re.compile(r"\b(i|the|a|an|to|and|we|it|you|of|in|my|so|but|if|or)(?:\s+\1)+\b", re.IGNORECASE)
_SENTENCE_START = "\x00"


def single_line(text: str) -> str:
    """Collapse line breaks, tabs and control characters into single spaces.

    Pasted text must never carry a line break: in a terminal or a chat box a
    pasted newline can run a command or send a message.
    """
    return " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())


def remove_fillers(text: str) -> str:
    """Drop filler words and stuttered repeats, keeping everything else as spoken."""

    def drop(match: re.Match) -> str:
        before = match.string[: match.start()].rstrip()
        # Mark removals at the start of a sentence so the next word can take the capital.
        return _SENTENCE_START if not before or before[-1] in ".!?" else ""

    text = _FILLER.sub(drop, text)
    text = re.sub(_SENTENCE_START + r"[\s.,;!?]*", _SENTENCE_START, text)
    text = re.sub(_SENTENCE_START + r"(\w)", lambda m: m.group(1).upper(), text)
    text = text.replace(_SENTENCE_START, "")
    text = _STUTTER.sub(lambda m: m.group(0).split()[0], text)
    text = re.sub(r"[,;]\s*(?=[.!?])", "", text)  # "Well, ." -> "Well."
    text = re.sub(r"\s+(?=[.,;!?])", "", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" ,;")


def apply_vocabulary(text: str, hears: dict[str, list[str]]) -> str:
    """Put the words you want in place of the ways the speech model mishears them.

    ``hears`` maps a wanted word ("Acme") to the phrases the model tends to produce for it
    ("ack me", "ak me"). Matching is on whole words, ignoring capitalisation and the exact
    spacing between words, and the wanted word itself is put back in its own capitalisation
    ("acme" becomes "Acme"). A trailing 's stays: "ack me's" becomes "Acme's".
    """
    for wanted, phrases in hears.items():
        for phrase in sorted({wanted, *phrases}, key=len, reverse=True):
            words = phrase.split()
            if not words:
                continue
            pattern = (
                r"(?<![\w'-])"
                + r"[\s-]+".join(re.escape(word) for word in words)
                + r"(?![\w-])(?!'(?!s(?![\w])))"
            )
            text = re.sub(pattern, lambda _m, w=wanted: w, text, flags=re.IGNORECASE)
    return text
