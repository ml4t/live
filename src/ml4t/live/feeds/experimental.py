"""Explicit opt-in contract for feeds outside the beta support boundary."""

from __future__ import annotations

import warnings
from collections.abc import Sequence


class ExperimentalFeedError(RuntimeError):
    """Raised when an experimental feed is constructed without deliberate opt-in."""


class ExperimentalFeedWarning(UserWarning):
    """Reports guarantees that do not apply to an experimental feed."""


def require_experimental_opt_in(
    feed: str,
    *,
    experimental: bool,
    missing_guarantees: Sequence[str],
) -> None:
    """Require explicit opt-in and report the unsupported guarantees."""
    if experimental is not True:
        raise ExperimentalFeedError(
            f"{feed} is experimental and is not part of the ml4t-live beta support contract. "
            "Pass experimental=True only after accepting its documented limitations."
        )
    detail = ", ".join(missing_guarantees)
    warnings.warn(
        f"{feed} experimental opt-in accepted. Missing beta guarantees: {detail}.",
        ExperimentalFeedWarning,
        stacklevel=3,
    )


__all__ = ["ExperimentalFeedError", "ExperimentalFeedWarning"]
