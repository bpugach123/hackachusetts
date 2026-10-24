"""Pure scoring helpers shared by every component score.

Mirrored in ``frontend/src/model/ramps.ts``; keep the two in sync.

No I/O and no rounding here: results are rounded to 1 dp at write time only.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import NamedTuple


def ramp(
    x: float,
    x0: float,
    x1: float,
    y0: float = 0.0,
    y1: float = 100.0,
) -> float:
    """Clamped linear ramp from ``(x0, y0)`` to ``(x1, y1)``.

    The output is ``y0`` on ``x0``'s side of the interval, ``y1`` on ``x1``'s
    side, and linearly interpolated between. The result always lies within
    ``[min(y0, y1), max(y0, y1)]``.

    Descending ramps are supported either way:

    * ``y0 > y1`` with ``x0 < x1``, e.g. ``ramp(d_km, 1, 30, 100, 0)`` gives 100
      at or below 1 km and 0 at or above 30 km (Req 6.4, 9.3, 9.4).
    * ``x0 > x1`` is also accepted; the interpolation parameter is
      ``(x - x0) / (x1 - x0)`` clamped to ``[0, 1]``, so the mapping is the
      same line regardless of argument order.

    Degenerate interval (``x0 == x1``): the ramp collapses to a step.
    ``x <= x0`` returns ``y0`` (the ``x0`` endpoint is inclusive and wins the
    tie) and ``x > x0`` returns ``y1``. For a "full score at or below the
    threshold" ramp this keeps the threshold itself at full score.

    Raises ``ValueError`` if any argument is NaN.
    """
    if any(math.isnan(v) for v in (x, x0, x1, y0, y1)):
        raise ValueError("ramp arguments must not be NaN")

    if x0 == x1:
        return float(y0) if x <= x0 else float(y1)

    t = (x - x0) / (x1 - x0)
    if t <= 0.0:
        return float(y0)
    if t >= 1.0:
        return float(y1)
    value = y0 + t * (y1 - y0)
    # Guard against float overshoot at the ends.
    lo, hi = (y0, y1) if y0 <= y1 else (y1, y0)
    return float(min(max(value, lo), hi))


class WeightedMean(NamedTuple):
    """Result of :func:`weighted_mean_available`.

    ``value`` is ``None`` when no sub-score is available or the available
    weights sum to 0. ``omitted`` lists the sub-score names left out of the
    mean (unavailable scores), in ``weights`` order, for the Cell breakdown.
    """

    value: float | None
    omitted: list[str]


def _available(score: float | None) -> bool:
    return score is not None and not math.isnan(score)


def weighted_mean_available(
    scores: Mapping[str, float | None],
    weights: Mapping[str, float],
) -> WeightedMean:
    """Weighted mean over the available sub-scores, with renormalized weights.

    The set of sub-scores is defined by ``weights`` (the configured weights).
    A sub-score is unavailable, and so omitted, when it is missing from
    ``scores``, is ``None`` (a Data_Gap), or is NaN. The weights of the
    available sub-scores are renormalized to sum to 1 (Req 6.5, 6.6, 7.4, 7.6,
    9.5, 9.6).

    Scores whose names are not in ``weights`` are ignored. Negative weights
    raise ``ValueError``.

    Returns ``WeightedMean(value, omitted)``; it unpacks as a tuple.
    """
    for name, w in weights.items():
        if math.isnan(w) or w < 0:
            raise ValueError(f"weight for {name!r} must be a non-negative number")

    omitted: list[str] = []
    total_w = 0.0
    acc = 0.0
    for name, w in weights.items():
        s = scores.get(name)
        if not _available(s):
            omitted.append(name)
            continue
        total_w += w
        acc += w * s  # type: ignore[operator]

    if total_w == 0.0:
        return WeightedMean(None, omitted)
    return WeightedMean(acc / total_w, omitted)
