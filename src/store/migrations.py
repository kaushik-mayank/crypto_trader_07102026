"""Schema migrations.

Forward-only, numbered, applied in one transaction each, recorded in ``schema_version``. A gap or an
unknown applied version is a :class:`MigrationError` — never a silent "best effort" schema, because
a half-migrated database that accepts writes corrupts the source of truth.

**Decimal storage:** SQLite has no decimal type, and REAL would silently round exchange prices. Every
price and quantity is stored as **TEXT** and read back through ``Decimal``. That is the only
representation that survives a round trip exactly (invariant 5).
"""
from __future__ import annotations

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "core market data, dq events, runs",
        """
        CREATE TABLE bars (
            asset          TEXT    NOT NULL,
            product        TEXT    NOT NULL,
            timeframe      TEXT    NOT NULL,
            open_time_ms   INTEGER NOT NULL,
            open           TEXT    NOT NULL,
            high           TEXT    NOT NULL,
            low            TEXT    NOT NULL,
            close          TEXT    NOT NULL,
            volume_base    TEXT    NOT NULL,
            is_final       INTEGER NOT NULL DEFAULT 0,
            synthesised    INTEGER NOT NULL DEFAULT 0,
            source         TEXT    NOT NULL DEFAULT '',
            content_hash   TEXT    NOT NULL,
            first_fetched_ms INTEGER NOT NULL,
            last_fetched_ms  INTEGER NOT NULL,
            fetch_count    INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (asset, product, timeframe, open_time_ms)
        ) WITHOUT ROWID;

        CREATE INDEX idx_bars_lookup ON bars (asset, product, timeframe, is_final, open_time_ms);

        CREATE TABLE known_absent_bars (
            asset          TEXT    NOT NULL,
            product        TEXT    NOT NULL,
            timeframe      TEXT    NOT NULL,
            open_time_ms   INTEGER NOT NULL,
            confirmations  INTEGER NOT NULL DEFAULT 1,
            first_confirmed_ms INTEGER NOT NULL,
            last_confirmed_ms  INTEGER NOT NULL,
            PRIMARY KEY (asset, product, timeframe, open_time_ms)
        ) WITHOUT ROWID;

        CREATE TABLE futures_rt (
            asset TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            funding_rate TEXT NOT NULL, predicted_funding_rate TEXT NOT NULL,
            mark_price TEXT NOT NULL, last_price TEXT NOT NULL,
            high_24h TEXT, low_24h TEXT, volume_quote_24h TEXT, price_change_pct TEXT,
            skw INTEGER, tpe_tick_send_ms INTEGER, tpe_mark_send_ms INTEGER,
            received_at_ms INTEGER NOT NULL,
            source_ts_ms INTEGER NOT NULL,           -- the row's own time: freshness comes from here
            PRIMARY KEY (asset, source_ts_ms)
        ) WITHOUT ROWID;

        CREATE TABLE spot_ticker (
            asset TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            last_price TEXT NOT NULL, bid TEXT, ask TEXT,
            high_24h TEXT, low_24h TEXT, volume_quote_24h TEXT, change_pct_24h TEXT,
            received_at_ms INTEGER NOT NULL,
            PRIMARY KEY (asset, ts_ms)
        ) WITHOUT ROWID;

        CREATE TABLE positioning (
            asset TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            long_count_pct TEXT NOT NULL, short_count_pct TEXT NOT NULL,
            long_value_pct TEXT NOT NULL, short_value_pct TEXT NOT NULL,
            PRIMARY KEY (asset, ts_ms)
        ) WITHOUT ROWID;

        CREATE TABLE book_snapshots (
            asset TEXT NOT NULL, product TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            bids TEXT NOT NULL, asks TEXT NOT NULL,      -- JSON [[price, qty], ...] as strings
            version INTEGER, received_at_ms INTEGER NOT NULL,
            best_bid TEXT NOT NULL, best_ask TEXT NOT NULL, levels INTEGER NOT NULL,
            PRIMARY KEY (asset, product, ts_ms)
        ) WITHOUT ROWID;

        CREATE TABLE trades (
            asset TEXT NOT NULL, product TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            price TEXT NOT NULL, qty TEXT NOT NULL, is_buyer_maker INTEGER NOT NULL,
            PRIMARY KEY (asset, product, ts_ms, price, qty, is_buyer_maker)
        ) WITHOUT ROWID;

        CREATE TABLE instrument_meta (
            asset TEXT NOT NULL, product TEXT NOT NULL, fetched_at_ms INTEGER NOT NULL,
            tick TEXT NOT NULL, qty_increment TEXT NOT NULL, min_notional TEXT NOT NULL,
            maker_fee_fraction TEXT NOT NULL, taker_fee_fraction TEXT NOT NULL,
            multiplier_up TEXT, multiplier_down TEXT, funding_frequency_h INTEGER,
            max_leverage TEXT, margin_tiers TEXT NOT NULL, status TEXT NOT NULL,
            PRIMARY KEY (asset, product, fetched_at_ms)
        ) WITHOUT ROWID;

        CREATE TABLE dq_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at_ms INTEGER NOT NULL, kind TEXT NOT NULL,
            component TEXT NOT NULL, operation TEXT NOT NULL,
            problem_class TEXT, severity TEXT NOT NULL DEFAULT 'warning',
            asset TEXT, detail TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_dq_events_time ON dq_events (at_ms, kind);

        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            process TEXT NOT NULL, started_ms INTEGER NOT NULL, stopped_ms INTEGER,
            code_version TEXT NOT NULL, parameter_hash TEXT NOT NULL,
            settings_hash TEXT NOT NULL DEFAULT '', stop_reason TEXT, notes TEXT NOT NULL DEFAULT ''
        );
        """,
    ),
    (
        2,
        "signals, lifecycle, audit, rejections, arbitration (specs/10 §7)",
        """
        CREATE TABLE signals (
            signal_id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL, strategy_class INTEGER NOT NULL,
            asset TEXT NOT NULL, product TEXT NOT NULL, direction TEXT NOT NULL,
            state TEXT NOT NULL, state_since_ms INTEGER NOT NULL,
            tier TEXT NOT NULL, validated INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'suggestion',
            armed_at_ms INTEGER NOT NULL, expires_at_ms INTEGER,
            entry TEXT, stop TEXT, targets TEXT, trail_level TEXT,
            risk_reward TEXT, remaining_rr TEXT, k_net TEXT, breakeven_hit_rate TEXT,
            strength TEXT, confidence TEXT, confidence_unavailable_reason TEXT,
            regime_cell TEXT, regime_dwell INTEGER,
            trigger_price TEXT, trigger_time_ms INTEGER,
            original_entry_price TEXT, original_entry_ms INTEGER,
            handoff_from TEXT, handoff_to TEXT, suppressed_by TEXT,
            parameter_hash TEXT NOT NULL, code_version TEXT NOT NULL,
            snapshot_id TEXT NOT NULL, as_of_ms INTEGER NOT NULL,
            payload TEXT NOT NULL,                       -- the full published Signal object as JSON
            created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
        );
        CREATE INDEX idx_signals_state ON signals (state, asset, strategy_id);
        CREATE INDEX idx_signals_created ON signals (created_at_ms, signal_id);

        CREATE TABLE signal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT NOT NULL, at_ms INTEGER NOT NULL,
            from_state TEXT, to_state TEXT NOT NULL, reason TEXT, detail TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (signal_id) REFERENCES signals (signal_id)
        );
        CREATE INDEX idx_signal_events_time ON signal_events (at_ms, id);

        CREATE TABLE signal_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT NOT NULL, as_of_ms INTEGER NOT NULL, snapshot_id TEXT NOT NULL,
            code_version TEXT NOT NULL, parameter_hash TEXT NOT NULL,
            record TEXT NOT NULL,                        -- the complete decision trace, JSON
            bar_mutated INTEGER NOT NULL DEFAULT 0,
            created_at_ms INTEGER NOT NULL
        );
        CREATE INDEX idx_signal_audit_signal ON signal_audit (signal_id, as_of_ms);

        CREATE TABLE rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at_ms INTEGER NOT NULL, as_of_ms INTEGER NOT NULL,
            strategy_id TEXT NOT NULL, asset TEXT NOT NULL, product TEXT NOT NULL,
            binding_condition TEXT,                      -- the first failing condition, for the UI
            passed_count INTEGER NOT NULL, total_count INTEGER NOT NULL,
            conditions TEXT NOT NULL,                    -- ALL results, JSON (rolled up after 30 days)
            rolled_up INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_rejections_lookup ON rejections (asset, strategy_id, at_ms);

        CREATE TABLE rejection_rollup (
            day TEXT NOT NULL, strategy_id TEXT NOT NULL, asset TEXT NOT NULL,
            condition_name TEXT NOT NULL, failures INTEGER NOT NULL, evaluations INTEGER NOT NULL,
            PRIMARY KEY (day, strategy_id, asset, condition_name)
        ) WITHOUT ROWID;

        CREATE TABLE arbitration_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at_ms INTEGER NOT NULL, kind TEXT NOT NULL,   -- CONFLICT | MERGE | DEDUP | HANDOFF
            asset TEXT, primary_signal_id TEXT, other_signal_id TEXT,
            detail TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_arbitration_time ON arbitration_events (at_ms, kind);

        CREATE TABLE setup_state (
            signal_id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL, asset TEXT NOT NULL, product TEXT NOT NULL,
            direction TEXT NOT NULL, armed_at_ms INTEGER NOT NULL, armed_snapshot_id TEXT NOT NULL,
            x_star TEXT, e_t TEXT, lifecycle TEXT NOT NULL, expires_at_ms INTEGER,
            original_entry_price TEXT, parameter_hash TEXT NOT NULL, strategy_version TEXT NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        """,
    ),
    (
        3,
        "setup_state carries the context and trigger time a restart needs to resume (F-062)",
        """
        -- Two columns the resume path cannot work without, and whose absence made the existing
        -- `setup_state` table insufficient for the purpose it was created for.
        --
        -- `context` is where a strategy keeps the facts only it knows: C3-C's `inside_is_below`
        -- (which side price approached the boundary from), C2's monotone `trail_level` and
        -- `extreme_since_entry`, C3-C's `breaks_seen`. `SetupState.context`'s own docstring calls
        -- them "needed to re-evaluate invalidation", and they were being dropped on write. Resuming
        -- without them would silently reset a trailing stop to its starting width and invert C3-C's
        -- acceptance test — worse than not resuming at all, because it looks like it worked.
        --
        -- `triggered_at_ms` is when the hypothetical entry occurred, distinct from `armed_at_ms`
        -- (when the setup was detected) and from `updated_at_ms` (when this row was last written).
        --
        -- Additive, nullable, no backfill: a row written by the previous build simply has NULL and
        -- the resume path declines to resume it rather than guessing at a context it never saved.
        ALTER TABLE setup_state ADD COLUMN context TEXT;
        ALTER TABLE setup_state ADD COLUMN triggered_at_ms INTEGER;

        -- Resume reads by lifecycle. Without this it is a scan of every setup ever armed.
        CREATE INDEX idx_setup_state_lifecycle ON setup_state (lifecycle, armed_at_ms);

        -- Phase 2A filters suggestions by model, and Phase 2C browses them by date. The signals
        -- table already has `idx_signals_created (created_at_ms, signal_id)` for the date axis;
        -- this covers the model axis and the two combined, so a filtered date page stays an index
        -- range scan instead of a table scan plus a filter.
        CREATE INDEX idx_signals_class_created ON signals (strategy_class, created_at_ms);
        """,
    ),
    (
        4,
        "repair strategy_class on signals written with the missing-class default (F-065)",
        """
        -- A **repair**, not a reinterpretation, and the distinction matters.
        --
        -- `build_signal` never published `strategy_class`, and `_write_signal` read it from the
        -- payload with a default of 0. The C4 strategies pass it in their own detail dict, so C4
        -- signals were stored correctly and every C1, C2 and C3 signal was stored as **0**. The
        -- dashboard filters on that column, so three of the four model filters matched nothing and
        -- their counts read zero while the total did not.
        --
        -- The correct value is not a judgement: it is derivable with certainty from the
        -- `strategy_id` already in the same row, using the same `C<n>-` mapping the registry has
        -- always had. So this restores a value that was always determined and was written wrong,
        -- which is why it is safe to apply to historical rows. It introduces no new rule and
        -- changes no outcome, status, price or timestamp.
        --
        -- Scoped twice over: only rows whose class is not already one of the four, and only where
        -- the id matches the pattern. A row that matches neither is left exactly as it is rather
        -- than guessed at, and will still report as unclassified where the dashboard now shows it.
        UPDATE signals
           SET strategy_class = CAST(SUBSTR(strategy_id, 2, 1) AS INTEGER)
         WHERE strategy_class NOT IN (1, 2, 3, 4)
           AND strategy_id GLOB 'C[1-4]-*';
        """,
    ),
]


def latest_version() -> int:
    return max(v for v, _, _ in MIGRATIONS)
