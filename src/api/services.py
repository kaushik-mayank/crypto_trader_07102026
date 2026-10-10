"""The service layer (specs/14 §2).

> **Engines write. The API reads. No signal logic, no feature computation, no risk evaluation lives
> behind an HTTP handler.**

A slow or repeated query must never delay signal evaluation, and a dashboard refresh must never change
a signal's content. The two paths touch only through the store.

Both renderings — JSON and HTML fragments — call *these* methods. Duplicating logic between them is a
defect, so the handlers are thin by construction: they choose a representation, never compute one.

**Absence of data is not absence of signals.** An empty panel means "healthy market, no setups"; a
failed query means "we don't know". Conflating them tells the user the market is quiet when the system
is blind, which specs/14 §10 calls the single most dangerous thing this API can do. Every service
method therefore returns a result object carrying its own status, and a failure sets ``DOWN`` with a
reason rather than returning an empty list.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.errors import ALL_ERRORS, BadRequest, CryptoTraderError, ServiceUnavailable
from core.health import HealthReport, SystemStatus
from core.logging import get_logger

log = get_logger("api.services")
COMPONENT = "api.services"

#: Interval lengths in milliseconds, for measuring a bar's age from its **close**. A 1h bar that
#: opened 59 minutes ago is current, not an hour old, and reporting it as an hour old would make a
#: healthy feed look stale on every panel that shows freshness.
_TIMEFRAME_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
                 "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}


def _timeframe_ms(timeframe: str) -> int:
    return _TIMEFRAME_MS.get(timeframe, 0)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class FreshnessEnvelope:
    """Attached to every response (specs/14 §3).

    ``age_seconds`` is computed from ``data_as_of_ms``, **never** from when a cache entry was created.
    A cached response that reports itself as fresh turns a visible outage into an invisible one, so the
    cache stores this envelope alongside the payload and the age is recomputed on serve.
    """

    served_at_ms: int
    data_as_of_ms: int | None
    system_status: SystemStatus
    sources: dict[str, Any] = field(default_factory=dict)
    degraded_reasons: list[str] = field(default_factory=list)
    correlation_id: str = ""

    @property
    def age_seconds(self) -> float | None:
        if self.data_as_of_ms is None:
            return None
        return max(0.0, (self.served_at_ms - self.data_as_of_ms) / 1000)

    def to_dict(self) -> dict[str, Any]:
        return {"served_at_ms": self.served_at_ms, "data_as_of_ms": self.data_as_of_ms,
                "age_seconds": self.age_seconds, "system_status": self.system_status.value,
                "sources": dict(self.sources), "degraded_reasons": list(self.degraded_reasons),
                "correlation_id": self.correlation_id}


@dataclass
class ServiceResult:
    """A payload plus the envelope that says how much to trust it."""

    payload: Any
    envelope: FreshnessEnvelope
    error: CryptoTraderError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict[str, Any]:
        out = {"envelope": self.envelope.to_dict(), "data": self.payload}
        if self.error is not None:
            # Structured, never a stack trace; the correlation id ties it to the logs.
            out["error"] = {**self.error.to_dict(),
                            "correlation_id": self.envelope.correlation_id}
        return out


@dataclass
class BaseService:
    store: Any
    health_source: Any = None

    def _envelope(self, data_as_of_ms: int | None, *, status: SystemStatus | None = None,
                  reasons: list[str] | None = None) -> FreshnessEnvelope:
        report = self._health_report()
        return FreshnessEnvelope(
            served_at_ms=_now_ms(), data_as_of_ms=data_as_of_ms,
            system_status=status or (report.status if report else SystemStatus.HEALTHY),
            sources={c.name: {"status": c.status.value, "detail": c.detail,
                              "age_s": c.age_seconds, "problem_class":
                                  c.problem_class.value if c.problem_class else None}
                     for c in (report.components if report else ())},
            degraded_reasons=reasons or (list(report.degraded_reasons) if report else []),
            correlation_id=uuid.uuid4().hex[:12])

    def _health_report(self) -> HealthReport | None:
        source = self.health_source
        if source is None:
            return None
        try:
            return source() if callable(source) else source
        except ALL_ERRORS as e:
            log.error("health_source_failed", error=e.to_dict())
            return None

    def _failed(self, operation: str, e: CryptoTraderError, *, payload: Any) -> ServiceResult:
        """A failure returns ``DOWN`` with the reason and an *explicitly empty* payload shape.

        The payload keeps its type — an empty list stays a list — so a caller's rendering does not
        also have to handle None, but the envelope says DOWN so nobody reads the emptiness as calm.
        """
        envelope = self._envelope(None, status=SystemStatus.DOWN,
                                  reasons=[f"{operation}: {e.message}"])
        log.error("service_failed", operation=operation, correlation_id=envelope.correlation_id,
                  error=e.to_dict())
        return ServiceResult(payload=payload, envelope=envelope, error=e)


@dataclass
class SignalService(BaseService):
    """Reads signals, their detail, their audit record and their history."""

    def live(self, *, asset: str | None = None, include_untradeable: bool = False) -> ServiceResult:
        """The current set. Control-strategy signals are filtered out here as well as upstream.

        specs/13 §13 requires that no endpoint ever returns a signal with ``tradeable = False``; the
        filter lives at the boundary as well as in the runner so that forgetting one does not leak.
        """
        try:
            rows = self.store.conn.execute(
                """SELECT * FROM signals
                   WHERE state IN ('SETUP_DETECTED','ENTER_NOW','TRIGGERED')
                   """ + ("AND asset = ? " if asset else "") + """
                   ORDER BY updated_at_ms DESC LIMIT 200""",
                ((asset,) if asset else ())).fetchall()
            signals = [self._row_to_signal(r) for r in rows]
            if not include_untradeable:
                signals = [s for s in signals if s.get("tradeable", True)]
            # Falls back to the newest stored bar when there are no signals, and that fallback is
            # the point rather than a nicety.
            #
            # A quiet market is the normal state, so "no signals" is the common case — and with no
            # signal to date the envelope reported `age_seconds: null`, which the dashboard rendered
            # as **"market data age unknown"**. That is the one thing a reader must never be left
            # with: unable to tell a healthy quiet market from a feed that stopped hours ago. Both
            # show no suggestions; only the age distinguishes them (F-045).
            #
            # The newest bar's close is the right answer because it is what the evaluation was based
            # on. Its *close*, not its open: a 1h bar that opened 59 minutes ago is current.
            newest = max((s["as_of_ms"] for s in signals), default=None)
            basis = "newest signal"
            if newest is None:
                newest, basis = self._newest_bar_close_ms(), "newest stored bar"

            # **Two different ages, and conflating them produced a permanent false alarm.**
            #
            # The envelope's age is the *payload's* age, which for this endpoint is the newest
            # signal's `as_of_ms` — the moment the newest setup was last written. A setup at an
            # hourly horizon legitimately stays armed for hours, so that number grows with the
            # setup's age and has nothing to do with whether the feed is alive. The dashboard
            # labelled it "market data" and judged it against a 300-second red threshold, so a
            # healthy system with a 12-minute-old setup reported **"too old to act on"** (F-052).
            #
            # So market-data freshness is published separately, measured from the newest 1-minute
            # bar close — the fastest series the engine actually polls, and therefore the one whose
            # age says whether data is still arriving. The 60/300-second thresholds are meaningful
            # against that and meaningless against the other.
            market = self._newest_bar_close_ms(timeframe="1m")
            return ServiceResult(payload={"signals": signals, "count": len(signals),
                                          "by_asset": self._group(signals),
                                          "freshness_basis": basis,
                                          "market_data": {
                                              "as_of_ms": market,
                                              "age_s": (None if market is None
                                                        else max(0.0, (_now_ms() - market) / 1000)),
                                              "basis": "newest 1m bar close",
                                              "interval_s": _timeframe_ms("1m") / 1000}},
                                 envelope=self._envelope(newest))
        except ALL_ERRORS as e:
            return self._failed("signals.live", e, payload={"signals": [], "count": 0,
                                                            "by_asset": {}})

    def _newest_bar_close_ms(self, *, timeframe: str = "1h") -> int | None:
        """The close time of the newest bar of ``timeframe`` across all assets, or ``None``.

        Deliberately tolerant: a failure here must not turn a working signals response into a
        failed one, because this is a secondary reading used only to date the envelope. If it cannot
        be read, the envelope says "unknown" exactly as it did before — which is honest, just less
        useful.
        """
        if timeframe not in _TIMEFRAME_MS:
            return None
        try:
            row = self.store.conn.execute(
                "SELECT MAX(open_time_ms) AS t FROM bars WHERE timeframe = ?",
                (timeframe,)).fetchone()
        except Exception:                               # noqa: BLE001 - secondary reading only
            return None
        if row is None or row["t"] is None:
            return None
        return int(row["t"]) + _timeframe_ms(timeframe)

    def detail(self, signal_id: str) -> ServiceResult:
        try:
            row = self.store.conn.execute("SELECT * FROM signals WHERE signal_id = ?",
                                          (signal_id,)).fetchone()
            if row is None:
                raise ServiceUnavailable(
                    f"no signal {signal_id}",
                    component=COMPONENT, operation="detail",
                    context={"signal_id": signal_id},
                    likely_cause="the id is wrong, or the signal predates the current store",
                    remediation="list /api/signals/history to find valid ids")
            signal = self._row_to_signal(row)
            events = [dict(r) for r in self.store.conn.execute(
                "SELECT * FROM signal_events WHERE signal_id = ? ORDER BY at_ms", (signal_id,))]
            return ServiceResult(payload={"signal": signal, "events": events},
                                 envelope=self._envelope(signal["as_of_ms"]))
        except ALL_ERRORS as e:
            return self._failed("signals.detail", e, payload=None)

    def audit(self, signal_id: str) -> ServiceResult:
        try:
            row = self.store.conn.execute(
                "SELECT * FROM signal_audit WHERE signal_id = ? ORDER BY as_of_ms DESC LIMIT 1",
                (signal_id,)).fetchone()
            if row is None:
                raise ServiceUnavailable(
                    f"no audit record for {signal_id}",
                    component=COMPONENT, operation="audit",
                    context={"signal_id": signal_id},
                    likely_cause="the signal was produced before the audit trail was wired, or the id "
                                 "is wrong",
                    remediation="every signal written since PHASE-22 carries one")
            record = dict(row)
            record["record"] = json.loads(record["record"])
            return ServiceResult(payload=record, envelope=self._envelope(record["as_of_ms"]))
        except ALL_ERRORS as e:
            return self._failed("signals.audit", e, payload=None)

    def history(self, *, cursor: tuple[int, str] | None = None, limit: int = 50) -> ServiceResult:
        """Cursor-paginated on ``(created_at_ms, signal_id)``.

        Offset pagination is rejected by specs/14 §11 because it skips or duplicates rows when new
        signals arrive mid-page — and this table is written to while it is read.
        """
        try:
            if cursor:
                rows = self.store.conn.execute(
                    """SELECT * FROM signals WHERE (created_at_ms, signal_id) < (?, ?)
                       ORDER BY created_at_ms DESC, signal_id DESC LIMIT ?""",
                    (cursor[0], cursor[1], limit)).fetchall()
            else:
                rows = self.store.conn.execute(
                    "SELECT * FROM signals ORDER BY created_at_ms DESC, signal_id DESC LIMIT ?",
                    (limit,)).fetchall()
            signals = [self._row_to_signal(r) for r in rows]
            next_cursor = ((signals[-1]["created_at_ms"], signals[-1]["signal_id"])
                           if len(signals) == limit else None)
            return ServiceResult(
                payload={"signals": signals, "next_cursor": next_cursor},
                envelope=self._envelope(max((s["as_of_ms"] for s in signals), default=None)))
        except ALL_ERRORS as e:
            return self._failed("signals.history", e, payload={"signals": [], "next_cursor": None})

    def events_since(self, since_ms: int, *, limit: int = 200) -> ServiceResult:
        try:
            rows = self.store.conn.execute(
                "SELECT * FROM signal_events WHERE at_ms > ? ORDER BY at_ms, id LIMIT ?",
                (since_ms, limit)).fetchall()
            events = [dict(r) for r in rows]
            return ServiceResult(payload={"events": events},
                                 envelope=self._envelope(max((e["at_ms"] for e in events),
                                                             default=None)))
        except ALL_ERRORS as e:
            return self._failed("signals.events", e, payload={"events": []})

    def _row_to_signal(self, row: Any) -> dict[str, Any]:
        """One signal, with `detail` **lifted to the top level**.

        `detail` is the part every consumer reads — it carries `price_basis`, `pending_direction`,
        `validated`, `legs`, `breakeven_hit_rate` — and it was reachable only as
        ``signal.payload.detail``. Two of the three readers got that wrong: `render.py` reads
        ``payload["detail"]`` correctly, while the Vercel dashboard read ``signal.detail`` and
        therefore read `undefined` **every time**, silently. Its control-signal firewall never
        fired, and its "never validated" notice was right only by accident (F-056).

        Nesting that one reader in three gets wrong is a bad shape, so it is flattened here rather
        than corrected in each reader. `payload` is still published unchanged, because the audit
        path and older clients read it.
        """
        out = dict(row)
        payload = json.loads(out.pop("payload", "{}") or "{}")
        out["payload"] = payload
        out["detail"] = payload.get("detail") or {}
        out["tradeable"] = bool(payload.get("tradeable", True))
        out["targets"] = json.loads(out.get("targets") or "[]")
        return out

    def _group(self, signals: list[dict[str, Any]]) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for s in signals:
            grouped.setdefault(s["asset"], []).append(s["signal_id"])
        return grouped


@dataclass
class MarketService(BaseService):
    """Per-asset state, the regime, and the rejection record that answers "why is there no signal?"."""

    def asset_state(self, asset: str) -> ServiceResult:
        try:
            row = self.store.conn.execute(
                """SELECT * FROM bars WHERE asset = ? AND timeframe = '1h' AND is_final = 1
                   ORDER BY open_time_ms DESC LIMIT 1""", (asset,)).fetchone()
            latest = dict(row) if row else None
            rt = self.store.conn.execute(
                "SELECT * FROM futures_rt WHERE asset = ? ORDER BY ts_ms DESC LIMIT 1",
                (asset,)).fetchone()
            book = self.store.conn.execute(
                """SELECT * FROM book_snapshots WHERE asset = ? AND product = 'futures'
                   ORDER BY ts_ms DESC LIMIT 1""", (asset,)).fetchone()
            payload = {"asset": asset, "last_bar": latest,
                       "futures_rt": dict(rt) if rt else None,
                       "book": {k: dict(book)[k] for k in ("ts_ms", "version")} if book else None}
            as_of = latest["open_time_ms"] if latest else None
            return ServiceResult(payload=payload, envelope=self._envelope(as_of))
        except ALL_ERRORS as e:
            return self._failed("market.asset_state", e, payload={"asset": asset})

    def rejections(self, asset: str, *, limit: int = 50) -> ServiceResult:
        """specs/14 §7 — the endpoint most systems cannot offer, because they never recorded this."""
        try:
            rows = self.store.conn.execute(
                """SELECT * FROM rejections WHERE asset = ? ORDER BY at_ms DESC LIMIT ?""",
                (asset, limit)).fetchall()
            rejections = []
            for r in rows:
                item = dict(r)
                item["conditions"] = json.loads(item.get("conditions") or "[]")
                rejections.append(item)
            binding = self.store.conn.execute(
                """SELECT strategy_id, binding_condition, COUNT(*) AS n FROM rejections
                   WHERE asset = ? AND binding_condition IS NOT NULL
                   GROUP BY strategy_id, binding_condition ORDER BY n DESC LIMIT 10""",
                (asset,)).fetchall()
            return ServiceResult(
                payload={"asset": asset, "rejections": rejections,
                         "most_binding": [dict(b) for b in binding]},
                envelope=self._envelope(max((r["as_of_ms"] for r in rejections), default=None)))
        except ALL_ERRORS as e:
            return self._failed("market.rejections", e,
                                payload={"asset": asset, "rejections": [], "most_binding": []})

    def candles(self, asset: str, *, timeframe: str = "1h", limit: int = 500,
                product: str = "futures") -> ServiceResult:
        """OHLCV for a chart, newest last.

        The endpoint exists for one reason worth stating: **a person needs to be able to see whether
        the data is arriving at all.** Every other panel on the dashboard reports conclusions — no
        suggestions, a regime cell, a health status — and all of them look the same whether the
        prices behind them are current, stale or absent. A candle chart is the only view where
        wrong data is obvious at a glance.

        Non-final bars are included and **flagged**. The feature layer refuses to compute on them
        (hard invariant: a bar that has been fetched once is not yet confirmed), but hiding them from
        a chart would make the most recent hour look missing, which is the opposite of the honesty
        this view is for. The chart draws them differently and says so.
        """
        try:
            allowed = {"1m", "5m", "15m", "30m", "1h", "4h", "1d"}
            if timeframe not in allowed:
                # ``BadRequest``, deliberately: the caller asked for an interval that does not
                # exist, which is not an outage. Raised as ``ServiceUnavailable`` it would be
                # classified ``infrastructure`` and answered 503, sending the operator to check the
                # VM, the firewall and the service logs for a mistake that is in the query string.
                raise BadRequest(
                    f"unknown timeframe {timeframe!r}",
                    component=COMPONENT, operation="candles",
                    context={"requested": timeframe, "allowed": sorted(allowed)},
                    likely_cause="the chart asked for an interval this system does not store",
                    remediation=f"choose one of {sorted(allowed)}")
            limit = max(1, min(int(limit), 5000))

            rows = self.store.conn.execute(
                """SELECT open_time_ms, open, high, low, close, volume_base, is_final
                   FROM bars WHERE asset = ? AND product = ? AND timeframe = ?
                   ORDER BY open_time_ms DESC LIMIT ?""",
                (asset, product, timeframe, limit)).fetchall()

            # Reversed into chronological order here rather than in the chart: ordering is a
            # property of the data and belongs where the data is produced (specs/10 §5), and a
            # client that forgets to sort would draw a mirror image of the market.
            candles = [
                {"t": int(r["open_time_ms"]),
                 # Strings, not floats. The store keeps decimals as TEXT precisely so no rounding
                 # happens on the way through, and JSON numbers are floats — so the exact value is
                 # preserved and the chart parses what it needs for pixels only.
                 "o": r["open"], "h": r["high"], "l": r["low"], "c": r["close"],
                 "v": r["volume_base"], "final": bool(r["is_final"])}
                for r in reversed(rows)
            ]

            total = self.store.conn.execute(
                "SELECT COUNT(*) AS n FROM bars WHERE asset = ? AND product = ? AND timeframe = ?",
                (asset, product, timeframe)).fetchone()["n"]

            newest = candles[-1]["t"] if candles else None
            return ServiceResult(
                payload={
                    "asset": asset, "product": product, "timeframe": timeframe,
                    "candles": candles, "count": len(candles), "stored_total": int(total),
                    "oldest_ms": candles[0]["t"] if candles else None,
                    "newest_ms": newest,
                    "pending_confirmation": sum(1 for c in candles if not c["final"]),
                    "note": ("bars marked final: false have been fetched once and not yet confirmed "
                             "by a second fetch. The feature layer will not compute on them; they "
                             "are drawn so the most recent interval does not look missing"),
                },
                # The envelope's age is measured from the newest bar's **close**, not its open: a
                # 1h bar that opened 59 minutes ago is current, not an hour old.
                envelope=self._envelope(
                    (newest + _timeframe_ms(timeframe)) if newest is not None else None))
        except ALL_ERRORS as e:
            return self._failed("market.candles", e,
                                payload={"asset": asset, "timeframe": timeframe,
                                         "candles": [], "count": 0, "stored_total": 0})

    # ---- what are we actually fetching? ------------------------------------------------------

    #: Every point-in-time source, as ``(id, table, time_column, label, tier, fields)``.
    #:
    #: Declared as data rather than written out as six near-identical queries, because the thing a
    #: reader needs from this page is a *comparison* — this source is current, that one stopped —
    #: and six hand-written blocks drift until the comparison stops being one.
    #:
    #: ``tier`` is the distinction that matters most and is the easiest to lose: **Tier A can be
    #: rebuilt from the venue, Tier B cannot.** A gap in 1h candles is an inconvenience; a gap in
    #: funding or positioning is permanent, because no endpoint serves yesterday's order book
    #: (specs/00 §5). The page says so next to each source rather than in a footnote.
    _PIT_SOURCES = (
        ("futures_rt", "futures_rt", "source_ts_ms", "Futures real-time (funding, mark, last)", "B",
         ("funding_rate", "mark_price", "last_price")),
        ("spot_ticker", "spot_ticker", "ts_ms", "Spot ticker", "B", ("last_price", "bid", "ask")),
        ("positioning", "positioning", "ts_ms", "Long/short positioning", "B",
         ("long_count_pct", "short_count_pct")),
        ("book_snapshots", "book_snapshots", "ts_ms", "Order book snapshots", "B",
         ("best_bid", "best_ask", "levels")),
        # Listed, with ``collected=False``, rather than omitted. The table exists in the schema and
        # `adapters.spot_trades` exists, but no poll task writes it — so an empty table here is the
        # system working as built. Omitting the row would leave a reader wondering whether trades
        # were being recorded; showing it with a bare zero would suggest a broken poller. Saying
        # "not collected" is the only one of the three that is true.
        ("trades", "trades", "ts_ms", "Trades (tick data)", "B", ("price", "qty"), False),
        ("instrument_meta", "instrument_meta", "fetched_at_ms",
         "Instrument metadata (tick, fees, limits)", "A",
         ("tick", "taker_fee_fraction", "min_notional")),
    )

    def sources(self) -> ServiceResult:
        """Every data source, with its row count, its newest row and that row's last value.

        **The page this feeds answers one question: are we fetching what we think we are fetching?**
        Nothing else in the system answers it. Health reports whether components are *working*,
        which is not the same — a poller that has been failing for an hour is DEGRADED, but a poller
        that succeeds while the venue returns the same frozen row is HEALTHY, and the only way to see
        that is to look at the data's own timestamps.

        Deliberately a summary and not a dump: the last value of each source, not its history. A page
        that returned 3 million 1-minute bars would be slower than the thing it is diagnosing, and
        nobody reads the millionth row.

        Each source is reported independently and a failure on one does not fail the response — one
        unreadable table must not hide the five that are fine, which is the whole point of looking.
        The error is attached **to that source**, so it is visible rather than absorbed.
        """
        now = _now_ms()
        out: dict[str, Any] = {"as_of_ms": now, "series": [], "point_in_time": [], "errors": []}
        try:
            out["series"] = self._series_sources(now)
        except ALL_ERRORS as e:
            out["errors"].append({"source": "bars", **e.to_dict()})
        except Exception as e:                           # noqa: BLE001 - one table must not hide five
            out["errors"].append({"source": "bars", "message": str(e),
                                  "problem_class": "application_bug"})

        for spec in self._PIT_SOURCES:
            sid, table, tcol, label, tier, fields = spec[:6]
            collected = spec[6] if len(spec) > 6 else True
            try:
                out["point_in_time"].append(
                    self._pit_source(sid, table, tcol, label, tier, fields, now,
                                     collected=bool(collected)))
            except Exception as e:                       # noqa: BLE001 - per source, never fatal
                out["point_in_time"].append({
                    "id": sid, "label": label, "tier": tier, "kind": "point_in_time",
                    "collected": bool(collected),
                    "readable": False, "rows": None, "newest_ms": None, "age_s": None,
                    "error": {"message": str(e), "problem_class": "application_bug",
                              "likely_cause": f"the {table} table could not be read",
                              "remediation": "check the schema version on this host"}})

        try:
            out["data_quality"] = self._dq_summary(now)
        except Exception as e:                           # noqa: BLE001 - secondary reading
            out["data_quality"] = {"readable": False, "message": str(e)}

        newest = max((s["newest_ms"] for s in out["series"] if s.get("newest_ms")), default=None)
        return ServiceResult(payload=out, envelope=self._envelope(newest))

    def _series_sources(self, now: int) -> list[dict[str, Any]]:
        """One row per (asset, product, timeframe) actually present in `bars`.

        Enumerated from the data rather than from a declared list on purpose: a series the engine
        believes it is recording but is not will simply be **absent here**, which is the finding. A
        declared list would print it with a zero beside it and look like a configuration note.
        """
        groups = self.store.conn.execute(
            """SELECT asset, product, timeframe, COUNT(*) AS rows,
                      MAX(open_time_ms) AS newest, MIN(open_time_ms) AS oldest,
                      SUM(CASE WHEN is_final = 0 THEN 1 ELSE 0 END) AS pending
               FROM bars GROUP BY asset, product, timeframe
               ORDER BY asset, product, timeframe""").fetchall()
        out = []
        for g in groups:
            tf = g["timeframe"]
            newest = int(g["newest"]) if g["newest"] is not None else None
            last = self.store.conn.execute(
                """SELECT open, high, low, close, volume_base, is_final FROM bars
                   WHERE asset = ? AND product = ? AND timeframe = ? AND open_time_ms = ?""",
                (g["asset"], g["product"], tf, g["newest"])).fetchone()
            # Age from the bar's **close**: a 1h bar that opened 59 minutes ago is current.
            close_ms = (newest + _timeframe_ms(tf)) if newest is not None else None
            out.append({
                "id": f"{g['product']}_candles_{tf}", "kind": "ohlc", "tier": "A",
                "label": f"{g['product'].title()} candles {tf}",
                "asset": g["asset"], "product": g["product"], "timeframe": tf,
                "rows": int(g["rows"]), "newest_ms": close_ms, "oldest_ms": int(g["oldest"]),
                "pending_confirmation": int(g["pending"] or 0),
                "age_s": None if close_ms is None else max(0.0, (now - close_ms) / 1000),
                # One interval is the natural yardstick: a series cannot be fresher than its own bar
                # length, so expressing lateness in intervals is the only reading that compares
                # across timeframes. Seconds alone make every daily series look catastrophic.
                "interval_s": _timeframe_ms(tf) / 1000 or None,
                "intervals_behind": (None if close_ms is None or not _timeframe_ms(tf)
                                     else max(0, int((now - close_ms) // _timeframe_ms(tf)))),
                "last_value": ({"o": last["open"], "h": last["high"], "l": last["low"],
                                "c": last["close"], "v": last["volume_base"],
                                "final": bool(last["is_final"])} if last else None),
                "readable": True,
            })
        return out

    def _pit_source(self, sid: str, table: str, tcol: str, label: str, tier: str,
                    fields: tuple[str, ...], now: int, *, collected: bool = True
                    ) -> dict[str, Any]:
        """One point-in-time source, per asset where the table has an asset column.

        ``rows_last_hour`` is **measured, not declared.** The alternative was to publish the
        configured poll interval and compare against it, which reports what the system was told to
        do; this reports what it did. A source configured for every 15 seconds and delivering four
        rows an hour is exactly the failure this page exists to surface, and only the measurement
        shows it.
        """
        cols = {r["name"] for r in self.store.conn.execute(f"PRAGMA table_info({table})")}
        has_asset = "asset" in cols
        select = ", ".join(f for f in fields if f in cols)
        hour_ago = now - 3_600_000

        def one(asset: str | None) -> dict[str, Any]:
            where = "WHERE asset = ?" if asset else ""
            args: tuple[Any, ...] = (asset,) if asset else ()
            agg = self.store.conn.execute(
                f"SELECT COUNT(*) AS rows, MAX({tcol}) AS newest FROM {table} {where}",
                args).fetchone()
            newest = int(agg["newest"]) if agg and agg["newest"] is not None else None
            recent = self.store.conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} "
                f"{'WHERE asset = ? AND' if asset else 'WHERE'} {tcol} >= ?",
                (*args, hour_ago)).fetchone()
            last = None
            if newest is not None and select:
                row = self.store.conn.execute(
                    f"SELECT {select} FROM {table} {where} {'AND' if asset else 'WHERE'} "
                    f"{tcol} = ? LIMIT 1", (*args, newest)).fetchone()
                # `sqlite3.Row` is not a dict, so `.keys()` is the API rather than a lint slip.
                last = dict(zip(row.keys(), tuple(row), strict=True)) if row else None
            return {"asset": asset, "rows": int(agg["rows"] if agg else 0), "newest_ms": newest,
                    "age_s": None if newest is None else max(0.0, (now - newest) / 1000),
                    "rows_last_hour": int(recent["n"] if recent else 0), "last_value": last}

        assets: list[str | None] = [None]
        if has_asset:
            found = self.store.conn.execute(
                f"SELECT DISTINCT asset FROM {table} ORDER BY asset").fetchall()
            assets = [r["asset"] for r in found] or [None]
        return {"id": sid, "label": label, "tier": tier, "kind": "point_in_time",
                "readable": True, "collected": collected,
                "per_asset": [one(a) for a in assets],
                "rebuildable": tier == "A",
                "note": ("this system does not poll this source; the table exists but nothing "
                         "writes it, so an empty row here is correct and not a failure"
                         if not collected else
                         "rebuildable from the venue if lost" if tier == "A" else
                         "point-in-time only: there is no archive endpoint, so a gap here is "
                         "permanent for everyone, with any API key")}

    def _dq_summary(self, now: int) -> dict[str, Any]:
        """Data-quality events in the last 24 hours, by kind.

        On this page because a source can be current and wrong: `ROWS_REJECTED` rising while the
        timestamps stay fresh means the venue changed something and we are discarding rows. A
        freshness check alone would call that healthy.
        """
        rows = self.store.conn.execute(
            """SELECT kind, severity, COUNT(*) AS n, MAX(at_ms) AS newest FROM dq_events
               WHERE at_ms >= ? GROUP BY kind, severity ORDER BY n DESC LIMIT 20""",
            (now - 86_400_000,)).fetchall()
        return {"readable": True, "window_h": 24,
                "events": [{"kind": r["kind"], "severity": r["severity"], "count": int(r["n"]),
                            "newest_ms": int(r["newest"])} for r in rows]}

    def regime(self, regimes: dict[str, Any] | None = None) -> ServiceResult:
        """Regime state per asset. Supplied by the runner rather than recomputed — the API never
        computes (specs/14 §1)."""
        payload = {"assets": regimes or {},
                   "note": "regime state is written by the engine; the API reads it"}
        reasons = [f"{a}: regime degraded" for a, r in (regimes or {}).items()
                   if isinstance(r, dict) and r.get("degraded")]
        return ServiceResult(payload=payload,
                             envelope=self._envelope(
                                 max((r.get("as_of_ms", 0) for r in (regimes or {}).values()),
                                     default=None) or None,
                                 status=SystemStatus.DEGRADED if reasons else None,
                                 reasons=reasons))


@dataclass
class HealthService(BaseService):
    """Health and warm-up. Warm-up is a **state**, not an error (specs/14 §5)."""

    def health(self) -> ServiceResult:
        report = self._health_report()
        if report is None:
            e = ServiceUnavailable(
                "no health source is attached to the API",
                component=COMPONENT, operation="health",
                likely_cause="the app was constructed without passing the runner's health callable",
                remediation="pass health_source=runner.health when building the services")
            return self._failed("health", e, payload={"components": []})
        return ServiceResult(payload=report.to_dict(),
                             envelope=self._envelope(None, status=report.status,
                                                     reasons=list(report.degraded_reasons)))

    def warmup(self, feature_sets: dict[str, Any] | None = None) -> ServiceResult:
        """Per-asset warm-up: what is still warming, and how much longer.

        A warming system is not a broken one, and the dashboard must be able to say which.
        """
        assets: dict[str, Any] = {}
        for asset, fs in (feature_sets or {}).items():
            warming = [v.to_dict() for v in fs if v.status.value == "WARMING"]
            assets[asset] = {
                "computed": len(fs), "ok": fs.ok_count,
                "statuses": fs.status_counts(),
                "warming": warming[:20],
                "hours_to_warm": max((v.get("detail", {}).get("hours_to_warm", 0) or 0
                                      for v in warming), default=0),
            }
        any_warming = any(a["statuses"].get("WARMING") for a in assets.values())
        return ServiceResult(
            payload={"assets": assets, "warming": any_warming},
            envelope=self._envelope(None,
                                    status=SystemStatus.WARMING if any_warming else None,
                                    reasons=["features are still warming"] if any_warming else []))


def build_services(store: Any, *, health_source: Any = None
                   ) -> tuple[SignalService, MarketService, HealthService]:
    return (SignalService(store=store, health_source=health_source),
            MarketService(store=store, health_source=health_source),
            HealthService(store=store, health_source=health_source))
