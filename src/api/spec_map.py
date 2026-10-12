"""Where each strategy series is specified, and where it is implemented.

**Why this is data and not a page of prose.** The obvious way to build a "Specs" page is to write the
mapping into the frontend by hand. It would be correct on the day it was written and wrong by the
next rename, with nothing to notice — the same failure as every other duplicated fact in this
project (one ACT, one trail, one cost model, one `STRATEGY_CLASSES`).

So the mapping lives here, and two things check it:

* :func:`spec_index` builds the implementation half **from the registry**, so a series that exists in
  the code and not in this table, or the reverse, is reported rather than silently omitted;
* `tests/test_spec_map.py` opens each spec file and asserts the named heading is actually present, so
  a section that gets renumbered breaks a test instead of a reader's trust.

The spec text itself is never reproduced here. A copy would be a second source of truth for trading
rules, which is precisely what `specs/` exists to be the only one of.
"""
from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from core.logging import get_logger

log = get_logger("api.spec_map")
COMPONENT = "api.spec_map"

#: Repository root, from this file's location. The specs live beside `src/`, not inside it.
REPO_ROOT = Path(__file__).resolve().parents[2]

#: ``series -> (spec file, heading, title, mechanism)``.
#:
#: ``heading`` is the literal markdown heading in that file, which is what the test greps for — so
#: the section number cannot drift from reality without failing.
SPEC_MAP: dict[str, dict[str, str]] = {
    "C1-A": {"file": "specs/03-class1-correction.md", "section": "§3",
             "heading": "## 3. C1-A — Funding Dislocation Reversion",
             "title": "Funding Dislocation Reversion",
             "mechanism": "a funding cash flow compels the correction — the strongest footing in "
                          "the system, because the flow is observable"},
    "C1-B": {"file": "specs/03-class1-correction.md", "section": "§4",
             "heading": "## 4. C1-B — Conditional Statistical Overextension",
             "title": "Conditional Statistical Overextension",
             "mechanism": "fade a stretched move, but only where the variance ratio says mean "
                          "reversion is the operative regime"},
    "C1-C": {"file": "specs/03-class1-correction.md", "section": "§5",
             "heading": "## 5. C1-C — Cross-Asset Beta Dislocation",
             "title": "Cross-Asset Beta Dislocation",
             "mechanism": "two legs against each other: the spread between an asset and its "
                          "beta-implied path. Its levels are a **residual, not a price**"},
    "C2-A": {"file": "specs/04-class2-participation.md", "section": "§3",
             "heading": "## 3. C2-A — Trend Continuation on Volatility-Normalized Pullback",
             "title": "Trend Continuation on Volatility-Normalized Pullback",
             "mechanism": "join an established trend on a pullback measured in ATR, not in percent"},
    "C2-B": {"file": "specs/04-class2-participation.md", "section": "§4",
             "heading": "## 4. C2-B — Market-Factor Trend with Best-Expression Selection",
             "title": "Market-Factor Trend with Best-Expression Selection",
             "mechanism": "take the market factor through whichever asset expresses it best"},
    "C2-C": {"file": "specs/04-class2-participation.md", "section": "§5",
             "heading": "## 5. C2-C — Size-Backed Trend Participation",
             "title": "Size-Backed Trend Participation (Tier B)",
             "mechanism": "trend participation confirmed by positioning data. **Tier B**: its "
                          "inputs are point-in-time and cannot be rebuilt, so it has no history "
                          "to be tested against"},
    "C3-A": {"file": "specs/05-class3-transition.md", "section": "§3",
             "heading": "## 3. C3-A — Volatility Contraction Resolution",
             "title": "Volatility Contraction Resolution",
             "mechanism": "a compressed range resolves. Arms a **directionless bracket** and lets "
                          "the break choose the side"},
    "C3-B": {"file": "specs/05-class3-transition.md", "section": "§4",
             "heading": "## 4. C3-B — Persistence Transition (Trend Birth)",
             "title": "Persistence Transition (Trend Birth)",
             "mechanism": "the variance ratio crossing 1 dates the moment mean reversion gives way "
                          "to trending"},
    "C3-C": {"file": "specs/05-class3-transition.md", "section": "§5",
             "heading": "## 5. C3-C — Structural Boundary Resolution",
             "title": "Structural Boundary Resolution",
             "mechanism": "a significant level either holds or does not. **Direction is not "
                          "predicted** — acceptance trades with the break, rejection against it"},
    "C4-A": {"file": "specs/06-class4-established.md", "section": "§4.1",
             "heading": "### 4.1 C4-A — Donchian Channel Breakout with Volatility-Scaled Exit",
             "title": "Donchian Channel Breakout with Volatility-Scaled Exit",
             "mechanism": "a published, independently documented breakout rule. **No fixed target** "
                          "— trailed from entry, because capping a trend destroys its expectancy"},
    "C4-B": {"file": "specs/06-class4-established.md", "section": "§4.2",
             "heading": "### 4.2 C4-B — Dual-Timeframe Momentum with Volatility Targeting",
             "title": "Dual-Timeframe Momentum with Volatility Targeting",
             "mechanism": "time-series momentum with exposure scaled inversely to volatility. "
                          "**No fixed target** — trailed"},
    "C4-C": {"file": "specs/06-class4-established.md", "section": "§4.3",
             "heading": "### 4.3 C4-C — Short-Horizon Reversal",
             "title": "Short-Horizon Reversal",
             "mechanism": "the documented short-horizon reversal effect, with a hard "
                          "volatility-scaled stop and a fixed target"},
    "C4-X1": {"file": "specs/06-class4-established.md", "section": "§5.1",
              "heading": "### 5.1 C4-X1 — Random-Entry Control",
              "title": "Random-Entry Control",
              "mechanism": "a **control, never tradeable**. Random entries with the same sizing and "
                           "exits, so the rest can be measured against luck"},
    "C4-X2": {"file": "specs/06-class4-established.md", "section": "§5.2",
              "heading": "### 5.2 C4-X2 — Weak-Evidence Control",
              "title": "Weak-Evidence Control",
              "mechanism": "a **control, never tradeable**. A deliberately weak rule: if it "
                           "backtests well, the harness is wrong rather than the rule being good"},
}

