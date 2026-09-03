"""Canonical rendering for reproducibility fingerprints.

A fingerprint is only useful if it is byte-identical for logically identical
input, in every process and on every machine. Two things break that quietly:

*   ``str(Decimal)`` preserves trailing zeros and can emit exponent notation, so
    ``Decimal("2.0")``, ``Decimal("2.00")`` and ``Decimal("2E+0")`` compare equal
    but render three different ways.
*   Python's builtin ``hash()`` is salted per process for strings unless
    ``PYTHONHASHSEED`` is pinned, so it must never reach a fingerprint. Use
    ``hashlib`` over canonical bytes instead.

These helpers give exactly one rendering per value type. Every module that folds
a value into a fingerprint or a manifest goes through them, so the renderings
cannot drift apart.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

__all__ = ["canonical_datetime", "canonical_decimal"]


def canonical_decimal(value: Decimal) -> str:
    """Render a Decimal with no trailing zeros and never in exponent notation.

    ``normalize()`` strips trailing zeros but may itself produce an exponent
    form (``Decimal("100").normalize()`` is ``1E+2``); the ``"f"`` format always
    expands that back out.
    """
    return format(value.normalize(), "f")


def canonical_datetime(value: datetime) -> str:
    """Render an aware datetime as UTC ISO-8601.

    Naive input is rejected rather than assumed to be UTC - guessing the zone is
    how two "identical" inputs end up with different fingerprints.
    """
    if value.tzinfo is None:
        raise ValueError(f"cannot canonicalise naive datetime {value!r}")
    return value.astimezone(UTC).isoformat()
