"""Untrusted-content boundary for reference text (N8 B4).

WHY THIS EXISTS
---------------
A design reference is external content. Its label, descriptor, source URI and
revision end up inside a Decisions prompt. If any of them carries a newline, a
carriage return, a NUL, an ANSI escape or a bidi override, that text can
fabricate what looks like application-authored prompt structure.

WHAT THIS DOES AND DOES NOT DO
------------------------------
It neutralises STRUCTURAL injection: a retrieved value can no longer start a new
prompt line, hide characters, or reorder what a reader sees. It cannot stop
SEMANTIC injection -- a single line reading "ignore previous instructions" is
still a single line. That is defended architecturally, not textually:

* the model's answer is only ever a SCORE or an ID from a fixed offered set;
* an ID that was not offered is rejected outright;
* provenance is copied from the reference, never produced by the model.

So the worst case is a bad score, never a bad action.

The function is applied at RENDER time. Stored evidence is never mutated: what
was ingested stays exactly as ingested, so an audit still sees the original.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

#: ANSI/VT escape sequences: CSI, OSC and the simple two-character forms.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]")

#: Default maximum length of a rendered field.
DEFAULT_RENDER_LIMIT = 200


def one_line(value: Any, limit: int = DEFAULT_RENDER_LIMIT) -> str:
    """Collapse *value* to a single inert line of bounded length.

    Removes ANSI sequences, drops every C0/C1 control character and every
    format character (bidi overrides, zero-width joiners), then collapses all
    whitespace runs to one space and truncates.

    ``" ".join(s.split())`` alone is NOT sufficient: it leaves NUL, ANSI escapes
    and bidi overrides intact, all of which survive into a prompt.
    """
    text = _ANSI_RE.sub("", str(value))
    text = "".join(
        ch for ch in text if unicodedata.category(ch) not in ("Cc", "Cf")
    )
    text = " ".join(text.split())
    return text[:limit]


def render_candidate_line(
    candidate_id: Any,
    label: Any,
    source_uri: Any,
    source_revision: Any,
    descriptor: Any,
) -> str:
    """Render one candidate as a single prompt line, sanitising every field.

    Every interpolated value passes through :func:`one_line`, so no retrieved
    string can introduce a new line, a control character, or a bidi override
    into the prompt structure.
    """
    return (
        f"- {one_line(candidate_id, 80)}: {one_line(label, 120)} | "
        f"provenance: {one_line(source_uri, 200)} @ "
        f"{one_line(source_revision, 64)} | "
        f"descriptor: {one_line(descriptor, 600)}"
    )


__all__ = ["one_line", "render_candidate_line", "DEFAULT_RENDER_LIMIT"]
