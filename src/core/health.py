"""System health and state reporting (specs/14 §3–§5, specs/17 §8).

The question this answers is the one that matters when something is wrong:

> **Is this an application bug, a configuration mistake, local infrastructure, or CoinDCX?**

Each component reports a ``ComponentHealth`` with a state and, when unhealthy, the
``ProblemClass`` that owns it. The overall ``SystemStatus`` is the worst of them, and the report
lists the specific reasons — so a dashboard banner and a future Azure alert both say *what* broke
and *who* fixes it, rather than "degraded".

Deliberate distinctions, because collapsing any of them misleads (specs/14 §4):

* ``HEALTHY`` — working.
* ``WARMING`` — working, but features are still accumulating bars. Normal for a new install; it is
  **progress, not failure**, and must never render as empty.
* ``DEGRADED`` — some sources failing, others fine. Panels that depend on healthy sources stay live.
* ``STALE`` — data present but too old to trust. A triggered signal still shows its stop.
* ``DOWN`` — ingestion or storage is broken. **Never the same as "no setups found".**
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .errors import CryptoTraderError, ProblemClass


class SystemStatus(str, Enum):
    HEALTHY = "HEALTHY"
    WARMING = "WARMING"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    DOWN = "DOWN"

    @property
    def rank(self) -> int:
        return {"HEALTHY": 0, "WARMING": 1, "DEGRADED": 2, "STALE": 3, "DOWN": 4}[self.value]

    def worst_of(self, other: SystemStatus) -> SystemStatus:
        return self if self.rank >= other.rank else other


def now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class ComponentHealth:
    """One named component's state. ``problem_class`` is required unless HEALTHY/WARMING."""

    name: str
    status: SystemStatus
    detail: str = ""
    problem_class: ProblemClass | None = None
    last_success_ms: int | None = None
    consecutive_failures: int = 0
    context: dict[str, Any] = field(default_factory=dict)
    remediation: str | None = None

    def __post_init__(self) -> None:
        if self.status in (SystemStatus.DEGRADED, SystemStatus.STALE, SystemStatus.DOWN) and self.problem_class is None:
            # Enforced: an unhealthy component with no owner is an unactionable alert.
            raise ValueError(f"component {self.name!r} is {self.status.value} but declares no problem_class")

    @property
    def age_seconds(self) -> float | None:
        return None if self.last_success_ms is None else (now_ms() - self.last_success_ms) / 1000

    @classmethod
    def healthy(cls, name: str, *, last_success_ms: int | None = None, detail: str = "", **ctx: Any) -> ComponentHealth:
        return cls(name=name, status=SystemStatus.HEALTHY, detail=detail, last_success_ms=last_success_ms, context=ctx)

    @classmethod
    def from_error(cls, name: str, err: CryptoTraderError, *, status: SystemStatus = SystemStatus.DOWN,
                   last_success_ms: int | None = None, consecutive_failures: int = 0) -> ComponentHealth:
        """Build health straight from a raised error, so the classification cannot drift from it."""
        return cls(
            name=name,
            status=status,
            detail=err.message,
            problem_class=err.problem_class,
            last_success_ms=last_success_ms,
            consecutive_failures=consecutive_failures,
            context={"code": err.code, **err.context},
            remediation=err.remediation,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "detail": self.detail,
            "problem_class": self.problem_class.value if self.problem_class else None,
            "last_success_ms": self.last_success_ms,
            "age_seconds": round(self.age_seconds, 1) if self.age_seconds is not None else None,
            "consecutive_failures": self.consecutive_failures,
            "context": self.context,
            "remediation": self.remediation,
        }