#: Documents that are not a single series but govern all of them. Listed because "where is this
#: defined" is asked about thresholds and exits at least as often as about a strategy.
CROSS_CUTTING: tuple[dict[str, str], ...] = (
    {"file": "specs/02-features-and-regime.md", "topic": "Features and the regime model",
     "detail": "every measurement the conditions read, and the regime axes"},
    {"file": "specs/07-arbitration-and-lifecycle.md", "topic": "Lifecycle and arbitration",
     "detail": "the eleven signal states, the legal transitions, and how conflicts between "
               "strategies are resolved"},
    {"file": "specs/08-risk-engine.md", "topic": "Risk, sizing and costs",
     "detail": "position sizing, the veto ladder, and the tick-rounding rule that makes a stop "
               "round away from entry and a target toward it"},
    {"file": "specs/09-validation-protocol.md", "topic": "Validation protocol",
     "detail": "why nothing here is validated yet, and what would have to happen for it to be"},
    {"file": "specs/12-feature-and-regime-engine.md", "topic": "Degraded regime mode",
     "detail": "§10: what the system does when the regime model fails its validity tests — the "
               "reduced-selectivity mode BTC and SOL are running in"},
)


def _implementations() -> dict[str, dict[str, Any]]:
    """Where each registered strategy's class is defined, read from the code itself."""
    from strategy.registry import backtest_strategies

    out: dict[str, dict[str, Any]] = {}
    for strategy in backtest_strategies({}):
        cls = type(strategy)
        try:
            module = inspect.getmodule(cls)
            source_file = Path(inspect.getfile(cls)).resolve()
            line = inspect.getsourcelines(cls)[1]
            relative = source_file.relative_to(REPO_ROOT).as_posix()
        except (OSError, TypeError, ValueError):            # pragma: no cover - import edge
            module, relative, line = None, None, None
        out[strategy.id] = {
            "series": strategy.id,
            "strategy_class": int(strategy.strategy_class),
            "tradeable": bool(strategy.tradeable),
            "version": str(strategy.version),
            "tier": str(getattr(strategy, "tier", "")),
            "implementation": relative,
            "implementation_line": line,
            "module": module.__name__ if module else None,
        }
    return out


def spec_index() -> dict[str, Any]:
    """The whole mapping: every registered series, its specification, and its implementation.

    Discrepancies are **reported, not hidden**. A series in the registry with no entry here, or an
    entry here for a series the registry does not have, appears in ``unmapped`` / ``orphaned`` — the
    page shows them, because a Specs page that quietly omits a strategy is worse than no Specs page.
    """
    implementations = _implementations()
    series = []
    for sid, impl in sorted(implementations.items()):
        spec = SPEC_MAP.get(sid)
        series.append({**impl,
                       "spec_file": spec["file"] if spec else None,
                       "spec_section": spec["section"] if spec else None,
                       "spec_heading": spec["heading"] if spec else None,
                       "title": spec["title"] if spec else None,
                       "mechanism": spec["mechanism"] if spec else None})

    unmapped = sorted(set(implementations) - set(SPEC_MAP))
    orphaned = sorted(set(SPEC_MAP) - set(implementations))
    if unmapped or orphaned:
        log.warning("spec_map_incomplete", unmapped=unmapped, orphaned=orphaned,
                    detail="a registered strategy has no specification entry, or an entry names a "
                           "strategy that is not registered",
                    remediation="update SPEC_MAP in src/api/spec_map.py alongside the registry")
    return {
        "series": series,
        "cross_cutting": [dict(d) for d in CROSS_CUTTING],
        "unmapped": unmapped,
        "orphaned": orphaned,
        "capability_map": {"file": "specs/00-capability-map.md",
                           "note": "the authoritative record of what the exchange API actually "
                                   "provides — it overrides the vendor documentation, which "
                                   "contains errors this project has verified"},
        "note": "The specification files are the authority for every trading rule. This page says "
                "where each one lives; it never restates the rules, because a second copy would be "
                "a second thing to keep correct.",
    }
