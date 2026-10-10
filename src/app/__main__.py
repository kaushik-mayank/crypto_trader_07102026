"""Run the app: evaluate on a cadence, serve the dashboard, report health.

    python -m app                 # evaluate once, print the result, exit
    python -m app --serve         # evaluate on a cadence and serve the dashboard
    python -m app --check         # preflight only: prove the system could start, then exit

``--check`` exists because the most valuable thing to know before a deployment is whether startup
*would* succeed, and finding out by starting the real thing is a worse way to learn it.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

from api.app import create_app
from api.services import build_services
from app.runner import AppRunner, load_classifiers
from audit.trail import AuditTrail
from config.parameters import load_registry
from config.settings import Settings
from core.errors import ALL_ERRORS, StartupError
from core.health import HealthReport
from core.logging import get_logger, setup_logging
from features.engine import FeatureEngine
from normalize.canonical import Product, Timeframe
from risk.engine import RiskEngine
from snapshot.builder import WARMUP_BARS, SeriesRequest, SnapshotBuilder, SnapshotRequest
from snapshot.models import InstrumentKey, SeriesKey
from store import open_store
from store.reader import ReadOnlyStore
from strategy.registry import live_strategies

log = get_logger("app.main")

ASSETS = ("B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT")
#: The trigger timeframes ACT needs beyond the alpha set: 15m sizes the unit, 1m confirms.
TRIGGER_BARS = {Timeframe.M15: 200, Timeframe.M1: 240}


def build_request(assets=ASSETS, product=Product.FUTURES) -> SnapshotRequest:
    series = [SeriesRequest(SeriesKey(a, product, tf), bars=WARMUP_BARS[tf])
              for a in assets for tf in (Timeframe.H1, Timeframe.H4, Timeframe.D1)]
    series += [SeriesRequest(SeriesKey(a, product, tf), bars=n)
               for a in assets for tf, n in TRIGGER_BARS.items()]
    return SnapshotRequest(
        series=tuple(series),
        tier_b=tuple((s, a) for a in assets
                     for s in ("futures_rt", "spot_ticker", "positioning", "futures_book",
                               "spot_book")),
        instruments=tuple(InstrumentKey(a, product) for a in assets),
        forming=tuple(SeriesKey(a, product, Timeframe.H1) for a in assets),
        tier_b_history_ms=7 * 86_400_000,
    )


def build_runner(store: Any, settings: Settings) -> AppRunner:
    registry = load_registry(settings.paths.parameters_file)
    params = {p.name: p.value for p in registry.parameters}
    unset = frozenset(p.name for p in registry.parameters if p.status == "unset")
    engine = FeatureEngine(params, parameter_hash=registry.parameter_hash, unset_params=unset)
    runner = AppRunner(
        builder=SnapshotBuilder.from_settings(store, settings, registry.parameter_hash),
        engine=engine, request=build_request(),
        strategies=live_strategies(params),
        risk=RiskEngine(params=params),
        classifiers=load_classifiers(ASSETS, directory=str(Path(settings.paths.parameters_file).parent)),
        assets=ASSETS,
    )
    runner.preflight()
    return runner


def _serve(runner: AppRunner, settings: Settings, evaluate_once: Any, *,
           interval_s: float) -> int:
    """Serve the API **and keep evaluating** on a cadence.

    The two have to happen together, and getting that wrong is subtle enough to be worth spelling
    out. The first version called ``uvicorn.run()`` after a single evaluation; ``uvicorn.run()``
    blocks forever, so the system produced one set of suggestions at startup and then served that
    same frozen evaluation indefinitely. On a 72-hour observation run you would have watched one
    moment for three days, and the only thing on the dashboard that moved would have been the age
    counter — which is precisely why the freshness envelope exists, but it is a poor way to find out
    (F-043).

    **uvicorn runs on a worker thread and the evaluation loop keeps the main thread.** That ordering
    is deliberate: the writable SQLite connection was opened on the main thread, and a ``sqlite3``
    connection may only be used by the thread that created it. Running the evaluation on a worker
    would hit exactly the error that ``store/reader.py`` was written to avoid. The API's reads go
    through :class:`store.reader.ReadOnlyStore`, which already opens one read-only connection per
    thread, so the worker serves happily from its own.
    """
    import signal
    import threading

    import uvicorn

    reader = ReadOnlyStore(settings.paths.state_db)

    def api_health() -> Any:
        """Health for a request thread: the runner's view, plus the API's *own* read connection.

        ``include_store=False`` because the writer's sqlite connection belongs to the main thread
        and a sync handler runs in a worker — querying it from a request is the cross-thread error
        in F-043. The reader's own check replaces it and is the more useful number: it tests the
        connection this request would actually read through.
        """
        report = runner.health(include_store=False)
        return HealthReport(components=[reader.health(), *report.components])

    signals, market, health = build_services(reader, health_source=api_health)
    app = create_app(signals=signals, market=market, health=health,
                     host=settings.server.host, auth_token=settings.server.auth_token,
                     public_via_tunnel=settings.server.public_via_tunnel,
                     regimes_source=lambda: {a: c.stats() for a, c in runner.classifiers.items()})

    config = uvicorn.Config(app, host=settings.server.host, port=settings.server.port,
                            log_level="warning")
    server = uvicorn.Server(config)
    # No need to disable uvicorn's signal handling: it checks for the main thread itself and skips
    # installation on a worker, because `signal.signal` only works on the main thread. So the loop
    # below owns shutdown for both, which is what we want — the server must not stop while an
    # evaluation is mid-write.
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()

    stopping = threading.Event()

    def stop(signum: int, _frame: Any) -> None:
        log.info("shutdown_requested", signal=signum,
                 detail="finishing the current evaluation, then stopping")
        stopping.set()
        server.should_exit = True

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                signal.signal(sig, stop)
            except (OSError, ValueError):            # pragma: no cover - platform guard
                pass

    log.info("serving", host=settings.server.host, port=settings.server.port,
             interval_s=interval_s,
             detail="the API is served on a worker thread while this thread keeps evaluating")

    while not stopping.is_set():
        started = time.monotonic()
        try:
            evaluate_once()
        except ALL_ERRORS as e:
            # One failed evaluation must not end the run. The runner already records the failure and
            # health already reports it; stopping here would turn a transient data problem into an
            # outage, and an outage is what nobody is watching for.
            log.error("evaluation_failed", error=e.to_dict(),
                      detail="the loop continues; health reports this")
        except Exception as e:                        # noqa: BLE001 - the loop must survive
            log.error("evaluation_crashed", error=str(e), error_type=type(e).__name__,
                      detail="unclassified failure; the loop continues so the API stays up")
        if not thread.is_alive() and not stopping.is_set():
            log.error("api_thread_died",
                      detail="the API stopped serving while the evaluation loop was running")
            return 1
        # Sleep the remainder of the interval, so a slow evaluation does not push the cadence out.
        stopping.wait(max(0.0, interval_s - (time.monotonic() - started)))

    server.should_exit = True
    thread.join(timeout=10)
    log.info("stopped", evaluations=runner.evaluations)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="crypto-trader suggestion engine")
    ap.add_argument("--serve", action="store_true", help="serve the dashboard after each evaluation")
    ap.add_argument("--check", action="store_true", help="preflight only, then exit")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between evaluations")
    ap.add_argument("--once", action="store_true", help="evaluate once and exit")
    args = ap.parse_args(argv)

    settings = Settings()
    setup_logging(settings.paths.log_dir, log_name="app")
    store = open_store(settings.paths.state_db, migrate=False)

    try:
        runner = build_runner(store, settings)
    except StartupError as e:
        # Deterministic and loud: every problem at once, on stderr, with a non-zero exit.
        print(f"STARTUP FAILED: {e.message}", file=sys.stderr)
        for problem in e.context.get("problems", []):
            print(f"  - {problem}", file=sys.stderr)
        print(f"  fix: {e.remediation}", file=sys.stderr)
        return 2
    except ALL_ERRORS as e:
        print(f"STARTUP FAILED: {e}", file=sys.stderr)
        return 2

    if args.check:
        print("preflight OK: the system can start")
        print(f"  assets:     {', '.join(runner.assets)}")
        print(f"  strategies: {', '.join(s.id for s in runner.strategies)}")
        print(f"  degraded regimes: "
              f"{[a for a, c in runner.classifiers.items() if c.model.degraded] or 'none'}")
        store.close()
        return 0

    audit = AuditTrail(store=store)

    def evaluate_once() -> Any:
        result = runner.evaluate(int(time.time() * 1000))
        if result.error is None:
            audit.record_evaluation(result)
        return result

    if args.serve:
        return _serve(runner, settings, evaluate_once, interval_s=args.interval)

    while True:
        result = evaluate_once()
        published = len(result.published)
        print(f"{result.as_of_ms}: {published} published, {result.elapsed_ms} ms, "
              f"health={runner.health().status.value}")
        if args.once:
            break
        time.sleep(args.interval)
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
