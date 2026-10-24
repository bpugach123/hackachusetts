"""Data_Gap model (Req 2.6-2.8).

`DataGap` matches the schema's DataGap shape (docs/schema.md): factor, source,
reason in {"fetch failure", "empty response", "no coverage"}, optional detail.
The fetch layer produces "fetch failure" and "empty response"; "no coverage" is
raised per cell by scorers. `GapRegistry` collects gaps for the run summary.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal, get_args

GapReason = Literal["fetch failure", "empty response", "no coverage"]
GAP_REASONS: frozenset[str] = frozenset(get_args(GapReason))


@dataclass(frozen=True)
class DataGap:
    factor: str
    source: str
    reason: GapReason
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.reason not in GAP_REASONS:
            raise ValueError(
                f"DataGap reason must be one of {sorted(GAP_REASONS)}, "
                f"got {self.reason!r}"
            )

    def for_factor(self, factor: str) -> DataGap:
        """Same gap relabeled for ``factor`` (fetch gaps start as run-wide ``"*"``)."""
        return DataGap(factor, self.source, self.reason, self.detail)

    def to_dict(self) -> dict:
        """Serialize to the schema's DataGap shape (`detail` omitted when None)."""
        out: dict = {
            "factor": self.factor,
            "source": self.source,
            "reason": self.reason,
        }
        if self.detail is not None:
            out["detail"] = self.detail
        return out


class FatalSourceError(RuntimeError):
    """A required source is unavailable with no usable cache; the run must stop.

    Raised for sources the pipeline cannot run without (the shoreline, Req 1.5).
    ``source`` names the dataset and ``reason`` the failure, so the CLI can
    report both and exit without touching the Static_Output.
    """

    def __init__(self, source: str, reason: str):
        self.source = source
        self.reason = reason
        super().__init__(f"{source}: {reason}")

    @classmethod
    def from_gap(cls, source: str, gap: DataGap) -> FatalSourceError:
        reason = gap.reason if gap.detail is None else f"{gap.reason} ({gap.detail})"
        return cls(source, reason)


class GapRegistry:
    """Collects Data_Gaps across a run and summarizes them by source (Req 2.8).

    ``add`` records a gap against a cell id (or ``"*"`` for a run-wide gap). The
    same gap can be registered for many cells; ``summary`` counts every recorded
    entry grouped by source, which matches the count of DataGaps in the output.
    """

    def __init__(self) -> None:
        self._entries: list[tuple[str, DataGap]] = []

    def add(
        self,
        cell_id: str,
        factor: str | DataGap,
        source: str | None = None,
        reason: GapReason | None = None,
        detail: str | None = None,
    ) -> DataGap:
        """Record a gap against ``cell_id`` (use ``"*"`` for a run-wide gap).

        Call as ``add(cell_id, factor, source, reason[, detail])`` or pass a
        ready-made :class:`DataGap` as the second argument.
        """
        if isinstance(factor, DataGap):
            gap = factor
        else:
            if source is None or reason is None:
                raise ValueError("GapRegistry.add needs factor, source and reason")
            gap = DataGap(factor, source, reason, detail)
        self._entries.append((cell_id, gap))
        return gap

    def extend(self, cell_id: str, gaps: Iterable[DataGap]) -> None:
        for gap in gaps:
            self.add(cell_id, gap)

    def summary(self) -> dict[str, int]:
        """``{source: count}`` over every recorded gap, sorted by source."""
        counts: dict[str, int] = {}
        for _cell_id, gap in self._entries:
            counts[gap.source] = counts.get(gap.source, 0) + 1
        return {k: counts[k] for k in sorted(counts)}

    def entries(self) -> list[tuple[str, DataGap]]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)
