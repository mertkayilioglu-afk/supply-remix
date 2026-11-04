"""Shared error type for the staged pipeline.

DESIGN NOTE — why the pipeline is staged at all (generator -> splitter
-> scoring -> remix -> API), and why that matters here:

This tool is deliberately staged rather than end-to-end, on the
explicit request of the people who used the original version. Two
reasons, both load-bearing:

1. Trust. A tool that produces a result and acts on it without showing
   its work does not get used. Planners need to see the reasoning at
   each stage before accepting it — a tool nobody trusts enough to use
   has not solved anything, no matter how correct it is.
2. Failure visibility. In a single end-to-end run, a data problem or a
   bug anywhere breaks the whole chain, and finding which stage caused
   it is hard. A staged pipeline puts a checkpoint at each transition:
   you can see which stage produced bad output, rather than debugging
   the whole pipeline from a single failure at the end.

`StageError` exists because of reason 2 specifically: a checkpoint
that fails with a generic exception (or worse, silently returns
nothing) defeats the purpose of having checkpoints at all. Every stage
in this codebase that can reject its input raises a `StageError`
naming itself, what it expected, and what it actually got.

It subclasses ValueError so existing `except ValueError` /
`pytest.raises(ValueError, ...)` handling upstream keeps working
without change — this only adds structure, it doesn't change the
exception's fundamental type.
"""

from __future__ import annotations


class StageError(ValueError):
    """Raised when a pipeline stage receives data it cannot process.

    Attributes:
        stage: which stage raised this (e.g. "Step 2 — availability splitter").
        expected: a short description of what the stage needed.
        received: a short description of what it actually got.
    """

    def __init__(
        self,
        stage: str,
        message: str,
        *,
        expected: str | None = None,
        received: str | None = None,
    ) -> None:
        self.stage = stage
        self.expected = expected
        self.received = received
        parts = [f"[{stage}] {message}"]
        if expected is not None:
            parts.append(f"Expected: {expected}.")
        if received is not None:
            parts.append(f"Got: {received}.")
        super().__init__(" ".join(parts))