@dataclass
class HealthReport:
    components: list[ComponentHealth] = field(default_factory=list)
    served_at_ms: int = field(default_factory=now_ms)
    data_as_of_ms: int | None = None
    warmup_pct: float | None = None
    notes: list[str] = field(default_factory=list)

    def add(self, c: ComponentHealth) -> HealthReport:
        self.components.append(c)
        return self

    @property
    def status(self) -> SystemStatus:
        status = SystemStatus.HEALTHY
        for c in self.components:
            status = status.worst_of(c.status)
        return status

    @property
    def operational_status(self) -> SystemStatus:
        """The worst status among components that represent something **actionable**.

        `MODELLING` components are excluded, and the exclusion is the point.

        A `MODELLING` component says the data and the code are both fine and a model's assumption is
        not supported by this market. For the regime model that is a *verified, recorded, permanent*
        finding — F-033 measured median dwell 2.0 bars against a requirement of 3 on BTC and SOL, and
        `specs/12` §10 prescribes exactly what the system then does: drop the dwell-stability
        conditions, keep every raw-axis threshold, run with reduced selectivity.

        Folding that into one `worst_of` over everything made the whole system permanently
        `DEGRADED`, with two consequences that matter more than the label:

        1. the dashboard said "2 not healthy" for an expected operating mode, which is a warning that
           is always on, and a warning that is always on is one nobody reads;
        2. **a real DEGRADED — a failing poller, a stale feed — became invisible**, because it looked
           exactly like the two that are always there.

        The second is the actual defect. The distinction `ProblemClass.MODELLING` was created to draw
        ("the triage answer is *read the finding*, not *check the feed* or *fix the code*") was being
        discarded one layer above the place it was declared (F-060).

        ``status`` is unchanged and still the worst of everything, for callers that want it.
        """
        status = SystemStatus.HEALTHY
        for c in self.components:
            if c.problem_class is ProblemClass.MODELLING:
                continue
            status = status.worst_of(c.status)
        return status

    @property
    def modelling_components(self) -> list[ComponentHealth]:
        """Components reporting a model finding rather than a fault. Never hidden, never a page."""
        return [c for c in self.components if c.problem_class is ProblemClass.MODELLING]

    @property
    def modelling_findings(self) -> list[dict[str, Any]]:
        """The model findings, for a panel of their own (`specs/12` §10 requires them visible)."""
        return [{"name": c.name, "status": c.status.value, "detail": c.detail,
                 "remediation": c.remediation, "context": dict(c.context)}
                for c in self.modelling_components]

    @property
    def problem_classes(self) -> list[str]:
        """Distinct owners of every current problem — the triage summary."""
        seen: list[str] = []
        for c in self.components:
            if c.problem_class and c.problem_class.value not in seen:
                seen.append(c.problem_class.value)
        return seen

    def unhealthy(self) -> list[ComponentHealth]:
        return [c for c in self.components if c.status is not SystemStatus.HEALTHY]

    def faults(self) -> list[ComponentHealth]:
        """Unhealthy components that are **faults** — everything except model findings."""
        return [c for c in self.unhealthy() if c.problem_class is not ProblemClass.MODELLING]

    @property
    def degraded_reasons(self) -> list[str]:
        """Human-readable, specific, and never apologetic (specs/15 §11).

        Faults only. A model finding is reported through :attr:`modelling_findings`, which is a
        different question with a different answer, and mixing them is what buried the one that
        needed acting on.
        """
        out = []
        for c in self.faults():
            age = f", last ok {c.age_seconds:.0f}s ago" if c.age_seconds is not None else ""
            owner = f" [{c.problem_class.value}]" if c.problem_class else ""
            out.append(f"{c.name}: {c.status.value.lower()} — {c.detail}{age}{owner}")
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            # `status` is the worst of everything and stays that way; `operational_status` is the
            # worst of the things anyone can act on. Both are published because they answer
            # different questions, and a consumer that only shows one must show the second.
            "status": self.status.value,
            "operational_status": self.operational_status.value,
            "served_at_ms": self.served_at_ms,
            "data_as_of_ms": self.data_as_of_ms,
            "age_seconds": round((self.served_at_ms - self.data_as_of_ms) / 1000, 1)
            if self.data_as_of_ms is not None else None,
            "problem_classes": self.problem_classes,
            "degraded_reasons": self.degraded_reasons,
            # Separate, and never empty-by-omission: `specs/12` §10 requires the degradation to be
            # surfaced so "a user should be able to see the system is running with a weaker filter
            # set". It is surfaced here, as a finding, rather than as a fault in the list above.
            "modelling_findings": self.modelling_findings,
            "fault_count": len(self.faults()),
            "warmup_pct": self.warmup_pct,
            "notes": self.notes,
            "components": [c.to_dict() for c in self.components],
        }
