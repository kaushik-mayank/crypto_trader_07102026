"""The canonical snapshot request: what the system asks the store for, in one place.

Three processes need to agree on this and they must not learn it from each other:

* ``app`` builds snapshots from it and evaluates them;
* ``bootstrap`` derives *what history to fetch* from it, so the fetch depth cannot drift from what
  the app actually reads;
* ``ingest`` checks completeness against it at startup.

It lived in ``app/__main__.py``, which meant the ingest process imported the app to find out what
data to collect — backwards, and a dependency that would have made the two-process topology of
specs/10 §3 a fiction. Here it belongs to neither.

**Futures only, deliberately.** No strategy reads a spot *candle*: the basis family reads the spot
*ticker*, which is point-in-time. Requesting spot series nobody computes on would add an hour to
every fresh host's first fetch for nothing.
"""
from __future__ import annotations

from normalize.canonical import Product, Timeframe
from snapshot.builder import WARMUP_BARS, SeriesRequest, SnapshotRequest
from snapshot.models import InstrumentKey, SeriesKey

ASSETS: tuple[str, ...] = ("B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT")

#: The alpha set: the timeframes features are computed on, each at its full warm-up depth.
ALPHA_TIMEFRAMES = (Timeframe.H1, Timeframe.H4, Timeframe.D1)

#: The trigger set ACT needs beyond the alpha timeframes: 15m sizes the unit, 1m confirms the entry
#: (specs/03 §2). Shallower than the alpha set because a trigger reads the recent past only.
TRIGGER_BARS: dict[Timeframe, int] = {Timeframe.M15: 200, Timeframe.M1: 240}

#: The point-in-time sources (Tier B). These have **no archive** — they accumulate only while the
#: ingest process runs, which is why two strategies cannot be backtested at all (specs/09 §13).
TIER_B_SOURCES = ("futures_rt", "spot_ticker", "positioning", "futures_book", "spot_book")

TIER_B_HISTORY_MS = 7 * 86_400_000


def build_request(assets: tuple[str, ...] = ASSETS,
                  product: Product = Product.FUTURES) -> SnapshotRequest:
    """The request every process shares. Pure: same arguments, same request, every time."""
    series = [SeriesRequest(SeriesKey(a, product, tf), bars=WARMUP_BARS[tf])
              for a in assets for tf in ALPHA_TIMEFRAMES]
    series += [SeriesRequest(SeriesKey(a, product, tf), bars=n)
               for a in assets for tf, n in TRIGGER_BARS.items()]
    return SnapshotRequest(
        series=tuple(series),
        tier_b=tuple((s, a) for a in assets for s in TIER_B_SOURCES),
        instruments=tuple(InstrumentKey(a, product) for a in assets),
        forming=tuple(SeriesKey(a, product, Timeframe.H1) for a in assets),
        tier_b_history_ms=TIER_B_HISTORY_MS,
    )
