# Findings

Append-only record of verified facts. Each entry: UTC date, exact request, raw evidence,
resolved answer. **⚠ CORRECTION** marks an entry that contradicts `specs/00-capability-map.md`
(the spec has been updated to match, per CLAUDE.md working rules).

These entries come from **recorder development**, not from the formal probes in
`specs/01-verification.md`. Where one overlaps a probe, it is marked *preliminary*: the
probe still has to run in full.

---

## F-001 ⚠ CORRECTION — spot order book: `Depth` (capital D) is rejected; `depth` works

**Date:** 2026-09-18 ~12:38 UTC
**Requests** (identical results on `api.coindcx.com` and `public.coindcx.com`):

| Query | Status | Levels (bids/asks) |
|---|---|---|
| `/market_data/orderbook?pair=B-BTC_USDT` | 200 | 50 / 50 |
| `...&Depth=50` | **422** | — |
| `...&Depth=20` | **422** | — |
| `...&Depth=200` | **422** | — |
| `...&depth=50` | 200 | 50 / 50 |
| `...&depth=20` | 200 | 20 / 20 |

422 body: `{"code":422,"errorCode":"BFF-SO-004","message":"Depth is not allowed","status":"error"}`
(fixture `tests/fixtures/spot_book_Depth_422_2026-09-18T1237Z.json`).

**Answer:** the depth parameter is lowercase `depth`. With no parameter, the response has
50 levels. Capability map §3 and hazard §5 #8 claimed the opposite, and the old
hazard would have produced a 422 on every spot-book poll. Whether `depth=100/200`
works is still open and belongs to **P14**.

**Nearby things that may also be wrong:** the documented depth set {1,5,10,20,50,100,200}
is unverified above 50.

## F-002 — spot `/market_data/*` is served on both hosts

**Date:** 2026-09-18 ~12:38 UTC. `GET {api,public}.coindcx.com/market_data/candles?pair=B-BTC_USDT&interval=1h&limit=3`
returned the same bars on both hosts. The only difference was the volume of the still-open
bar (351.67586 vs 351.80731, fetched ~1s apart). The order book behaves the same on both.
The recorder uses `api.coindcx.com`, as in the capability map.

## F-003 — number formatting: re-serializing parsed JSON is not verbatim

**Date:** 2026-09-18. `current_prices/futures/rt` sends `"fr":0.00007903`. Python's
`json.loads` then `json.dumps` produces `7.903e-05`. The value is the same, but the bytes differ.
**Consequence:** the recorder does not re-serialize payloads. It slices the exact source
text out of the response (`cdx_http/jsonutil.py`). This is covered by
`test_float_roundtrip_would_not_be_verbatim` and `test_futures_rt_stores_three_verbatim_rows`.

## F-004 ⚠ CORRECTION — `skw` is NOT a per-row health flag

**Date:** 2026-09-18, 12:30–12:56 UTC.
**Request:** `GET https://public.coindcx.com/market_data/v3/current_prices/futures/rt`,
sampled repeatedly (recorder smoke runs plus 6 samples 8 s apart).

**Evidence:**

| Time (UTC) | fresh rows (`btST` < 60 s old) | `skw` across all fresh rows |
|---|---|---|
| 12:37:25 (fixture) | — | BTC/ETH/SOL all `0` |
| 12:50–12:54 (recorder) | — | BTC/ETH/SOL moved together `1 → 3 → 10 → 33` |
| 12:55:03 | 496 | **all 496 = 40** |
| 12:55:20 – 12:55:54 (5 samples) | 488–497 | all = 40 |
| 12:56:03 | 495 | **all 495 = 24** |

Recorded live row, B-BTC_USDT, `btST` 0.7 s old: `"skw":10`.
At 12:55:03 the stale rows (btST ≥ 60 s) showed mixed values: 40, 4, 10, 1, 0, 36, 46, 2, −4, −20, 21, 64.

**Answer:** `skw` is a single value that the server stamps on every row it refreshes in a
snapshot. It drifts over time (seen 0…40, and negatives on frozen rows). Stale rows
keep whatever value was current when they froze. That is why the original verification
saw non-zero values only on frozen rows: at that moment the live value happened to be 0.
The name suggests a clock skew or latency figure, but the exact meaning is **unknown**.

**Impact:**
- Capability map §4 said "`skw` 0 = live, non-zero = frozen row". **Wrong.** Updated.
- **CLAUDE.md hard invariant #3 ("`skw != 0` means reject the row") is wrong.** Applied
  as written, it would reject every live BTC/ETH/SOL row whenever the shared value is
  non-zero, which was most of the time during these samples. Freshness has to be judged
  by `btST`/`bmST` age (invariant #2) alone. **CLAUDE.md is not yet edited. That decision is
  the project owner's.** See Open questions.
- The recorder is unaffected: it stores rows raw and never rejects on `skw`. Its P13
  quality rollup records `skw_min`/`skw_max` per hour, which will show how `skw` behaves
  over the long run.

## F-005 — response shapes and units observed while building the recorder

**Date:** 2026-09-18 ~12:37 UTC. Fixtures are in `tests/fixtures/` (see its README).

- `current_prices/futures/rt`: `{"ts": <ms>, "vs": <int>, "prices": {<pair>: {...}}}`, **539 rows**
  (the map said ~450). The stale, schema-incomplete `B-NKN_USDT` row is still present:
  `{fr, mp, efr, bmST, cmRT, skw}`, `bmST` = 2026-02-16.
- `/exchange/ticker`: array of **997** rows. Target rows use `market` = `BTCUSDT`/`ETHUSDT`/`SOLUSDT`.
  All values are strings. `timestamp` is **epoch seconds** (e.g. `1789735075`).
- `/exchange/v1/markets_details`: BTC/ETH/SOL vs USDT each appear once, as
  `pair=B-{X}_USDT`, `ecode=B`, `status=active`. *Preliminary for P3*: the full ecode
  survey has not been done.
- Spot order book: `{"timestamp": <ms>, "bids": {...}, "asks": {...}}`.
- Futures order book `/market_data/v3/orderbook/B-BTC_USDT-futures/50`: `{"ts": <ms>, "vs": <int>, "asks": {...}, "bids": {...}}`.
  Price keys have inconsistent precision (`"78058"`, `"78057.5"`). Key order is not sorted.
- Futures candlesticks (`resolution=60`, 3h window): `{"s":"ok","data":[...]}`, **ascending**,
  `time` in **ms** even though `from`/`to` are in seconds. *Preliminary for P4* (one sample only).
  BTC 1h volume ≈ 2,600–5,900, so probably **base (BTC)**. *Preliminary for P6*: not yet cross-checked.
- Spot candles `limit=3`: descending, ms bar-open times. Consistent with the map.
- Instrument (`B-BTC_USDT`): `taker_fee 0.059`, `maker_fee 0.0236`, `min_notional 60.0`,
  `price_increment 0.1`, `quantity_increment 0.001`. Consistent with the map. Note that
  `max_leverage_long`/`_short` = **20.0** at the top level, while the map lists a 100x tier. Not investigated.

## F-006 — recorder smoke test (not the 72 h acceptance run)

**Date:** 2026-09-18 12:46–12:54 UTC, scratch data directory, live API.
- Run 1, 150 s: 35 tasks, 13 streams, every stream recorded, **0 errors, 0 non-2xx**.
  Latency 60–100 ms per request.
- Run 2 was killed with `taskkill /F` (the Windows equivalent of kill -9) after 75 s and restarted 90 s later:
  prior data intact (0 unparseable lines), `prior_shutdown_clean:false` recorded, and every
  task resumed from its last record. The outage (~108 s for S1) was under the 2×interval
  threshold, so no gap record was expected. Gap records are covered by unit tests
  (`test_restart_records_startup_gap_and_prior_unclean`, `test_sleep_gap_reason_unknown_and_error_streak`).
- Run 4 was stopped with a simulated Ctrl-C: exit 0, `shutdown` marker written, partial-hour quality rollup flushed.
- Futures-book skew (source `ts` − local request midpoint): mean −73 ms, range −134…−32 ms
  (a few samples only; P15 needs ~200).

**Not yet demonstrated:** the ≥72 h run, a 10-minute network block, laptop sleep/wake,
a live gap record, and measured monthly disk growth (acceptance criteria 1, 3, 4, 7).
Those are now covered in F-020 onwards.

---

# Phase 1 probes (formal runs, 2026-09-19)

Every probe writes its full result, including the exact requests, to
`data/probe_results/<probe>_<UTC stamp>.json`. Probes that capture raw responses keep them under
`data/raw/probe/<probe>/<stamp>/`. The code is in `src/probes/`. The numbers below are quoted
from those files.

## F-007 — P3: spot markets for BTC / ETH / SOL ✅

**Run:** 2026-09-19 10:29 UTC, `GET https://api.coindcx.com/exchange/v1/markets_details`
(995 markets, ecodes `B`, `G`, `I`, `KC`). Result: `p03_20260919T102907Z.json`.

| pair | ecode | status | min_notional | step | base prec. | target prec. | min_quantity |
|---|---|---|---|---|---|---|---|
| `B-BTC_USDT` | B | active | 5 | 0.00001 | 2 | 5 | 0.00001 |
| `B-ETH_USDT` | B | active | 5 | 0.0001 | 2 | 4 | 0.0001 |
| `B-SOL_USDT` | B | active | 5 | 0.001 | 2 | 3 | 0.001 |

Other ecode variants of these assets: `I-BTC_INR`, `I-ETH_INR`, `I-SOL_INR` (all ecode I,
INR-quoted), plus `B-ETH_BTC`. **There are no non-B USDT markets.** The ticker `market`
names are `BTCUSDT`, `ETHUSDT`, `SOLUSDT`, so the recorder's provisional config is now confirmed.
**P9 is only possible via INR:** `I-X_INR ÷ I-USDT_INR` compared against `B-X_USDT` (see F-015).

## F-008 ⚠ CORRECTION — P4 / P4b: futures candles are ascending, and serve many more resolutions than documented

**Run:** 2026-09-19 10:29 UTC. Results: `p04_p05_20260919T102908Z.json`, `p04b_20260919T102949Z.json`.

- **P4:** a 24h `resolution=60` window returned 25 bars, **ascending**, times in **ms**, `s:"ok"`.
  - A future window and a pre-listing (2010) window both return `200` with `{"s":"no_data","data":[]}`.
  - `from > to` returns **400** `{"code":400,"message":"Invalid Request.","status":"error"}`.
  - `from == to` returns the single bar that contains that instant, which is the still-open bar.
- **P4b resolution survey** (B-BTC_USDT, 20 bars each):
  - **Futures serve `1, 5, 15, 30, 60, 240, 480, 1D, 3D, 1M`**, all with exact spacing and UTC alignment.
  - `3, 120, 360, 720, 1W` return **400**.
  - The capability map (§3, §9) listed only `1, 5, 60, 1D`. **Futures 4h (`240`) is native**, so it needs no aggregation.
  - **Spot serves only `1m, 15m, 1h, 1d`.** Everything else returns 422 `"interval must be one of [1m, 15m, 1h, 1d]"`. That matches the map.
  - Spot 4h still has to be aggregated from 1h. P12 shows that aggregation is exact.

## F-009 ⚠ NEW HAZARD — P5: futures candlesticks silently truncate at 30,000 bars and keep the OLDEST

**Run:** 2026-09-19 10:29 UTC, `resolution=1`, B-BTC_USDT, windows ending 10:00Z.

| window | expected | returned | kept |
|---|---|---|---|
| 1h | 61 | 61 | all |
| 1d | 1,441 | 1,441 | all |
| 3d | 4,321 | 4,321 | all |
| 1w | 10,081 | 10,081 | all |
| 1 month | 43,201 | **30,000** | **oldest** (2026-08-20T10:00 → 2026-09-10T05:59) |

The truncation is **silent**: HTTP 200, `s:"ok"`, and nothing to indicate it. Because the oldest end is kept,
a naive "request everything since X" returns what looks like complete history that simply stops
9 days early. **Chunk futures requests well under 30,000 bars.** The recorder uses 500 and the
backfill 10,000.

## F-010 — P6: futures candle volume is BASE-denominated ✅

**Run:** 2026-09-19 10:29 UTC. The bar tested is 08:00Z 1h; `p06_20260919T102916Z.json`.
Σ(volume × close) over 24 futures 1h bars ÷ rt `v` (quote, 24h) = **0.966 (BTC), 0.952 (ETH), 0.976 (SOL)**.
The ratios are ≈1, so volume is base. They fall slightly short of 1 because the rt 24h window is rolling
and the close is only a proxy for the trade price. Example: the BTC bar has volume 2,972.412, which at
≈81,238 is ≈241.5M USDT, while spot BTC for the same hour is 342.3 BTC (base, verified).
**All three candle volume fields are therefore base; only rt `v` is quote.** Hazard §5 #4 is resolved.

## F-011 — P7: futures `data/trades` is a fixed 30-row snapshot; `limit` is ignored ✅

**Run:** 2026-09-19 10:29 UTC. Every variant (`limit` absent, `limit=5`, `limit=1000`) returned the
**same 30 rows**, spanning **7.06 s**, **oldest-first**, with keys `is_maker, price, quantity, timestamp`
and `is_maker` a boolean (15/15 split). **The tape cannot be used to reconstruct per-bar aggressor
volume at any bar size;** 30 BTC trades cover about 7 seconds. That would need continuous polling
several times a second, which is out of scope at this horizon.

## F-012 — P11: all 1h and 1d bars are aligned to UTC, on both products ✅

**Run:** 2026-09-19 10:29 UTC. 400 bars × {spot 1h, spot 1d, futures 60, futures 1D} × 3 pairs =
4,800 bars. **Every open time modulo 3,600,000 ms / 86,400,000 ms is 0.** There is no IST offset.
P4b also confirms UTC alignment for futures 15/30/240/480.

## F-013 — P12: aggregation reproduces native bars exactly ✅

**Run:** 2026-09-19 10:39 UTC over 2026-09-09T04:00Z → 2026-09-19T04:00Z. That covers 10 days,
including 10 overnight low-volume periods. `p12_20260919T103905Z.json`.

| test | windows per pair | exact match (BTC / ETH / SOL) |
|---|---|---|
| spot 15 × 1m → native 15m | 960 | 100% / 100% / 100% |
| futures 60 × 1m → native 60 | 240 | 99.58% (one bar per pair differs in **volume only**; max relative diff 1.8×10⁻⁵; OHLC 100%) |
| futures 3 × 5m → native 15 | 960 | 100% / 100% / 100% |
| futures 4 × 60 → native 240 | 60 | 100% / 100% / 100% |
| spot 4 × 1h vs futures 240 (shape only) | 59 | return correlation 0.9999; mean \|close diff\| 4.5–4.7 bps |

- **Continuity:** 14,400 of 14,400 expected 1m bars present on both products for all three
  pairs, with zero zero-volume bars. The failure mode the spec feared (empty minutes omitted)
  did not occur in this window. The backfill gap counts (F-018) test this over the full history.
- **Spec error:** 01-verification §P12 said "aggregate 12 × 5m → compare against spot native 15m".
  12 × 5m is one hour. The test used 3 × 5m, and compared it with native *futures* 15m (F-008) for exact equality.
- **Decision fed:** aggregated 4h (spot) and native 4h (futures) are both trustworthy, so the
  timeframe stack does not need to shrink.

## F-014 ⚠ NEW HAZARD — P14 (live part): order-book depth parameters

**Run:** 2026-09-19 10:35 UTC, `p14_depth_20260919T103558Z.json`.

- **Spot** `depth` (lowercase) accepts exactly **{1, 5, 10, 20, 50, 100, 200}**, and each value returns
  that many levels per side. `depth=500` returns 422 `"depth must be one of [1, 5, 10, 20, 50, 100, 200]"`.
  This refines F-001: the documented set was right, but the parameter name was wrong.
- **Futures** `/orderbook/{pair}-futures/{depth}`: 10, 20 and 50 return that many levels. **5 and 100 return
  HTTP 200 with an EMPTY book** (`bids:{}`, `asks:{}`). The failure is silent. Any consumer must treat
  an empty futures book as invalid, never as "no liquidity".
- The spread and depth distributions for P14 come from the recorder's 5-minute snapshots; see F-021.

## F-016 — P17: spot trade tape aggressor flag ✅ (populated), semantics consistent with upstream

**Run:** 2026-09-19 10:29 UTC. 500 trades (the maximum), spanning **24.6 s**, **newest-first**. Keys are
`T, m, p, q, s`, where `s` is the upstream symbol `BTCUSDT`. `m` is always a JSON boolean:
**191 true / 309 false**. It is populated and not constant.

The cross-check against the book is only indicative, because the book was fetched after the tape and
the price moved during those 24 s. Trades above the mid were 86 false / 57 true; trades below were
134 true / 223 false. The skew points the expected way: `m=true` means the buyer was the maker, so the
aggressor sold. It is not clean enough to call verified. At 500 trades per ~25 s, per-bar aggressor
volume would need continuous polling. **Deprioritized, as the spec allows.**

## F-017 — P1 (binary search): history depth, preliminary

**Run:** 2026-09-19 10:30 UTC, `p01_20260919T103037Z.json`.

The earliest bar was found by binary search on "a 499-bar window contains data". An out-of-range
request (2010) returns `[]` on spot and `s:"no_data"` on futures, never recent data.

| pair | spot 1d | spot 1h | spot 15m | spot 1m | fut 1D | fut 60 | fut 1 | fut 5/15/30/240/480 |
|---|---|---|---|---|---|---|---|---|
| BTC | 2017-08-17 | 2018-08-23 | 2018-08-23 | 2017-12-12 | 2021-01-01 | 2022-01-19 | 2021-01-01 | 2023-01-11 |
| ETH | 2017-08-17 | 2018-09-21 | 2018-09-21 | 2018-09-21 | 2022-07-01 | 2022-01-19 | 2022-08-11 | 2023-01-23 |
| SOL | 2020-08-11 | 2021-02-11 | 2021-02-27 | 2021-03-04 | 2022-07-01 | 2022-06-30 | 2022-06-30 | 2023-01-23 |

**These results are internally inconsistent, so they are treated as preliminary.** For example, spot BTC
1m supposedly starts before BTC 15m/1h, and ETH futures 60 before ETH futures 1D. The binary search
assumes no internal hole longer than one 499-bar window, and these patterns suggest such holes exist.
**The backfill resolves this.** It starts every series of a product/pair at the earliest bar found at
*any* resolution, scans forward in full, and reports the true first bar and every hole (F-018).
Even on these preliminary numbers, the answer to the spec's question is clear: **1h history is at least
3.6 years on futures and 5.6–8 years on spot.** That is ample for walk-forward validation at the
hourly/daily horizon.

## F-019 ⚠ CORRECTION — P8: positioning (`data/stats`) is PUBLIC; the documented POST is 404 ✅ (part A)

**Run:** 2026-09-19 10:56–11:00 UTC. `p08_20260919T*.json`; the fake-key run is labelled `_DUMMY_CREDENTIALS`.

| request | result |
|---|---|
| GET `…/api/v1/derivatives/futures/data/stats?pair=B-BTC_USDT`, **no auth headers** | **200**, full payload |
| same, with a *fake* key + signature (ms or s, pair in query or body) | 200, identical payload; the credentials are not checked |
| POST (any variant, with or without the fake signature) | **404** `{"status":"error","message":"not_found","code":404}` |
| GET without `pair`, or `pair=B-NOPE_USDT` | **200 with empty objects** `{"price_change_percent":{},"high_and_low":{},"position":{}}` ⚠ silent |

Payload example (BTC, 10:57 UTC):
`"position":{"count_percent":{"long":54.92,"short":45.08},"value_percent":{"long":31.65,"short":68.35}}`.
Both percentages are populated for all three pairs, and each long/short pair sums to 100.
There are also `price_change_percent` {1H,1D,1W,1M} and `high_and_low` {1D,1W}.

**Answer:** crowd positioning **is available**, and needs no credentials. Class 1 keeps its most
distinctive input. **Positioning history started accumulating at 2026-09-19 11:00:45 UTC** (S2
enabled, `auth = "none"`, 15-minute polls). The recorder flags any 200 with an empty `position`
as an `empty_position` error, but still records the response.

**Contradicts:**
- The capability map's §3 and §8, which said `data/stats` is signed and documented as POST. Both are updated.
- **CLAUDE.md invariant 12** ("Only two endpoints need authentication: `data/stats` and `conversions`").
  This is **not edited; it is the owner's decision** (see Q5).

**Part B, still pending:** whether a *valid* signed request returns anything more or different. The probe
runs this comparison automatically once `.env` has values: `python -m probes.p08_positioning`.

## F-020 — Recorder acceptance 3: 10-minute network outage ✅ PASS

**Run:** 2026-09-19 10:39–10:58 UTC, `reliability_outage_20260919T105804Z.json`. A separate recorder
instance was run through a local proxy controlled by the test. The production recorder was untouched.

Timeline: up 3 min → **blackhole 5 min** (connections accepted, never answered, so every request times out)
→ **refuse 5 min** (connections dropped) → up 6 min.

- The process stayed alive throughout. There were 74 transport errors, all logged to `_meta/errors.jsonl`.
- **No hot loop:** at most **10 attempts per task in 600 s**. S1 would normally make 10 polls in that
  time, and the books only 2; the backoff capped retries at the 60 s transport ceiling.
- **Automatic recovery for every stream:** the first record after the outage came 0.8 s (futures book)
  to 37.5 s (spot ticker) after connectivity returned.
- **Gap records:** 8 were written, all with `reason:"error_streak"` and `recoverable:false` (S1, S3, and the
  three S5 and three S6 tasks), with durations of 686–790 s.
- **Candle gap-fill was pulled forward** by the unrecoverable gap and ran 1.3–8.0 s after recovery.
  All 8 candle streams recorded again without waiting for the hourly schedule.

A shortened 60 s dry run (`…_DRYRUN_scale0.1.json`) validated the harness first. It was too short to
produce gap records, which is expected.

## F-021 — Operational fixes found during testing

1. **A detached recorder could not be stopped cleanly.** On Windows it has no console, so it cannot
   receive Ctrl-C. The fix: create `data/raw/_meta/STOP` and the recorder shuts down cleanly within
   about a second. A stale STOP file found at startup is removed.
2. **A probe launched through `cmd.exe` died after one sample** from a console Ctrl-C (the first P9/P10
   sampler, 10:32 UTC). Long-running jobs are now launched without the wrapper, and have not been interrupted since.
3. **The production recorder was restarted at 11:00:45 UTC** to run the code that ships: S2 enabled,
   plus the stop file. The old process was hard-killed, which doubled as another live crash test.
   On restart it recorded `prior_shutdown_clean:false` and resumed every task from its last record.
   **The 72 h acceptance window is 2026-09-19 11:00:45 → 2026-09-22 11:00:45 UTC.**

## F-022 — P2: rate limits. No 429 up to 10 req/s; `api.coindcx.com` advertises 5,000 per 60 s ✅

**Run:** 2026-09-19 11:03–11:15 UTC, `p02_*.json`. Each endpoint was ramped separately at 1 → 2 → 5 → 10 req/s,
15 s per step (270 requests per endpoint), with 30 s pauses between endpoints. The production recorder and
the P9/P10 sampler were running at the same time (~0.17 req/s combined); **neither saw a single error.**

| endpoint | result | latency p50 at 10 req/s |
|---|---|---|
| `/exchange/ticker` | no 429 up to 10 req/s | 78 ms |
| `/market_data/candles` | no 429 up to 10 req/s | 73 ms |
| `/market_data/orderbook` | no 429 up to 10 req/s | 81 ms |
| `/market_data/trade_history` | no 429 up to 10 req/s | 76 ms |
| `public…/current_prices/futures/rt` | no 429 up to 10 req/s | 65 ms |
| `public…/candlesticks` | no 429 up to 10 req/s | 47 ms |

**Undocumented rate-limit headers (IETF RateLimit draft) on every `api.coindcx.com` response:**
`ratelimit-policy: 5000;w=60` and `ratelimit: limit=5000, remaining=4868, reset=47`. That is a
budget of **5,000 requests per 60 s window** (~83 req/s), and the counter is shared across that
host's endpoints. `public.coindcx.com` sends no rate-limit headers and did not throttle.
There is no `Retry-After`, because no 429 occurred.

**Decision fed:** the recorder's ~230 requests/hour is ~0.08% of the advertised budget. The backfill runs at
5 req/s per host (6% of it). The test deliberately stopped at 10 req/s, so the real trigger point is unmeasured.

## F-018 — Backfill complete + P1 final: history depth, with holes located ✅

**Run:** 2026-09-19 11:13–12:31 UTC, 5 req/s per host, 14,900 requests, 0 failures, and **no interference**:
0 recorder errors, 0 gap records, and 0 sampler errors during the run. The 3D series were re-pulled at 12:33 after
the gap-accounting fix. The files are `data/backfill/{summary,verify}.json` plus 42 parquet files; the data dir is 1.3 GB.

`python -m backfill --verify` re-reads every parquet independently. **All 42 series are sorted and unique,
and every served resolution is present.** Spot pages of 1,000 bars were verified live (`limit=1000` works).

**P1 answer (true continuous start = after the last hole of 7 days or more):**

| series | first bar | continuous from | completeness after that | worst remaining hole |
|---|---|---|---|---|
| spot BTC 1m | 2017-08-17 | **2018-02-10 06:15** ⚠ (see below) | 99.69% | 2,010 min, 2018-02-08 |
| spot BTC 15m / 1h | 2017-08-17 | 2018-08-23 | 99.82% / 99.87% | 10 h, 2019-05-15 (exchange outage) |
| spot ETH 1m / 15m / 1h | 2017-08-17 | 2018-09-21 | 99.71% / 99.80% / 99.85% | 15.7 h, 2024-06-20 |
| spot SOL 1m / 15m / 1h | 2020-08-11 | 2021-02/03 | 99.76% / 99.86% / 99.89% | 15.8 h, 2024-06-20 |
| spot BTC/ETH 1d | 2017-08-17 | 2017-08-17 | **100%** | — |
| spot SOL 1d | 2020-08-11 | 2020-08-11 | **100%** | — |
| futures BTC 1 / 1D | 2021-01-01 | 2021-01-01 | 99.99% / 100% | 186 min, 2024-09-22 |
| futures BTC/ETH 60 | 2022-01-19 | 2022-01-19 | 99.99% | 4 h |
| futures SOL 60 | 2022-06-30 | 2022-06-30 | 99.99% | 2 h |
| futures ETH 1 | 2022-07-06 | 2022-08-11 | 99.70% | 19 h |
| futures SOL 1 | 2022-06-30 | 2022-10-07 | **98.61%** | 7,495 min (5.2 d), 2022-10-07 |
| futures 5/15/30/240/480 (all) | 2023-01-11/23 | same | 99.91–100% | 2023-01-23 short outage |
| futures ETH/SOL 1D | 2022-07-01 | same | 100% | — |

- **Why P1's binary search looked inconsistent:** each spot series starts with a stub of a few hours on its
  listing day, followed by a **months-long hole** (BTC 1m: 2017-08-17 20:39 → 2017-12-12; ETH 1m → 2018-09-21;
  SOL 1m → 2021-03-04; BTC 1h: 2017-09-28 → 2018-08-23). The binary search had actually found the start of
  continuous history.
- **⚠ NEW HAZARD: spot BTC 1m bars from 2017-12-12 to 2018-02-10 open at :20 or :14 seconds past the minute**
  (9,637 bars). Aggregating that range to coarser bars would be wrong. **Use spot BTC 1m only from
  2018-02-10T06:15Z**, or restrict it to hourly and daily data. Everything else is exactly aligned.
- **3D bars do not sit on a fixed grid:** there is one re-anchoring in August 2023 (steps of 1 and 2 days). They
  are reported as `irregular_short_steps: 3`, with no data missing. 1M bars are calendar months.
- Residual holes line up with real upstream outages (for example 2019-05-15 and 2024-06-20, which affect every
  series at once). They are not failures of the API or the pipeline.
- **Decision fed (backtest window):** hourly history is continuous from **2018-08/09 on spot** (≈8 years) and
  **2022-01 on futures** (≈4.7 years), at ≥99.8% completeness. Daily is complete from **2017-08** (spot) and
  **2021-01** (BTC futures). That is enough for walk-forward validation at the hourly/daily horizon.

## F-023 — P10 basis ✅ and P9 ecode divergence ✅ (6 h synchronous sampler)

**Run:** 2026-09-19 10:39–16:47 UTC, 578 paired samples (rt + ticker fetched concurrently),
1 fetch error. `p09_p10_20260919T103951Z.json`. Local receive times differed by a median of 15 ms.

**P10 — basis (mark vs spot last), valid samples only (both timestamps within 2 s):**

| pair | valid / attempts | discard | mean bps | σ bps | min | max |
|---|---|---|---|---|---|---|
| B-BTC_USDT | 561 / 578 | 2.9% | **−3.85** | 0.70 | −6.58 | +0.26 |
| B-ETH_USDT | 565 / 578 | 2.3% | **−4.14** | 0.71 | −9.12 | −0.83 |
| B-SOL_USDT | 552 / 578 | 4.5% | **−2.00** | 1.01 | −8.96 | +2.78 |

**Answer:** basis is **measurable, persistent and small**. The mean is ≈5σ, so it is not
measurement noise — the P10 branch in `specs/02` §1 ("if σ is dominated by noise, cut Family 6 to
funding only") does **not** trigger; basis features are retained. **But the magnitude is ~4 bps
against a verified 11.8 bps futures round trip**, so basis convergence is not tradable on its own
and must never be presented as an opportunity. It stays what `specs/03` §1 already made it: a
consistency check for C1-A, not a confirmation input.

The perp trades at a small **discount** to spot throughout, which is consistent with `fr` sitting
at its 0.0001 baseline for the entire window. **Funding never moved off baseline in 6 h**, so
`corr(basis, fr)` is unmeasurable (zero variance) and C1-A's mechanism was never observed live.
Recording more is the only fix; this is the Tier B problem in miniature.

**P9 — ecode divergence.** Per F-007 there is **no non-B USDT market**, so the only available
cross is CoinDCX-internal INR: `I-X_INR ÷ I-USDT_INR` versus `B-X_USDT`, all three rows taken from
the same ticker response (timestamp disagreement 0 s by construction).

| asset | median | mean | σ | min | max |
|---|---|---|---|---|---|
| BTC | −13.1 bps | −18.4 | 15.9 | −86.5 | +3.1 |
| ETH | −10.5 bps | −18.4 | 23.8 | −89.6 | +22.0 |
| SOL | −2.9 bps | −11.5 | 26.6 | −80.6 | +31.4 |

**Answer:** CoinDCX-internal INR markets **do** diverge materially from the relayed `B-` markets,
by more than the futures round-trip cost at times. This does **not** create a strategy here: it is
an INR-rail/USDT-INR pricing effect requiring instruments outside this system's set (`B-*` USDT
only), and the spread is dominated by the USDT/INR leg rather than by BTC/ETH/SOL pricing.
**Capability map §7 stands: `B-*` basis is the upstream venue's basis.** P9 is resolved as
"applicable but not exploitable within this instrument set".

## F-024 — P13 continuity baseline: the recorder is correct, the host is not ⚠

**Window:** 2026-09-19 11:00 UTC → 2026-09-23 17:28 UTC (103 h wall clock), single recorder
process, no crash. Analysis: `recorder_analysis_*.json`, `storage_check_*.json`.

- **Measured uptime ≈ 12.1 h of 103 h.** No records at all on 2026-09-22. The laptop slept or was
  off; the process was gone from 2026-09-23 17:28 until restarted on 2026-09-27 13:03 UTC.
- **250 gap records**: 184 `unknown` (sleep/wake) and 66 `error_streak`; longest 49.96 h.
  99 transport errors, no HTTP error at all (`429`/`503` never seen).
- **Snapshot streams lost that time permanently** — as designed, with every gap recorded.
- **Candle gap-fill worked exactly as intended:** `futures_candles_1` holds 6,168 bars per pair
  from 2026-09-19T10:25 to 2026-09-23T17:12 with **0 missing and 0 misaligned bars** across four
  days containing ~91 h of downtime. The same is true of every other candle stream.
- Duplicate `source_ts` records: 0. Out-of-order arrivals: 0.

**Verdict:** the recorder's resume, gap-recording and backfill design is **validated**; the
**72 h-unattended criterion is not met**, and cannot be on a laptop that sleeps. This is exactly
the argument `specs/18` §2 makes for a 24/7 host, now with a measured number attached.
**Consequence for Tier B:** `earliest_tier_b_validation` must be measured from *continuous*
recording, not from the first start date, or it will overstate the evidence base.

## F-025 — P14 spread and depth: books are one tick wide and very shallow in price terms

**Run:** 2026-09-19 12:35–21:57 UTC, 7 rounds across 4–5 distinct UTC hours (the spec asks for
~20; the sampler was interrupted by the same host downtime as F-024 — **P14 is partially met**).
`p14_books_20260919T123535Z.json`, plus the depth-parameter result in F-014.

| book | levels | median spread | median reach from mid | median depth inside reach |
|---|---|---|---|---|
| futures BTC (50) | 50 | **0.012 bps** | ±0.76 bps | 1.19M USDT |
| futures ETH (50) | 50 | 0.038 bps | ±1.88 bps | 1.39M USDT |
| futures SOL (50) | 50 | 0.895 bps | ±44.3 bps | 17.1M USDT |
| spot BTC (200) | 200 | **0.001 bps** | ±3.64 bps | 3.83M USDT |
| spot ETH (200) | 200 | 0.038 bps | ±9.68 bps | 3.02M USDT |
| spot SOL (200) | 200 | 0.895 bps | ±178.5 bps | 9.95M USDT |

Two structural facts:

1. **The spread is exactly one tick, always** (0.012 bps = 0.1/81,000 on BTC; 0.895 bps = 0.01/112
   on SOL). Every sample, every hour. So `LIQ_SPREAD` is near-constant per asset, and the
   `specs/08` §8 Tier-3 `SPREAD` veto (half-spread > 10% of `R`) will effectively never bind.
2. **"Depth within 10/25/50 bps" is not measurable for BTC or ETH at any available depth.** Even
   200 spot levels span only ±3.6 bps on BTC. The bands that *are* measurable are 1, 2 and 5 bps,
   which is what the analysis now reports; wider bands are returned as `None`, never as a
   truncated number. The `specs/08` §7.1 slippage model must therefore be built on the 1–5 bps
   bands and is only valid for sizes inside ~1M USDT on BTC futures. For retail size this is ample;
   it must not be extrapolated.

## F-026 — P15 server clock skew, and a stale spot-book timestamp

Same window as F-024; skew = source timestamp − local request midpoint, net of RTT (median RTT
69–88 ms).

| source | n | median skew | range of hourly means |
|---|---|---|---|
| `futures_rt` envelope `ts` | 692 | +1,000 ms | −544 … +1,654 ms |
| futures book `ts` | 440 | +1,669 ms | −68 … +3,491 ms |
| spot book `timestamp` | 434 | +1,403 ms | **−631,990** … +1,879 ms |
| spot ticker `timestamp` (1 s resolution) | 692 | +624 ms | −931 … +1,386 ms |

**Answer:** skew sits within roughly **±2 s**, so the 2-second basis tolerance
(`specs/02` Family 6) is the right order of magnitude and P10's 2.9% discard rate confirms it is
workable. `DQ_CLOCK`'s tolerance should be set no tighter than **±3 s**, and these are *payload
generation* times rather than pure clock offsets — the futures-book figure includes the exchange's
own snapshot latency.

**⚠ One anomaly worth its own gate:** a spot order-book response once carried a `timestamp`
**10.5 minutes stale** while returning HTTP 200. A single freshness check on the spot book is
therefore mandatory, not optional — this is the same class of failure as the stale rows in
`current_prices/futures/rt`, on a different endpoint.

## F-027 — P16 spot liquidity: thinner in volume, comparable in book depth

| asset | spot 24 h quote volume | spot / futures volume | spot / futures book depth (median) |
|---|---|---|---|
| BTC | 2.03B | **0.109** | 0.45 |
| ETH | 1.00B | **0.080** | 0.40 |
| SOL | 0.44B | **0.146** | 0.28 |

**Answer:** spot turnover is **8–15% of futures**, an order of magnitude thinner, while resting
book depth is 28–45% of futures. So spot is not unusable, but futures is unambiguously the primary
venue. **Decision fed:** keep spot as a tradable product only for small size, and treat it
primarily as the cross-reference leg for basis (`BAS_BPS`) and for C1-C's relative-value legs. Any
spot suggestion must carry the size caveat, per the P16 question in `specs/01`.

## F-028 — Storage growth and gzip compression, measured ✅

`storage_check_*.json`, over 2026-09-19 → 2026-09-27.

- **42 completed day files compressed, every sha256 verified against the pre-compression bytes;
  0 mismatches, 0 leftover `.tmp` files, 0 unlogged files.** The 14 day files left plain when the
  process died on 09-23 were compressed at the 09-27 restart, which exercised the startup path.
- **Measured compression ratio: 5.3×** (JSONL of repetitive market data).
- **Steady-state projection from measured record sizes at full cadence: ~379 MB/month raw →
  ~71 MB/month on disk.** Breakdown: `futures_rt` 65, `spot_ticker` 56, `spot_book` 57,
  `futures_book` 55, candles ~37 (inflated by catch-up refetches after downtime, so this is an
  upper bound), `positioning` 4, `instrument_meta` <1.
- That lands **inside** `specs/01b`'s "well under 100 MB/month" target after compression, which
  resolves Q3 empirically: the spec's figure was right on disk and wrong pre-compression.

## F-029 — All seven `specs/02` §1 PENDING branches resolved (PHASE-01)

**Date:** 2026-09-27. Each branch is decided by recorded Phase-1 evidence; the taken branch and its
consequence are below. No branch is left open, and none was decided by preference.

| Probe | Branch taken | Evidence | Consequence for the build |
|---|---|---|---|
| **P1** history depth | **"≥ 3 years" → full walk-forward protocol** (`specs/09` §2.3) | F-018: 1h continuous history from 2018-08/09 on spot (≈8 y) and 2022-01 on futures (≈4.7 y), ≥99.8% complete | Full protocol, ~24+ folds. 1d also fittable on spot (3,320 daily bars) but stays **context/gating only** per `specs/02` §3. **Window boundaries must start at each series' `continuous_from`, not its first bar** |
| **P11** UTC alignment | **Aligned — no redefinition needed** | F-012: 4,800 bars across both products, every `open_time % 3600000 / 86400000 == 0`; F-008 confirms the same for futures 15/30/240/480 | Daily features, prior-day levels and the 4h grid use plain UTC boundaries. No IST offset anywhere |
| **P12** aggregation exactness | **Aggregation is safe — 4h and futures 15m retained** | F-013: spot 15×1m→15m and futures 3×5m→15, 4×60→240 all **100% exact** over 960/60 windows per pair; 14,400/14,400 1m bars present, **zero** zero-volume bars omitted | The feared failure mode was not observed. **`known_absent_bars` is still built** (PHASE-07) as the guard, because absence of evidence over 10 days is not proof, and `specs/11` §7.2 requires aggregation to refuse across an unconfirmed gap |
| **P6** futures volume units | **Base-denominated — no conversion** | F-010: Σ(volume×close) over 24 h ÷ rt `v` = 0.966 / 0.952 / 0.976 | All three candle volume fields are base; only `current_prices` `v` is quote. Hazard §5 #4 closed |
| **P3** spot markets | **All three exist** | F-007: `B-BTC_USDT`, `B-ETH_USDT`, `B-SOL_USDT`, ecode B, active, `min_notional` 5 | Class 1 coverage is symmetric; the spot leg exists for all three. **But F-027**: spot turnover is 8–15% of futures, so spot is small-size / cross-reference only |
| **P8** `data/stats` | **Reachable — Family 7 and C2-C retained** | F-019: public unauthenticated GET, both `count_percent` and `value_percent` populated and summing to 100 | `POS_*` features and C2-C survive. They remain **Tier B** (no history), so `validated: false` until `earliest_tier_b_validation` |
| **P10** basis signal-to-noise | **Basis retained, but as a consistency check only** | F-023: mean −3.85/−4.14/−2.00 bps at σ 0.70/0.71/1.01 → ≈5σ, so **not** noise-dominated; the `specs/02` §1 cut-to-funding-only branch does **not** trigger | `BAS_BPS`/`BAS_Z` are implemented. **Magnitude (~4 bps) is well below the 11.8 bps round trip**, so basis may never be presented as a tradable dislocation — exactly the C1-A condition-3 data-quality use `specs/03` §1 already specifies |

**Two consequences that change later phases, recorded here so they are not rediscovered:**

1. **P1's answer is per-series.** `specs/09` §2's fold boundaries must be computed from the
   `continuous_from` dates in `data/backfill/verify.json`, not from the first bar in each file. Using
   the first bar would put the earliest folds inside a months-long hole (F-018).
2. **P10 leaves C1-A's mechanism unobserved.** Funding sat at its 0.0001 baseline for the whole
   6-hour sample, so no funding extreme has ever been seen live by this system. C1-A cannot be
   evaluated at all until the recorder captures one — which makes continuous 24/7 recording a
   prerequisite for Class 1's Tier B member, not just a nice-to-have.

## F-030 — `specs/03` amendment applied: C1-C condition 7

**Date:** 2026-09-27. `specs/04` §7 identified a genuine specification gap: C1-C (fade a
cross-asset residual) and C2-B (follow the factor trend) could both fire on the same residual in
opposite directions, because they shared no partition.

**Applied to `specs/03` §5:** added **condition 7 — `MR_VR(1h) < 1` on the dislocated asset.**

This restores the same mutual-exclusivity partition that separates C1-B from C2-A, and it is now
one of the four partitions that must be asserted under test (`specs/13` §15). The interim
`CLASS_CONFLICT` suppression rule from `specs/04` §7 remains the fallback for any *other* opposing
pair, and is superseded in general by the `specs/07` §4 hierarchy.

## F-031 — the variance-ratio formula in `specs/02` divides by `k` twice

`specs/02` Family 4 states the Lo-MacKinlay variance ratio as

```
sigma2_k = Sum (lnP_t - lnP_{t-k} - k*mu)^2 / m,   m = k(nk - k + 1)(1 - k/(nk))
VR(k)    = sigma2_k / (k * sigma2_1)
```

`m` already contains the factor `k`, so `sigma2_k` is an estimate of the **per-period** variance of
k-period returns. Dividing by `k` again biases every value down by a factor of `k`.

Measured on simulated series (3,000 observations, `numpy` default RNG, seed 7):

| Series | | k=2 | k=4 | k=8 |
|---|---|---|---|---|
| Random walk | literal `specs/02` | 0.509 | 0.264 | 0.140 |
| Random walk | `sigma2_k / sigma2_1` | **1.017** | **1.058** | **1.117** |
| AR(1), phi = -0.4 | literal | 0.295 | — | — |
| AR(1), phi = -0.4 | `sigma2_k / sigma2_1` | **0.591** | — | — |

The literal formula contradicts the interpretation `specs/02` gives two lines later — "`VR > 1` ->
trending, `VR < 1` -> mean-reverting, `VR approx 1` -> random walk" — because under it a random walk
reads 0.5, 0.26 and 0.14 rather than ~1.

**Why this matters beyond tidiness.** `MR_VR(1h)` is the **P axis of the regime model** (`specs/02`
§6) and appears as a hard condition in C1-B (2), C1-C (7, added by F-030) and C2-A (3). With the
literal formula every asset reads mean-reverting at all times: the `MR_VR < 1` conditions would pass
permanently, the `MR_VR > 1` condition in C2-A would never pass, and the regime model's second axis
would carry almost no information. The failure is silent — the numbers look plausible.

**Resolution applied:** `src/features/numerics.py` implements `VR(k) = sigma2_k / sigma2_1`, the
standard Lo-MacKinlay ratio and the one consistent with the spec's stated interpretation. The
docstring records the deviation. `tests/test_features_numerics.py` asserts ~1 on a random walk, > 1
on a positively autocorrelated series and < 1 on a mean-reverting one — the test that would have
caught this.

**Owner decision needed (Q7):** amend `specs/02` Family 4 to drop the extra `k`, or state a
different intent.

## F-032 — the order book is far shallower than the depth vetoes assume; futures is capped at 50 levels

Measured 2026-09-28 against the live endpoints, BTC:

| Endpoint | Requested depth | HTTP | Levels returned | Reach from mid |
|---|---|---|---|---|
| `orderbook/B-BTC_USDT-futures/{d}` | 50 | 200 | 50 / 50 | **±0.74 bps** |
| | 100 | **200** | **0 / 0 (empty)** | — |
| | 200 | **200** | **0 / 0 (empty)** | — |
| | 500 | **200** | **0 / 0 (empty)** | — |
| `orderbook?pair=B-BTC_USDT&depth={d}` | 50 | 200 | 50 / 50 | ±1.01 bps |
| | 100 | 200 | 100 / 100 | **±1.79 bps** |
| | 500 | 422 | — | — |

Two separate facts:

**1. The futures book cannot be fetched deeper than 50 levels.** Depths 100, 200 and 500 return
HTTP **200 with an empty book** — the F-014 trap, now confirmed for every depth above 50. A caller
that treats 200-with-empty-book as "no liquidity" would conclude the market had vanished. The
normalizer already rejects an empty book as invalid (`normalize/canonical.py`), so this surfaces as a
rejection rather than as zero depth.

**2. No depth band `specs/08` §7.1 relies on is observable.** The futures book spans ±0.74 bps and
the spot book ±1.79 bps at its maximum usable depth. The spec's veto is stated over **25 bps**, which
is 14× wider than anything visible, and F-025's earlier ±3.6 bps measurement was also well short of
it. Even the 1 bps band is marginal on futures.

**Consequences, implemented:**

- `LIQ_DEPTH` reports the notional of the **entire visible book** as its scalar, paired with
  `visible_reach_bps`, and keeps the 1/2/5/10/25/50 bps bands in its detail with `None` where the book
  does not reach. A `None` band means *unknown*, never zero. Labelling ±0.75 bps of book as a "25 bps"
  figure would understate true depth and read identically for 10, 25 and 50 bps.
- `LIQ_IMBAL` measures over the visible book when 1 bps is not covered, and states the band it used.
- The risk engine (PHASE-21) **cannot implement the 25 bps depth veto as written.** It must either
  size against visible depth with an explicit unmeasurability flag, or refuse sizes it cannot justify.
  This is a data-availability limit at this venue, not an implementation shortcut.
- The recorder polls depth 50 for both books. Spot could usefully be raised to 100 (it works and
  nearly doubles reach); futures cannot be improved. That is an owner decision (Q8) because it changes
  the irreplaceable stream's shape.

## F-033 — the regime model passes separation and occupancy but **fails persistence on BTC and SOL**

Fitted 2026-09-28 on 4,001 stored 1h futures bars per asset (~5.5 months), axes `VOL_RATIO(24/720)`
and `MR_VR_K2(24)`, trailing quantile lookback 720, per `specs/02` §6.

### The forward horizon decides the separation test, so it was fixed from the specs

`specs/02` §6 requires forward return distributions to "differ measurably across cells" but does not
say over what horizon. It turns out to decide the answer:

| Asset | Grid | Kruskal-Wallis p at fwd = 1 bar | 6 bars | 24 bars |
|---|---|---|---|---|
| BTC | 3×3 | 0.889 | 0.399 | **0.000** |
| BTC | 2×2 | 0.925 | 0.160 | **0.000** |
| ETH | 3×3 | 0.657 | 0.001 | **0.000** |
| ETH | 2×2 | 0.549 | 0.032 | **0.000** |
| SOL | 3×3 | 0.361 | 0.000 | **0.000** |
| SOL | 2×2 | 0.550 | 0.130 | **0.000** |

A one-bar test says there is no structure anywhere; a 24-bar test says there is structure everywhere.
Choosing the horizon that passes would be precisely the curve-fitting the spec's trailing-quantile rule
exists to prevent, so it is anchored to the horizon the **strategies** operate on: `specs/03` §5 expires
an untriggered setup after "3 funding intervals (24h)" and `specs/06` §4 says holding periods "land in
days". `SEPARATION_FORWARD_BARS = 24`, recorded in the artifact as `separation_horizon_bars`, and a
separation verdict is never reported without it.

### The outcome at 24 bars

| Asset | Occupancy (min cell share) | Persistence (median dwell) | Separation (p) | Verdict |
|---|---|---|---|---|
| **BTC** | PASS (0.221) | **FAIL — 2.0 bars** (need ≥ 3) | PASS (5.1e-06) | **degraded** |
| **ETH** | PASS (0.235) | PASS — 3.0 bars | PASS (2.6e-10) | **usable, 2×2** |
| **SOL** | PASS (0.214) | **FAIL — 2.0 bars** | PASS (2.2e-05) | **degraded** |

3×3 fails persistence on all three assets (median dwell 2.0 everywhere), so the escalation collapses to
2×2 as `specs/12` §10 prescribes; ETH then passes all three and BTC and SOL do not.

### What this means

The axes **do** carry information about the next day — separation is significant at p < 1e-4 on every
asset, which is a positive result and worth stating plainly. What fails is the *label's stability*: on
BTC and SOL the cell changes every two bars, and `specs/02` §6 is explicit that "a label that flips
every bar is not a regime, it is a noisy indicator with a fancy name".

So the system runs **per-asset mixed mode**, which the degraded design already supports:

- **ETH** uses the full model: cell gating, dwell stability (C1-B 3, C2-A 7, C3-B 7) and
  `regime_persistence_prob` are all available.
- **BTC and SOL** run degraded: every raw-axis threshold still applies (`MR_VR < 1` in C1-B 2 / C1-C 7,
  `MR_VR > 1` in C2-A 3, the `VOL_RATIO` percentile in C3-A 1), the dwell-stability conditions are
  dropped, and C3-B publishes `regime_persistence_prob: null` with a reason. Reduced selectivity, not
  broken strategies.
- The dashboard must show which assets are degraded, since two of three are.

### Caveats on this fit

- 4,001 bars is ~5.5 months. The quantile lookback consumes 720 of them, and `VOL_RATIO` needs 721 bars
  to exist at all, so only 3,281 bars are classifiable. The fit should be repeated once a longer window
  is available; `specs/09`'s walk-forward protocol will re-fit per fold regardless.
- Persistence is measured on runs of the cell label. Because the cut points are trailing quantiles
  recomputed every bar, a value sitting near a boundary flips the label without the market changing.
  That is inherent to the method the spec prescribes, and it is the most likely reason dwell is short.
- **This is a substantive finding about this market at this horizon**, which is what `specs/12` §10 asks
  to be recorded either way.

Artifacts: `config/regime_model_B-{BTC,ETH,SOL}_USDT.json`, summary in
`data/probe_results/regime_fit.json`.

## F-034 — failure drill: 15 deliberate failures, all classified and actionable

Run 2026-09-29 (`python -m probes.failure_drill`, result in
`data/probe_results/failure_drill.json`). Each drill breaks one thing for real and records what the
system says. The question is not "does it fail" but **"would the message tell an operator what to do
at 3 a.m.?"** — which means naming the component, the likely cause, the remediation, and above all the
**problem class**, since on Azure that is what decides whether to look at the code, the configuration,
the VM or the venue.

| drill | what was broken | code | problem class |
|---|---|---|---|
| missing database | pointed the API at a database that does not exist | `STORE_FAILURE` | **infrastructure** |
| corrupt database | wrote text into a .db file and queried it | `STORE_FAILURE` | **infrastructure** |
| missing regime artifact | removed regime_model_*.json | `CONFIG_INVALID` | **configuration** |
| corrupt regime artifact | truncated regime_model_*.json mid-object | `CONFIG_INVALID` | **configuration** |
| missing parameter file | pointed the registry at a path with no parameters.toml | `CONFIG_INVALID` | **configuration** |
| parameter budget | nothing (checking the guard exists) | `CONFIG_PARAMETER_INVALID` | **configuration** |
| public bind, no token | asked the API to bind 0.0.0.0 with no auth token | `STARTUP_FAILED` | **configuration** |
| shallow snapshot request | asked for 50 bars where the registry needs 721 | `STARTUP_FAILED` | **configuration** |
| clock behind data | set the clock five hours behind the newest stored bar | `CLOCK_SKEWED` | **infrastructure** |
| stale Tier B | futures_rt is 9,000 s old against a 60 s window | `DQ_FAILED` | **data_quality** |
| store failure behind the API | the database connection was closed under a live request | `STORE_FAILURE` | **infrastructure** |
| network failure | a request to a host that does not resolve | `EXTERNAL_TRANSPORT` | **external_dependency** |
| aggregation across a gap | aggregated 4h from three of four hourly bars | `DQ_GAP` | **data_quality** |
| risk engine mutation | altered a published stop after the strategy set it | `RISK_LEVELS_MUTATED` | **application_bug** |
| control strategy live | evaluated C4-X1 outside backtest mode | `STRATEGY_FAILURE` | **application_bug** |

**Result: 15 of 15 actionable**, 0 silent. "Actionable" is asserted, not asserted-by-eye: the drill
requires a problem class, a likely cause and a remediation on every failure, and fails its own exit
code otherwise.

### Two real defects the drill found, both fixed

1. **A missing `config/parameters.toml` raised a bare `FileNotFoundError`.** The single most likely
   deployment mistake — running from the wrong directory, or not shipping the file — produced an
   unclassified traceback that named the path and nothing else. `load_registry` now raises
   `ConfigError` for a missing file, an unreadable one and malformed TOML, each with its own
   remediation. This is exactly the class of startup failure the local-first instruction was meant to
   surface before Azure, and it would have cost an hour on a first deploy.

2. **A `sqlite3` connection was shared across threads.** FastAPI runs a sync handler in a worker
   thread, and a connection belongs to the thread that created it — so the dashboard returned HTTP 500
   on its first request while the engine carried on writing happily. Found by `tests/test_api.py`
   before the drill; fixed with `store/reader.py`, one **read-only connection per thread**, with
   read-only enforced by SQLite's `mode=ro` URI rather than by convention.

### What the drill confirms about the design

- A **stale Tier B source** gates the family (`DQ_FAILED` with the age and the window) rather than
  publishing a stale number.
- A **store failure behind the API** renders `DOWN` with a correlation id and keeps the payload's
  shape — it never renders as an empty market, which `specs/14` §10 calls the most dangerous thing
  this API can do.
- **Aggregating across an unconfirmed gap** is refused with the reason, not computed from what is
  present.
- The **risk engine mutating a published level** raises `RISK_LEVELS_MUTATED` at CRITICAL, and a
  **control strategy evaluated outside backtest** refuses — both classified `application_bug`,
  because both mean the code is wrong rather than the market.

---

## F-035 — A dead feed reported itself as "warming": the series-state check tested depth before staleness ✅

**Date:** 2026-10-03. **Found by:** running `python -m app --once` after a two-week break and reading
the `snapshot_incomplete` payload rather than the summary line.

The report said, for every alpha series:

```
'reason': 'WARMING', 'detail': 'need 1440 1h bars, have 1105 (335.0h more to warm)'
```

Both halves were true. Ingestion had also been stopped since 2026-09-19, so the newest stored 1h bar
was **14 days old** — and nothing said so. `SnapshotBuilder._series` checked the three failure reasons
in the order `EMPTY -> WARMING -> EDGE_BEHIND`, and a window that is short *and* stale hits `WARMING`
first and returns.

The two messages are not interchangeable:

| Reason | What an operator does |
|---|---|
| `WARMING` | nothing — wait for history to accumulate |
| `EDGE_BEHIND` | restart ingestion; the series describes the past |

Reporting the reassuring one for a dead feed is how a stopped process goes unnoticed for a fortnight.

**Fix.** Staleness is now checked **before** depth, and when both hold the shortfall is named as a
consequence rather than as a separate problem: *"the window is also 331 bars short, but that is a
consequence of the stale edge, not a separate problem."* Covered by
`tests/test_snapshot.py::test_a_window_that_is_both_short_and_stale_reports_the_stale_edge_not_the_warm_up`
and by its converse, so "give it time" still wins when the edge *is* current.

**Related process gap, fixed the same day.** There was no runnable process that kept the SQLite tables
current. `python -m recorder` appends raw JSONL (hard invariant 8), and `store.import_raw` bridges
JSONL to SQLite for the **Tier B streams only** — it has no candle importer. `ingest/pipeline.py` held
the candle polling but had no `__main__`, so nothing ever ran it. Running the recorder alone therefore
leaves `bars` frozen wherever ingest last ran. `python -m ingest` now exists (`--check` / `--once` /
`--futures-only`), with a startup assertion that the candle set includes futures 15m and 1m, because
without those ACT arms and then waits forever without reporting a failure.

---

## F-036 — 332 of 1,440 1h bars were present, correct, and invisible: history pulled once never finalizes ✅

**Date:** 2026-10-03. **Found by:** `detect_gaps` reporting **zero** missing bars on a window the
snapshot builder said was 331 bars short. Two correct answers to what looked like the same question.

The difference is `final_only`. `detect_gaps` passes `final_only=False`; `SnapshotBuilder` passes
`final_only=True`. Counting both ways:

```
B-BTC_USDT futures, last 1440 h   is_final=0: 332   is_final=1: 1108
                       4h                      80            280
                       1d                      10             50
                      15m                    1339           4421
                       1m                   20122          66278
```

A bar becomes final only when a **second** fetch returns it unchanged (specs/11 §6.2, the two-fetch
rule). `poll_candles` re-fetches `refetch_bars` back from the newest stored bar, so a catch-up that
pulls two weeks of history in one request leaves every one of those bars non-final, and the next pass
starts from the new edge and never looks at them again.

The result is the failure mode this project treats as worst: data that is **present and correct in the
database and completely unusable**, reported indefinitely as `WARMING` while the row count says it is
there. Nothing is logged, because nothing is wrong.

**Fix.** `SqliteStore.non_final_bar_times(..., settled_before_ms=, limit=)` finds them, oldest first,
excluding bars close enough to now that the venue may still be revising them legitimately. One
`settle:` task per series (`IngestPipeline.poll_settle`) re-requests the oldest contiguous window, at
most `SETTLE_BATCH_BARS = 300` per pass, at priority **P2** — below the live polls, because settling
old history matters and never matters more than knowing the current price. A pass with nothing to
settle makes no request at all, which is what lets the cadence be brisk.

Measured on the first real pass:

```
4h  requested=79  returned=80   remaining_non_final=0
1d  requested=9   returned=10   remaining_non_final=0
1h  requested=300 returned=301  remaining_non_final=30
15m requested=300 returned=301  remaining_non_final=1036
1m  requested=300 returned=301  remaining_non_final=19819
```

A bar the venue no longer returns is left non-final rather than promoted: an unconfirmable bar is
exactly what the rule exists to exclude. A bar that comes back **different** still raises a
`BAR_MUTATION`, handled identically to the live path.

---

## F-037 — `content_hash` compared Decimal strings, so `105` and `105.0` were a mutation ✅

**Date:** 2026-10-03. **Found by:** writing the F-036 regression test and watching five identical bars
refuse to finalize.

`content_hash` built its payload from `str(Decimal)`:

```python
payload = "|".join(str(x) for x in (bar.open, bar.high, bar.low, bar.close, bar.volume_base))
```

`str(Decimal("105"))` is `"105"` and `str(Decimal("105.0"))` is `"105.0"`. Same price, different hash.

Two consequences, in opposite directions:

1. **A false `BAR_MUTATION`** — logged at ERROR, recorded as an `external_dependency` DQ event, and
   marking every signal at that `as_of` non-replayable. The loudest false alarm the store can raise,
   and a flood of them would train an operator to ignore the one alert that means history really did
   change.
2. **The two-fetch rule never fires**, so the F-036 sweep would re-request the same window forever and
   settle nothing.

This is not hypothetical: hard invariant 6 already records the venue sending inconsistent precision on
order books — `"11570.67000000"` beside `"2001"`.

**Fix.** `canonical_decimal()` returns `format(Decimal(v).normalize(), "f")`, which is equal whenever
the *value* is equal (`'f'` because `normalize()` turns `Decimal("100")` into `1E+2`). `content_hash`
uses it, and `same_content(row, bar)` compares the stored columns **by value** as the authority, with
the hash kept as the fast path — so rows written before the change do not all read as mutations the
first time they are re-fetched. Four tests cover it, including that a genuinely changed price is still
a mutation.

---

## F-038 — θ calibrated structurally at 0.75; the two criteria disagree, and the lookbacks are why ✅

**Date:** 2026-10-03. **Probe:** `python -m probes.swing_calibration`, grid at
`data/probe_results/swing_calibration.json`. **Window:** all stored futures history
(BTC 1h from 2022-01-19, ETH 1h from 2022-01-19, SOL 1h from 2022-06-30), final bars only.

`swing_atr_multiple` (θ, registry parameter 10) shipped `unset` with `value = 0.0`, which made every
Family 3 feature report `PARAMETER_UNSET` and silently disabled the structural conditions in C2-A/B/C,
C3-B and C3-C. It was the last uncalibrated parameter. It is now **0.75, `provisional`**, and the
registry has **zero** `unset` entries at exactly 30 of 30.

### Calibrated from structure only — no returns, no performance, no P&L

θ decides what counts as a swing pivot, which decides what C2-A's stop references, which decides every
`R` downstream. Choosing it by what made money on the training window would fit the structure
*definition* to one price path, and every strategy reading `STR_SWING` would inherit the fit with
nothing looking wrong. Three criteria, each tied to what a consumer actually needs:

| Criterion | What it measures | Why it is the shape it is |
|---|---|---|
| **availability** | fraction of evaluation points where the consumer's feature is *defined* | failure mode is **silence** — the condition never evaluates, which looks like "no setups today" |
| **spacing** | median bars between consecutive pivots vs `lookback_short / 2` | pivots alternate high and low, so one swing cycle is two spacings |
| **dispersion** | cross-asset CV of pivot density | one shared θ is only defensible if it means the same thing on BTC, ETH and SOL |

Availability is required **per pair**, because the requirement differs: `1h/short` is read for
`last_swing_high` / `last_swing_low` and needs **one pivot of each kind**; `1h/medium` feeds `STR_SEQ`
through `structure_intact` and needs **two of each**. One shared rule would either excuse a useless θ
or reject a usable one.

### The grid

```
 theta   avail   worst cell  spacing_err  dispersion  viable
  0.25   0.917     1h/short       0.6667      0.0191  yes
  0.50   0.917     1h/short       0.6528      0.0164  yes
  0.60   0.917     1h/short       0.6528      0.0128  yes
  0.75   0.883     1h/short       0.6250      0.0128  yes   <- chosen
  0.90   0.850     1h/short       0.6250      0.0210  yes
  1.00   0.842     1h/short       0.6250      0.0221  yes
  1.25   0.700     1h/short       0.6250      0.0238  no
  1.50   0.450     1h/short       0.5833      0.0294  no
  2.00   0.092     1h/short       0.4583      0.0407  no
  3.00   0.000     1h/short       0.8021      0.1190  no
```

Availability falls monotonically in θ, as it must — θ is a significance threshold, so raising it can
only remove pivots. The floor (0.80) is crossed between 1.00 and 1.25. Among viable candidates the
spacing error ties at 0.625 for 0.75 / 0.90 / 1.00, and **dispersion breaks the tie at 0.75** (0.0128
against 0.0210 and 0.0221), which also happens to have the highest availability of the three. The
optimum is flat — θ between 0.5 and 1.0 changes little — and that flatness is itself worth recording:
θ is a scale, not a sensitive knob.

### ⚠ The two criteria disagree, and the probe says so rather than averaging over it

Availability caps θ at **1.0**; spacing is best at **2.0**, where availability has collapsed to 0.09.
No single θ satisfies both, and the cause is **not** θ:

```
1h/medium median spacing by theta:  4.0  5.0  5.0  6.0  10.0  (theta 0.25 -> 2.0)
1h/short  median spacing by theta:  4.0  4.0  4.0  4.0   3.0
```

On `1h/short` the median is pinned at 4 bars across the whole usable grid, because with `k = 3`
confirmation two confirmable pivots cannot sit much closer than `k + 1` bars — the **window's
geometry** decides it, not θ. And `lookback_1h_short = 24` with `k = 3` simply cannot host the 12-bar
swings the trading horizon implies: by the time θ is large enough for a swing to be meaningful, a
24-bar window contains no confirmed pivot of each kind and C2-A's structural stop has nothing to
reference.

That is a statement about the **shared lookbacks**, not about θ. Changing `lookback_1h_short` would
alter every feature on the 1h timeframe, so it is raised as **Q11** rather than decided here. θ = 0.75
is the availability-respecting end of the conflict, which is a large part of why it is `provisional`
and not `fixed`. `probes.swing_calibration.tension()` computes and prints the gap on every run, so it
cannot quietly stop being true.

### Two mistakes the probe caught by refusing to answer

Both runs returned **"NO VIABLE THETA"** before the method was right, and both times the probe was
correct to decline:

1. **Measuring an impossibility.** The first run scored every (timeframe, horizon) pair, including
   `4h/short` (6 bars) and `1d/short` (7 bars) — fewer than the `2k + 2 = 8` bars a pivot needs to
   exist. Availability was 0.0 for every candidate. `STR_SWING` and `STR_SEQ` now declare
   `min_lookback = MIN_PIVOT_BARS`, so the engine **skips** those four combinations instead of
   computing and failing them on every evaluation; `undefined_combinations` went from 1 to 5. Before
   this, an arithmetic impossibility was being reported once per bar as a data-quality failure,
   indistinguishable from a real one.
2. **Deriving the spacing target per window instead of per timeframe.** Keying the target by horizon
   gave `1h/medium` a target of 84 bars (168 / 2). Wrong: pivot spacing is a property of the price
   process and θ, so looking back 168 bars reveals *more* swings, not longer ones. Both horizons on a
   timeframe share one target, derived from that timeframe's **short** lookback.

A calibration that always produces a number is indistinguishable from a guess.
`tests/test_swing_calibration.py` holds 13 tests, and the ones that matter most assert the refusals:
the probe raises `ConfigError` when no pair can hold a pivot, returns `chosen: null` when nothing
clears the floor, and the CLI exits non-zero so an operator cannot read "calibrated" from a run that
calibrated nothing.

---

## F-039 — The backtest window's start is derived from coverage, and coverage must be read per tier ✅

**Date:** 2026-10-03. **Found by:** `python -m backtest --window train --coverage` on the first
pre-registered window, which reported **0% usable** over 1,246 steps.

Two separate problems, and the first one hid the second.

### The reported number was useless, not wrong

`BacktestDriver.coverage()` counted a step as unusable if the snapshot had **any** problem. Over a
historical window every step has one, because **Tier B has zero history**: funding, basis,
positioning and the book are point-in-time with no archive before the recorder started in 2026-09.
`specs/09` §13 already anticipates this with a separate Tier B timeline — but a single
`usable_fraction` of 0.0 makes a perfectly sound Tier A backtest look impossible.

Worse, the ranked `reasons` list was truncated to its top 12 for readability, and with 640 Tier B
entries at one per source per asset, the truncation showed **only Tier B** and hid the handful of
Tier A reasons that were actually actionable.

Coverage now reports three readings, and the Tier A reasons in full:

```
steps                      1246
usable_fraction             0.000   <- strict: the only reading a Tier B strategy can be judged on
tier_a_usable_fraction      0.568   <- the one that matters for a Tier A backtest
tier_b_only_fraction        0.568   <- expected to be ~100% over history; if not, something is wrong
tier_a_reasons   {EMPTY: 2978, WARMING: 951, EDGE_BEHIND: 21}
```

### The window itself was wrong, and the fix is a derivation rather than a choice

With the Tier A number visible, 56.8% was the real problem. Measured per series on stored futures
history:

| series | first bar |
|---|---|
| 1h | 2022-01-19 (BTC, ETH), 2022-06-30 (SOL) |
| 1m | 2021-01-01 (BTC), 2022-06-30 (SOL), 2022-07-06 (ETH) |
| 1d | 2021-01-01 (BTC), 2022-07-01 (ETH, SOL) |
| **15m** | **2023-01-11 (BTC), 2023-01-23 (ETH, SOL)** |
| **4h** | **2023-01-11 (BTC), 2023-01-23 (ETH, SOL)** |

15m and 4h simply do not exist before 2023-01-23 — the venue's own history, not a gap in ours. The
pre-registered window started 2022-02-01, so for its first eleven months the snapshot could not build
two of the five required series, and ACT could not size a trigger at all.

Adding the warm-up on top (the 1d series needs 180 bars beyond the first) puts the earliest fully
warm `as_of` in late July 2023. The window was amended to start **2023-08-01**, which is a derivation
from coverage rather than a preference:

```
tier_a_usable_fraction 0.568 -> 0.914   (700 steps on a daily grid)
```

The amendment was made **before any result was seen**, so it did not burn the holdout — which is
exactly the situation `specs/09` §1.3 contemplates, and the reason the protocol was locked and then
coverage checked, in that order.

2023-08-01 → 2026-09-30 is **38 months**: `specs/09` §2.3's full protocol, with ~26 walk-forward folds
on the 32 months before the holdout at §2.2's 6-month train / 1-month test / 1-month step.

### What this leaves open

Two of twelve strategies — **C1-A and C2-C** — read Tier B exclusively and therefore **cannot be
backtested at all**. Not weakly: there is no data. They would reach shadow with no historical evidence
whatsoever, which is recorded as an open question rather than resolved by running them anyway.

---

## F-040 — The repository is clean for publication: no secret has ever been committed ✅

**Date:** 2026-10-04. **Check:** the `specs/18` §1 pre-push scan, run over the **entire** history
rather than the working tree:

```
git log -p --all | grep -icE "(api[-_]?key|api[-_]?secret|x-auth-apikey|BEGIN RSA|BEGIN OPENSSH)"
-> 22
```

Twenty-two matches, all inspected. Every one is a **variable name, a header name, documentation, a
redaction test, or a test fixture with an obviously fake value** — `COINDCX_API_KEY` as an
identifier, `X-AUTH-APIKEY` as a header, `"abc"` / `"def"` / `"k3y-val"` in tests, and the
`SENSITIVE_HEADERS` set that exists to redact them. No real credential appears at any point in the
history.

Structurally confirmed too: `git ls-files` shows **0** files under `data/` and `.env` is not tracked.
That matters more than it sounds — `data/` holds the holdout, and `specs/09` §1.2 requires it to stay
out of any shared repository where it could be inspected before Stage 10.

This is recorded because the check has to be *done*, not assumed, and because the answer changes what
is safe: the repository can be public, which makes the Azure deploy a one-line `git clone` instead of
a deploy key.

---

## F-041 — The history bootstrap, and an off-by-one that only a test could have found ✅

**Date:** 2026-10-04. **Context:** hosting needs a fresh machine to rebuild price history from the
API, because the 5.8 GB development database cannot be pushed to a repository (it holds the holdout)
and copying it by hand is a step that can go wrong silently.

`python -m bootstrap` does that. Measured on an **empty** database:

```
15 requests · 17,595 bars · ~20 seconds · 0 series incomplete
  written final immediately   17,535
  left for the live sweep         60
```

Then `ingest --once` (68 tasks, all OK) and `app --once` produced **5 signals in 350 ms**. So the
whole fresh-host sequence — no data, no configuration beyond a token — reaches a working system in
under a minute.

### What the signals' vetoes confirmed

On the fresh database the signals were declined for `PRICE_BAND`, `RR_FLOOR` and `FUNDING_ADVERSE` —
**economic** vetoes. The backtest's zero-trade problem (Q-N) is `DQ_STALE` + `DQ_META`, which are
*historical* data vetoes. Different causes, and this confirms the live path does not share the
backtest's blocker.

### The off-by-one

`plan()` counted expected bars over `[start, now]`. When `now` lands exactly on a timeframe boundary
that range includes the bar opening *at* `now` — which has not closed, cannot be fetched, and so is
permanently missing. The consequences:

* the plan would never report complete;
* `bootstrap --check` would exit non-zero forever, so the health check would cry wolf;
* a cron-driven `--run` would re-request the newest chunk on every pass.

It did **not** show up in any live run, because the wall clock almost never lands exactly on an hour
boundary. It showed up immediately in four tests that use a synthetic `T0` on a boundary. Fixed by
ending the window at the newest **closed** bar, which is the same convention
`SnapshotBuilder._series` already uses — so the bootstrap and the snapshot builder now agree about
what "the latest bar" means instead of each having its own idea.

### The finalization shortcut, and why it is defensible

A bar becomes `is_final` only when a second fetch returns it unchanged, and
`get_bars(final_only=True)` — what the snapshot builder uses — cannot see a non-final bar. A naive
bootstrap therefore writes correct history that is **completely invisible**, which is F-036 again.

The rule exists to catch a bar still forming or being revised. A bar that closed six months ago is
neither, so the bootstrap writes bars older than three intervals **final on first fetch** and leaves
the recent tail to the live `settle:` sweep. To keep that honest rather than merely convenient,
`verify_chunk` re-requests a sample and compares **by value** (so a precision difference is not a
difference, per F-037); any real mismatch fails the run with the window and field named, because
writing history final on one fetch is only safe if a second fetch agrees.

---

## F-042 — The first real deployment: four defects, and one of them turned a mistake into an outage ✅

**Date:** 2026-10-07 (deployment), 2026-10-10 (analysis and fixes). **Evidence:** the operator's full
terminal log of an Azure install, start to finish.

The deployment itself worked. The VM came up, the repository cloned, dependencies installed, the
bootstrap fetched 17,580 bars in 15 requests, both services started `active (running)`, the local API
returned **401 without a token and 200 with one**, and the tunnel registered. Then the dashboard said
*"the engine did not answer within 8 seconds"* and stayed that way.

Four defects, in the order they bit.

### 1. A guide line missing its `sudo -u crypto` prefix — the trigger

Every command in `HOSTING.md` that touches `data/` or `logs/` carries that prefix, because the
services run as `crypto` and those directories belong to `crypto`. One did not: the optional
`--profile full` note was written as prose rather than as a command block, and so lost it.

```
$ /opt/crypto-trader/.venv/bin/python -m bootstrap --run --profile full
PermissionError: [Errno 13] Permission denied: '/opt/crypto-trader/logs/bootstrap.jsonl'
```

**Fixed:** the note is a proper command block with the prefix, and it is followed by the rule stated
explicitly — *every* command touching those directories needs it, and a permission error means the
prefix is missing rather than that ownership needs changing.

### 2. `chown` as the obvious fix — the amplifier

```
$ sudo chown -R azureuser:azureuser /opt/crypto-trader/logs
```

Entirely reasonable in isolation, and it hands the directory to the wrong user. From that moment the
two running services could not write their logs. Then:

```
$ /opt/crypto-trader/.venv/bin/python -m bootstrap --run --profile full
BOOTSTRAP FAILED: the database is read-only
```

Because `data/` still belonged to `crypto` while the command ran as `azureuser` — the mirror image of
the first error, and a sign the ownership model had been half-inverted.

### 3. **A log file the process could not write killed the process** — the real defect

This is the one worth keeping. `setup_logging` called `mkdir` and opened a `RotatingFileHandler` with
nothing catching `OSError`, so an unwritable log directory raised during startup. systemd restarted
the service, it raised again, and it has been looping ever since on `RestartSec=10`.

The console handler was attached and working the whole time. **journald had every line.** The process
refused to run because it could not *write about* running.

The consequences compounded badly:

* `systemctl is-active` says `active` during the brief window between start and crash, so the
  services looked healthy;
* a crash-looping app accepts no connections, so the tunnel had nothing to forward and Cloudflare
  timed out;
* the only symptom reaching the operator was the dashboard's *"the engine did not answer"* — true,
  and four layers away from the cause.

**Fixed:** file logging now degrades. The handler is attempted, `OSError` is caught, and the process
continues on console logging with a WARNING that names the path, the error, `journalctl` as the place
the logs still are, and the `chown` that restores the files. A failure to log is not a failure to
work, and nothing about correctness depends on it.

### 4. The post-run check moved its own goalposts

```
remaining incomplete: 3 series, 3 bars
```

on a run that had fetched everything it planned. `bootstrap --main__` re-planned at the clock's
*current* value, so a run lasting a minute ended with its window a minute longer — and the 1m bar that
closed **during** the run, never in the plan, counted as missing. Since the exit code follows that
comparison, the command reported failure and the health check would have warned permanently.

**Fixed:** the re-plan uses the same `now_ms` the run was planned with. The honest question is "did I
fetch what I set out to fetch"; bars that closed since are the live ingest process's job.

### A fifth, smaller one: the executable bit

```
$ /opt/crypto-trader/deploy/azure/health-check.sh
-bash: Permission denied
```

`core.filemode=false` on Windows meant the `chmod +x` never reached the Git index — the scripts were
committed `100644`. Fixed with `git update-index --chmod=+x`, and every invocation in the
documentation now says `bash <script>`, which works either way.

### What this says about the diagnosis layer

The system classifies every failure by owner — application bug, configuration, infrastructure,
external dependency, data quality, modelling — and that classification worked: the errors were
correct and specific wherever they were *read*. The gap was that nobody read them, because the only
message that reached the operator was four layers downstream of the cause, and the one script that
would have helped could not execute.

So the fix is not a better message; it is a script that walks the chain. `deploy/azure/diagnose.sh`
tests, in order: directory ownership (including files *inside*, which is how this failure looked),
the token's presence, each service with its **restart count** (a crash loop reads as `active` without
it), the local API unauthenticated and authenticated, the tunnel's registration in its own log, the
price history, the disk, and the error count. The first `FAIL` is the thing to fix.

`update.sh` was also wrong for this host: it ran Git as the invoking user, which trips the "dubious
ownership" guard on a repository owned by `crypto`. It now takes root and steps down to `crypto` for
Git and pip, and re-asserts ownership of `data/` and `logs/` on every update — because getting that
wrong is what caused all of this, and it costs nothing to be sure.

### Confirmed working, for the record

The deployment did prove most of the chain. From an empty database: **17,580 bars, 15 requests**;
`401` unauthenticated and `200` authenticated on the local API; `active (running)` on all three
services; the tunnel registered. The engine was sound. The plumbing around it had four holes.

---

## F-043 — `--serve` evaluated once and then served that frozen moment forever ✅

**Date:** 2026-10-10. **Found by:** reading the second deployment log, where every F-042 fix had
landed and the engine was demonstrably healthy — then looking at what `--serve` actually does.

```python
evaluate_once()
uvicorn.run(app, host=..., port=...)      # blocks forever
```

One evaluation, then a web server that never returns. The system would have produced a single set of
suggestions at startup and served **that same evaluation for the whole 72-hour observation run**.

The reason this is worth a finding rather than a one-line fix is how it would have presented. Every
check in `diagnose.sh` passes: both services `active` with zero restarts, the API answers 401 and
200, the database is complete, no errors. The dashboard renders. The only moving part would have
been the freshness envelope's age counter, climbing from seconds to days — which is exactly what
that envelope exists for, and a poor way to discover that a three-day run measured one moment.

**Fixed** by giving uvicorn a worker thread and keeping the evaluation loop on the main thread. That
direction is not arbitrary: the writable `sqlite3` connection is opened on the main thread and may
only be used there, so evaluating on a worker would hit precisely the cross-thread error
`store/reader.py` was written to avoid. Measured after the fix: **6 evaluations in 75 s** at a 12 s
interval, against exactly 1 before.

The loop also survives failures — a classified error and an unclassified crash are both logged and
the loop continues. Stopping would turn a transient data problem into an outage, and an outage is
the thing nobody is watching for.

### The second bug, which the fix exposed

With uvicorn on a worker thread, `/api/health` immediately returned:

```
"store": {"status": "DOWN", "detail": "sqlite error: SQLite objects created in a thread ..."}
```

`AppRunner.health()` queries the **writer's** connection, and the API was calling it from a request.
Worth being precise: **this was not caused by the threading change.** FastAPI runs *sync* handlers in
a worker thread regardless, and `/api/health` is `def`, not `async def` — so the dashboard's health
panel had been reporting the store as `DOWN` with a confusing message all along, while the engine
wrote perfectly well. Moving uvicorn merely made it reproducible on demand.

**Fixed** with `health(include_store=False)`, which the API passes; the reader reports `store:read`
from its own per-thread connection instead. That is the more honest number anyway — the writer's
connection being healthy says nothing about whether a request can read. Verified from a request
thread: `store:read HEALTHY`, with the two known-degraded regime models (F-033) correctly flagged
and the overall status `DEGRADED`, which is true.

---

## F-044 — The Cloudflare tunnel is removed; the API is exposed directly behind its token ✅

**Date:** 2026-10-10. **Reason:** the owner could not get the tunnel working after several attempts
and asked for it gone if a direct connection was possible. It is.

### What changed

`CT_SERVER__HOST=0.0.0.0`, one Azure inbound rule for TCP 8000, and Vercel's `CT_API_BASE` becomes
`http://<vm-ip>:8000`. The tunnel service, its credential file and every reference are deleted.

**The existing bind guard is the entire safety model and it needed no change**: the app already
refuses to start on a non-loopback address without a token (`StartupError`, not a warning). So there
is no configuration in which the port is open and unauthenticated. Verified both ways on a real
network address — `0.0.0.0` with no token refuses to start; with a token, `401` unauthenticated and
`200` authenticated from another host on the LAN.

### What the exposure actually costs, measured rather than asserted

The honest loss is **TLS**: the token now crosses the internet in clear. What that token guards:

| | |
|---|---|
| Read access to public market prices | yes — data CoinDCX publishes to anyone |
| Read access to this system's suggestions | yes |
| Ability to trade | **no.** No such code exists, and `tests/test_normalize_hazards.py` fails the build if an order or wallet path appears in `src/` |
| Any credential | **no.** The CoinDCX fields are empty (F-019) |
| Ability to spend the exchange rate budget | **no**, and this one is worth stating because it is not obvious: **the API makes no outbound calls at all.** It reads only the local SQLite. Polling CoinDCX is the ingest process's job, on its own schedule, entirely independent of any request |

So the realistic worst case of a stolen token is someone reading public prices and a set of
unvalidated suggestions. That is a defensible trade for removing a component the owner could not
operate — an unmaintainable security control is not a security control.

### What was added rather than assumed

Opening a port without noticing who knocks would be careless, so the API now counts refused requests
and logs a **summary** every 50 (`api_requests_refused`, with the top source addresses). One line
per refusal would let a scanner fill the disk, which is a denial of service handed over for free; a
summary makes the distinction that matters visible — background scanning looks like a handful from
many addresses, someone working through tokens looks like a rising count from one.

`diagnose.sh` section 5 now checks `CT_SERVER__HOST`, asks the **kernel** what is actually listening
(`ss -ltn`, the only authority on whether the setting and the running process agree), and reports the
refusal count.

### The upgrade path, documented and not taken

`HOSTING.md` gains an appendix: a free DuckDNS hostname plus Caddy gives real certificates in about
ten minutes, after which 443 is the open port and the engine returns to loopback where only Caddy
reaches it. Recorded because the trade above is reasonable, not ideal, and the owner should be able
to close it without research.

---

## F-045 — With no signals the dashboard could not tell a quiet market from a dead feed ✅

**Date:** 2026-10-10 · **Problem class:** application_bug · **Fixed**

The deployed dashboard showed, all at once:

```
market data age unknown
No suggestions right now
long 0   short 0
The engine did not report any component health
```

Three separate defects, and the first is the dangerous one.

`/api/signals/live` dates its freshness envelope from the newest signal's `as_of_ms`. With no signal
in the database there is nothing to date it from, so `data_as_of_ms` was `null`, so `age_seconds` was
`null`, and the dashboard printed **"market data age unknown"**.

A quiet market is the *normal* state of this system — twelve strategies with conjunctive conditions
fire rarely by design — so "no signals" is the common case, not the edge case. Which means the common
case produced a panel on which a healthy system and a feed that stopped three days ago are
**indistinguishable**: both show no suggestions, both show no age. The age was the only thing that
could have separated them, and it was the thing that was missing.

The fix is a fallback, in order of honesty:

1. the newest signal's `as_of_ms` — what the suggestion was actually computed from;
2. failing that, the newest stored bar's **close** — what the engine would compute from if it ran now;
3. failing that, `null`, because with neither a signal nor a bar there genuinely is nothing to date.

The payload now also carries `freshness_basis` (`"newest signal"` or `"newest stored bar"`), because a
number whose meaning changes silently is worse than two numbers. Step 3 is kept: an invented age would
be worse than no age, which is the whole point of this finding.

The lesson is narrower than "add a fallback". It is that **the empty case is the one to design for
first** when emptiness is the normal state. Every test written before this one inserted a signal.

---

## F-046 — Polling cannot build history, so a wiped database would never have recovered ✅

**Date:** 2026-10-10 · **Problem class:** application_bug · **Fixed**

Asked whether a cleared database rebuilds itself, the answer was no, and the reason was arithmetic.

`poll_candles` requests `refetch_bars` intervals back from the **newest stored bar**, so that the
two-fetch finalization rule can settle recent bars. On an empty database there is no newest bar, so it
requests three bars; next pass, three more. The warm-up needs **2,880 hourly bars**. At three bars a
minute from a standing start it would arrive, in the sense that an hourglass eventually empties.

So `python -m ingest` now runs the bootstrap plan at startup, every time. It is the same idempotent
plan the `bootstrap` command uses — recomputed from the database, so a complete database costs one
query per series and fetches nothing.

Verified end to end, twice:

| Run | Database | Log | Result |
|---|---|---|---|
| 1 | empty | `history_incomplete series=15 missing_bars=17580` → `history_filled requests=15 bars=17580` | ~20 s |
| 2 | filled | `history_complete series=15` | no requests |

**No manual command is needed any more.** Wipe `data/state.db`, restart the service, and recent
history rebuilds and then stays current. A failure is logged and polling continues — refusing to start
because the *historical* fill failed would stop the live data too, and the live data is the half that
cannot be re-fetched later.

---

## F-047 — "The engine did not report any component health" was shown for an engine that was unreachable ✅

**Date:** 2026-10-10 · **Problem class:** application_bug · **Fixed**

The health panel rendered one sentence for three different situations:

| What happened | What the reader needed to know | What they were told |
|---|---|---|
| the reply was not JSON at all (a gateway error page) | the engine cannot be reached | "did not report any component health" |
| the reply had no `components` key | the VM and the dashboard are on different versions | "did not report any component health" |
| the reply had `components: []` | the API was built without its health source | "did not report any component health" |

The first is the one that matters: `res.json()` on an HTML error page throws, the catch returned
`payload: null`, and a null payload read downstream as *the engine answered with nothing*. An
unreachable engine therefore rendered as a calm one — the exact conflation `specs/14` §10 calls the
most dangerous thing this system can do, arriving by a route that had nothing to do with the service
layer that was carefully written to prevent it.

Three changes:

* `lib/api.ts` treats an unparseable body as a **transport failure**, naming the HTTP status, rather
  than as a null payload;
* `app/page.tsx` classifies the health reading before rendering it (`HealthSection`): transport
  failure, engine error, non-OK status with no error body, or a real reading;
* `HealthPanel` gives each of the three nothings its own message and its own remediation.

The general lesson: a `?? []` default is a decision to render absence as emptiness. Every one of them
is a place where "we don't know" can become "there is nothing", and this codebase cares about that
distinction everywhere *except* in the twelve characters it takes to write the default.

---

## F-048 — `payload.data` where the signals were at `payload.data.signals`: every suggestion was silently dropped ✅

**Date:** 2026-10-10 · **Problem class:** application_bug · **Fixed**

`frontend/app/page.tsx` read the signal list as:

```ts
const data: any[] = (sig?.payload as any)?.data ?? [];
```

The engine puts the list at `payload.data.signals`. So `data` was bound to an *object*, typed as an
array by the annotation and never checked. `data.length` on an object is `undefined`, so
`data.length === 0` was false, so the page fell through to `data.map(...)` on something that has no
`.map` — a TypeError during render.

**No suggestion could ever have appeared on the Vercel dashboard.** Not "appeared wrongly": the page
would have thrown the moment the engine returned a signals payload at all. It had never been seen
because the market has been quiet in every window the page has been open, and a quiet market returns
the same object shape with an empty list inside — which also throws, one line later, at
`data.map`. (The screen the owner reported, with "No suggestions right now" and an unknown age, came
from the engine's **own** server-rendered dashboard on the VM, which is Python and has no such bug.
Attributing that screen to this defect would have been wrong, and the distinction is the only reason
the real mechanism got traced.)

What makes this worth a finding is not the typo. It is that:

* `any[]` **invited** it — the annotation asserted a shape instead of checking one, so TypeScript had
  nothing to object to;
* the failure mode was the most plausible-looking screen in the system. A quiet market is normal, so
  the wrong output was indistinguishable from the right one;
* the engine was correct, the proxy was correct, the tests were correct, and the suite was green.

The accessor is now guarded (`Array.isArray(payloadData?.signals)`) and a mismatch renders as a
problem rather than as calm. `tests/test_frontend_contract.py` reads both sides as text and fails if
they diverge — weak, because Node is not installed in this environment and the TypeScript cannot be
compiled here, but the difference between catching it in the suite and catching it by wondering why
nothing ever appears.

---

## F-049 — Spot gaps were permanent: the live poll repaired futures and capped itself on spot ✅

**Date:** 2026-10-10 · **Problem class:** application_bug · **Fixed**

`poll_candles` requests from the newest stored bar to now, which means the live poll is also the gap
repair: after an outage the newest stored bar is old, the window spans the hole, and the venue fills
it on the next pass. No restart, no command.

That worked for futures, which passes no row cap. The spot branch asked for the same window but capped
the rows at `refetch_bars * 4 + 10` — **30 by default** — and spot candles arrive **newest-first**
(F-005). So an outage longer than 30 intervals got the newest 30 rows back and left the hole exactly
where it was. Permanently: `bootstrap` rebuilds **futures only**, so nothing else would ever fetch
those hours, and the hole would sit in the basis family and the spot/futures disagreement check until
someone noticed the features were unavailable and went looking for why.

The cap is now derived from the span actually being requested, bounded by the venue's own ceiling of
1,000, so a wider hole closes over several polls instead of never. Hand-computed in the test: a newest
bar 100 hours old gives a window starting 103 hours back, which is 105 rows with the partial bars at
each end — against the old cap of 22 at the test fixture's `refetch_bars = 3`.

Two things this says beyond the fix. First, a repair mechanism that is a **side effect** of another
mechanism is a repair mechanism nobody tested: the gap filling was real, documented nowhere, and half
broken. Second, the asymmetry survived because futures is the series everything depends on, so the
half that worked was the half that was watched.

---

---

## F-050 — A 404 rendered as a calm, empty market: the dashboard trusted any JSON it was handed ✅

**Date:** 2026-10-10 · **Problem class:** application_bug (triggered by configuration) · **Fixed**

This is the finding that explains the reported screen, and it is the worst kind this project can
produce.

```
market data age unknown
No suggestions right now
long 0   short 0
The engine did not report any component health
```

Every line of that is calm. Nothing on it is an error. And it was produced by a dashboard that had
never reached the engine at all.

The mechanism: `CT_API_BASE` with a path on the end — `http://IP:8000/api` instead of
`http://IP:8000` — makes the proxy request `/api/api/signals/live`. FastAPI answers
`404 {"detail": "Not Found"}`. That body is valid JSON, so the dashboard parsed it happily, and then:

| Check | Result | Why |
|---|---|---|
| `payload.error` | absent | a FastAPI 404 says `detail`, not `error` |
| `envelope.system_status` | absent | there is no envelope |
| `panelState(...)` | **`ready`** | nothing matched, so the default was "fine" |
| `envelope.age_seconds` | `undefined` | → "market data age unknown" |
| `data.signals` | absent | → empty list → "No suggestions right now" |
| `data.components` | absent | → "did not report any component health" |

The service layer was written with exactly this failure in mind — `specs/14` §10, every
`_failed` returning `DOWN` with a reason, `ServiceResult` carrying its own status. All of it was
bypassed, because none of it was running. The dashboard's own default was `{ kind: 'ready' }`, and a
default of "ready" is a decision to describe an unknown system as a working one.

Two fixes, deliberately overlapping:

1. **`panelState` requires an envelope.** Every engine response carries one (`specs/14` §3), so
   its absence now means *this did not come from the engine*, with the remediation naming
   `CT_API_BASE` and the trailing path. The default is no longer "ready" for anything that parses.
2. **The proxy wraps a non-2xx body that is not one of the engine's own errors**, naming the status,
   the path that was requested and the first 200 bytes of what answered. A genuine engine error still
   passes through untouched, so its cause and remediation are not replaced by a guess.

The general lesson is about where a default lives. This codebase is careful to distinguish "no data"
from "no signals" in the one place that produces them — and then threw the distinction away in a
`?? {}` and a fall-through `return { kind: 'ready' }` at the boundary that consumes them. **A
permissive default at the edge undoes every strict guarantee behind it.**

---

## F-051 — `entry -0.04145262148598610125`: a spread rendered as a price ✅

**Date:** 2026-10-11 · **Problem class:** application_bug · **Fixed**

Three C1-C cards on the live dashboard, each marked **LONG**, each showing:

```
entry   -0.04145262148598610125
stop    -0.0451935447161604650
target  0
```

C1-C is the residual relative-value strategy. Its observable is a **beta-adjusted log-return
spread** against BTC, so its levels are small, and **negative for roughly half of all LONG setups** —
a long residual trade is entered precisely when the spread sits below what beta explains. The numbers
are correct. What was wrong is that they were printed under a heading marked *entry*, on a card
otherwise indistinguishable from a tradeable BTC suggestion, and `-0.0414` is not a price that exists
on any exchange at any time.

**The engine had already said so, twice.** The signal's own payload carried:

```python
detail={"levels_are": "residual (spread) units, not price",
        "execution_note": "both legs execute together on trigger; the notionals differ because the
                           hedge is beta-weighted, and the UI must show both legs or the trade will
                           be executed wrong"}
```

Both ignored, for weeks, because **prose in a payload is advice and a field is a contract.** Nothing
could branch on a sentence. So `Levels` gained `price_basis`, C1-C sets `RESIDUAL_BASIS`, and the
dashboard now suppresses the levels row entirely for a non-price basis and renders the **leg table**
instead — which is what someone would actually place.

Suppressed rather than annotated, deliberately. A number under the word *entry* is a price to anyone
skimming, whatever the caveat beneath it says.

A second consequence worth recording: a residual must never be tick-rounded. On BTC's 0.1 tick
(F-054) a residual of −0.0414 would round to −0.1 — a different trade — or to 0, which is not a trade
at all. The basis field is what makes the rounding skip it.

---

## F-052 — "market data 12m ago — too old to act on", on a system whose data was current ✅

**Date:** 2026-10-11 · **Problem class:** application_bug · **Fixed**

One number doing two jobs, and the result was a red warning that is **always on**.

`/api/signals/live` dates its envelope from the newest signal's `as_of_ms`, which is correct — the
envelope documents the age of *the payload*, and the payload is signals. But a setup at an hourly
horizon legitimately stays armed for hours: that is what the strategies are for. So the number grows
with the setup's age and says nothing whatever about whether data is still arriving.

The dashboard labelled it **"market data"** and judged it against the specs/15 §9 thresholds — amber
at 60 s, red at 300 s — which were written for a feed, not for a setup. A perfectly healthy system
with a twelve-minute-old setup therefore reported *"too old to act on"*, permanently.

That is worse than a cosmetic error. A staleness warning that is always lit is a staleness warning
that gets ignored, and the one day it means something is the day it is read as background noise.

The fix separates the two and keeps three ages on screen, each labelled for what it is:

| Shown as | Measured from | Judged against |
|---|---|---|
| market data | the newest **1-minute bar close** | 60 s / 300 s — meaningful, because that series is polled every minute |
| newest suggestion | the newest signal's `as_of_ms` | nothing; a large value is normal and is stated to be normal |
| page read | this browser | nothing |

The thresholds did not change. **The number being judged did**, which was the actual error: F-045
redefined what the envelope's age measures without revisiting what judges it, and the inconsistency
then had a whole deployment to be visible in.

---

## F-053 — A setup whose premise is "we do not predict direction" was displayed as a LONG ✅

**Date:** 2026-10-11 · **Problem class:** application_bug · **Fixed**

Two cards reading `B-BTC_USDT **LONG** C3-C`, with `entry —` and `stop —`.

C3-C arms **pending**: it watches a structural boundary and trades whichever way the boundary
resolves — acceptance with the break, rejection against it. It cannot have an entry or a stop yet,
because the direction decides them. Its `SetupState` is constructed with

```python
direction=Direction.LONG,     # placeholder; the resolution assigns it
```

and the signal carries `detail["pending_direction"] = True` to say exactly that.

The card printed the placeholder, in green, under a heading that means a recommendation. A strategy
whose entire epistemic position is *we cannot predict this* was rendered as a long suggestion — and
two of them, identical on screen, were in fact two different boundaries with nothing on the card to
tell them apart.

It also poisoned the exposure strip, which is the panel specs/15 failure mode 5 exists for. It read
**"long 5, short 4"** and warned that five correlated longs are closer to one leveraged position than
to five opportunities. Of those five: two were pending-direction placeholders with no direction at
all, and two were C1-C spreads, which are **beta-neutral by construction** — the opposite of
concentrated directional risk. The warning was inflated, and inflated by precisely the signals that
add no directional exposure.

Now: pending setups render as **DIRECTION NOT YET DECIDED** with the level they are watching and the
rule that will resolve them; the exposure strip counts directional, spread and pending separately;
and the ENTER NOW banner refuses to print `at —` for a level that does not exist.

The pattern across F-051, F-053 and F-056 is one pattern. **The engine published the right
information and the dashboard dropped it on the floor three times.** Each layer is written carefully
and the boundary between them was not written at all.

---

## F-054 — Every published level was unrounded: `stop 83287.42500000000160` ✅

**Date:** 2026-10-11 · **Problem class:** application_bug · **Fixed**

BTC's tick is **0.1** (verified live via `instrument_meta`: BTC 0.1, ETH 0.01, SOL 0.01). The
dashboard was publishing stops like `83287.42500000000160` and entries like
`83086.139791666666190` — the raw Decimal residue of ATR arithmetic, to twenty significant figures,
none of which can be placed as an order.

`features/convert.py:to_price` implements specs/08 §10's rounding rule, with the direction logic
spelled out and tested. **It had no callers.** The rule was built, tested, and wired to nothing;
`grep` found it in `tests/` and nowhere in the strategy or risk path.

Rounding is now applied in `build_signal`, via `publishable_levels`, and the placement is the part
that needed thinking about:

* **Not at the display boundary.** The risk engine computes `k_net`, the breakeven hit rate and the
  R:R gate *from these numbers*. Round only for display and the reader sees a price that is not the
  one the arithmetic used — so a trade shown as clearing its R:R floor might not, by a tick, silently.
* **Not in the risk engine**, which may veto but never alters what a strategy published.
* **In `build_signal`**, which is the moment a strategy's arithmetic *becomes* a published level, and
  is one place rather than fourteen.

Direction matters and is not cosmetic: a stop rounds **away** from entry and a target **toward** it,
so rounding can only ever make a suggestion more conservative. Rounding to nearest would flatter the
trade by up to half a tick at both ends, for free, in the direction nobody checks.

Three cases decline to round, each for its own reason: a non-price basis (F-051), absent instrument
metadata (invariant 1 — the tick is never assumed, and the risk engine already vetoes such a signal
on `DQ_META`), and `None` levels, which must stay `None` rather than becoming a stop at zero.

---

## F-055 — The 404 diagnosis named the wrong cause first ✅

**Date:** 2026-10-11 · **Problem class:** application_bug (in the diagnosis layer) · **Fixed**

The chart page reported:

> likely cause: nothing is served at /api/candles/B-BTC_USDT — **CT_API_BASE probably includes a
> path**, so the request became a doubled one

`CT_API_BASE` was correct. The real cause was that the VM was running an engine from before
`/api/candles` existed — the suggestions page worked, the chart page did not.

This one is mine twice over, because the discriminator was sitting in plain sight and the message I
wrote for F-050 did not use it:

* a wrong `CT_API_BASE` makes **every** path 404, so nothing on the dashboard works;
* an out-of-date engine 404s only the **new** paths, so some pages work and some do not.

The user could see that some pages worked. The message could have said what that meant and instead
sent them to check a variable they had already checked.

The message now leads with version skew, gives `update.sh` as the remediation, and states the
discriminator explicitly — *"if other pages of this dashboard are working, this is the cause and
CT_API_BASE is fine; a wrong CT_API_BASE would break every page, not one."*

**The lesson is about diagnosis, not about 404s.** F-050 added a diagnostic message and I ranked its
causes by the one I had just debugged rather than by the evidence available at the point of failure.
A message that names a plausible cause confidently is worse than one that names two and says how to
tell them apart, because the reader stops at the first.

---

## F-056 — `signal.detail` was `undefined` on the dashboard, and every display rule read it ✅

**Date:** 2026-10-11 · **Problem class:** application_bug · **Fixed**

`_row_to_signal` returned the stored payload under `payload`, so `detail` was reachable only as
`signal.payload.detail`. `render.py` reads it correctly. `lib/honesty.ts` read `signal.detail`.

So **every rule in that file was branching on `undefined`**:

| Rule | What it read | What it did |
|---|---|---|
| `displayable` | `detail.control`, `detail.tradeable` | the frontend's control-strategy firewall — the third of three layers — never fired. Nothing leaked, because the other two work |
| `validationNotice` | `detail.validated` | returned "never tested against history" because `undefined !== true`. The **right answer for the wrong reason**, and it would have begun lying the moment anything was validated |
| `confidenceCopy` | `detail.breakeven_hit_rate` | fell through to the column of the same name, so it worked by luck |

Both new fields from this session — `price_basis` and `pending_direction` — would have been read as
`undefined` too, which means F-051 and F-053 would have been "fixed" and still broken, and the tests
that assert them are text-matching tests that would not have caught it.

`detail` is now lifted to the top level by the engine, and the frontend reads it through a single
`signalDetail()` with a `payload.detail` fallback — the fallback because the VM and Vercel deploy
separately, which is the same version skew that caused F-055 three hours earlier.

**Three accessor bugs in one session** (F-048, F-056, and F-050's missing envelope), all of the same
shape: a reader assuming a payload's shape instead of checking it, in a language where `undefined`
propagates silently through every comparison. Node is not installed on this machine so the
TypeScript cannot be compiled, and `any` types mean compiling would not have caught these anyway.
What catches them is reading the producer and the consumer side by side, which is now what
`tests/test_frontend_contract.py` does for each accessor by name.

---

---

## F-057 — `.gitignore` silently excluded the Data page, so the commit that added it added everything except it ✅

**Date:** 2026-10-11 · **Problem class:** configuration (mine) · **Fixed**

The Data page returned a Vercel 404. The file existed, the tests read it, the nav linked it, and it
had **never left the laptop.**

`.gitignore` line 8 was:

```
data/
```

A bare `data/` matches a directory named `data` at *any* depth. The holdout lives at `data/` in the
repository root, which is what the rule is for — but it also matched `frontend/app/data/`, the
dashboard route called "data".

```
$ git check-ignore -v frontend/app/data/page.tsx
.gitignore:8:data/   frontend/app/data/page.tsx
```

So `git add -A` skipped it, the commit succeeded, the push succeeded, Vercel built successfully, and
**nothing anywhere reported a problem** — because an ignored file is not an error. The only
symptom was a 404 on a route whose source was sitting on the development machine.

Fixed by anchoring the rule to the repository root: `/data/` and `/logs/`. The holdout is still
ignored (verified: `data/state.db`, `data/raw/`, `data/backfill/`, `logs/`, `.env` all still match),
and `frontend/app/data/` no longer is.

`tests/test_frontend_contract.py` now runs `git check-ignore` over every file under `src/`, `tests/`
and the dashboard's own source and fails if any of them cannot be committed. Confirmed to catch the
original bug by restoring the old pattern and watching it fail.

**The lesson is about which failures are silent.** This project is careful about failures that
*report* wrongly — a stale number, a vetoed signal, an empty panel. This one reported nothing at
all, at four separate stages that each had every opportunity to say something. The next person to
add a route called `data`, `logs`, `build` or `dist` would have hit it too, which is why the guard is
a test and not a comment.

---

## F-058 — The display fixes would have looked deployed and not been ✅

**Date:** 2026-10-11 · **Problem class:** application_bug · **Fixed**

Noticed while tracing F-059, before it could cost anything.

F-051 and F-053 were fixed by having the engine publish `price_basis` and `pending_direction`, and
the dashboard branch on them. Correct, and incomplete: **the dashboard and the engine deploy
separately**, so for as long as the VM lags, neither field exists in the payload, both branches take
their default, and a C1-C residual is rendered under the word *entry* exactly as before.

The same gap persists *after* an upgrade, for a different reason: the `signals` table still holds
setups armed by the previous version, and those rows will never gain the new fields.

Both dashboards now fall back to what an older engine already published:

| Decision | Preferred | Fallbacks already in old payloads |
|---|---|---|
| are these levels prices? | `price_basis` | `two_leg`, `levels_are` containing "residual" |
| is the direction decided? | `pending_direction` | `pending` in the setup context; two `entry_triggers` with no entry |

The fallbacks are not defensive padding; they are the case that actually occurs. A fix whose
correctness depends on both halves of a two-sided deployment being simultaneous is a fix that is
wrong for the whole period anyone is watching it.

---

## F-059 — Two sessions of deployment debugging because the engine code was never pushed ✅

**Date:** 2026-10-11 · **Problem class:** configuration (process) · **Fixed**

The chart page 404'd. `diagnose.sh` reported **OK on all eight sections**. The owner ran it twice,
checked the environment variables, confirmed the services, confirmed the data, and the page stayed
broken.

The reason, found by looking at git rather than at the VM:

```
$ git log --oneline -3
322fa30 data1
e408b5d chart 1
...
$ git show --stat 322fa30     # 8 files, all under frontend/
$ git show HEAD:src/api/app.py | grep -c "api/candles"
0
```

**None of the backend work was in GitHub.** Two commits had been made and both contained only
frontend files; every `src/` change from two sessions — `/api/candles`, `/api/sources`, tick
rounding, `price_basis`, the freshness split, the ingest self-bootstrap, the lifted `detail` —
existed only as uncommitted modifications on the laptop. `src/snapshot/request.py` was untracked
entirely, so even a partial push would have broken the import on the VM.

So `update.sh` was doing exactly what it was asked: fetching a branch whose newest commit did not
contain the route, reporting "already up to date", and restarting the same code. **My instruction to
run it was correct and useless**, and I gave it twice without once checking whether the code it
pulls contains the fix.

Three changes, because the failure had three places it could have been caught:

1. **`diagnose.sh` now asks the running engine which routes it serves** (section 4b) and prints the
   commit it is running with its distance from the remote (4c). Every other section asks *is the
   engine working*; none asked *is the engine the version the dashboard expects*, and that is the
   question when one page 404s while the rest looks perfect.
2. **`update.sh` no longer lets "already up to date" read as success.** It says that this means
   nothing new has been *pushed*, not that the VM is current, and after restarting it probes the
   dashboard's routes and names any that 404.
3. **`BUG_SOLVE.md` gives the push procedure**, with `git status` as a verification step rather than
   a formality. Previous rounds told the owner how to deploy and never how to commit, which assumed
   a step that was not happening.

The general lesson: **a two-sided deployment drifts apart by default, and neither side can see the
other's version.** Every symptom of that drift looks like a configuration problem on the side you
happen to be looking at. The fix is for each side to be able to state its own version and for one of
them to check the other's — not more careful instructions.

---

---

## F-060 — The regime finding was correct; aggregating it as a fault hid every real one ✅

**Date:** 2026-10-11 · **Problem class:** application_bug · **Fixed**

Reported symptom: the dashboard shows `regime:B-BTC_USDT — DEGRADED — modelling` and
`regime:B-SOL_USDT — DEGRADED — modelling`, permanently.

### The state is correct and must not be falsified

F-033 measured it on 4,001 stored 1h bars per asset: at 2×2, **BTC and SOL fail the persistence
test with a median dwell of 2.0 bars against a requirement of 3**, while ETH passes. Occupancy and
separation pass on all three. `specs/12` §10 prescribes exactly what follows — drop the
dwell-stability conditions, keep every raw-axis threshold, run with reduced selectivity — and
`specs/02` §6 is explicit that "a label that flips every bar is not a regime, it is a noisy
indicator with a fancy name".

So the model really is degraded, on those two assets, permanently, and the classifier reporting
`DEGRADED` with `ProblemClass.MODELLING` is the system working. **Nothing here was softened to clear
the warning**, and a test now asserts the classifier still reports it.

### What was actually broken

`HealthReport.status` is `worst_of` over every component, and the envelope took that. One permanent
model finding therefore made the **entire system** permanently `DEGRADED`:

| | Before | After |
|---|---|---|
| envelope `system_status` | DEGRADED, always | HEALTHY when nothing is broken |
| dashboard health count | "3 healthy, 2 not healthy" | "3 healthy, no faults" + a findings panel |
| server dashboard | permanent amber DEGRADED banner | a distinct "REDUCED SELECTIVITY" notice |
| **a genuinely failing poller** | **a third identical DEGRADED line** | the only line in the fault list |

The last row is the defect. The label was cosmetic; the consequence was that a real degradation
became invisible, because it looked exactly like the two that had been there for weeks. A warning
that is always on is a warning nobody reads — the same failure as F-052, arriving by a different
route.

`ProblemClass.MODELLING` was created to draw precisely this line, and its own docstring says so:
*"the triage answer is 'read the finding', not 'check the feed' or 'fix the code': paging someone
would waste the page."* The distinction was declared in `core/errors.py` and discarded one layer
above, in `core/health.py`.

### The change

* `HealthReport.operational_status` — worst status among components that are **not** model
  findings. `status` is unchanged and still reports everything.
* `HealthReport.faults()`, `modelling_components`, `modelling_findings`; `degraded_reasons` is now
  faults only.
* `to_dict()` publishes `operational_status`, `fault_count` and `modelling_findings` alongside the
  unchanged `status` and component list.
* The envelope carries `operational_status` and a `modelling_findings` list — so the finding
  travels with **every** response, not just `/api/health`.
* `HealthService.health()` was overriding the envelope with `report.status`; it now passes
  `operational_status`.
* Both dashboards render findings in a panel of their own, naming the asset and the failing test.
  `specs/12` §10 requires the degradation visible ("a user should be able to see the system is
  running with a weaker filter set"); it does not require it to look like something broke.

Verified both ways: findings alone → envelope HEALTHY, no fault banner, finding panel shown;
findings **plus** a failing poller → envelope DEGRADED, the poller named as the single fault, the
findings still shown.

---

## F-061 — C1-C's negative values are correct, and nothing was testing the one thing that could invert them ✅

**Date:** 2026-10-11 · **Problem class:** none (verified correct) · **Tests added**

Traced the full path for the reported `entry -0.04145262148598610125 / stop -0.0451935447161604650 /
target 0` on a **LONG** C1-C card.

`specs/03` §5 defines C1-C on the **residual** — a beta-adjusted log-return spread against BTC
— not on a price:

| Field | Spec | Observed | Verdict |
|---|---|---|---|
| entry | "ACT applied to the **spread series**, U = rolling σ of the residual" | −0.0414 | correct: a residual, not a price |
| stop | "residual extends beyond `X* ± s_rs·σ`" | −0.0452 | correct: below entry for a LONG |
| target | "residual returns to its rolling mean, **zero by construction**" | 0 | correct |
| direction | "**long the underperformer**, short the outperformer" | LONG at z < 0 | correct |

A negative residual means the asset returned *less* than its beta against BTC explains — it is the
underperformer — so the correction trade is to buy it. The ordering `stop < entry < target = 0` is
therefore exactly right, and all three terms are negative except the target. **These are expected
signed values under the canonical definition, not a sign error, not a unit mismatch, and not a
rounding artifact.** The defect was that the dashboard printed them under a heading marked *entry*
(F-051), which is fixed.

What the investigation did find is that **nothing tested the sign convention.** It lives in one
character — `direction = Direction.LONG if z < 0 else Direction.SHORT` — and inverting it would
make the system short every dislocated-cheap asset indefinitely, with no error raised, no validity
gate tripped, and every published number still looking entirely plausible. Five tests now pin it:
the residual is `own − beta·bench` (hand-computed), beta scales the benchmark leg only, a negative
residual goes long, the level ordering holds, and degenerate windows raise instead of publishing a
sign.

---


---

## F-062 — Every restart abandoned every armed setup, and the code to prevent it had no callers ✅

**Date:** 2026-10-11 · **Problem class:** application_bug · **Fixed**

`AuditTrail.open_setups()` existed. Its docstring read *"armed setups, for a restart to resume from
(specs/13 §11.1)"*. It had **no callers**, and `AppRunner.states` is `field(default_factory=dict)`.

So every start of `crypto-app` began with no armed setups. Every setup that had been
`SETUP_DETECTED`, `ENTER_NOW` or `TRIGGERED` when the process stopped was silently abandoned: its
row stayed in the `signals` table in an active state **permanently**, because the only thing that
could have moved it — `strategy/lifecycle.py:progress()` — needs a `SetupState` that no longer
existed. It could never reach `TARGET_HIT`, `STOPPED` or `EXPIRED`. The dashboard went on listing it
as a live suggestion indefinitely.

This is the **third** instance of one pattern: code written, tested in isolation, never wired to a
caller. F-043 was the serve loop that evaluated once. F-054 was `to_price`, which implemented
specs/08 §10's rounding rule and was called by nothing but its own tests. All three were invisible
for the same reason — **the thing that should have failed simply never ran**, so no test failed, no
error was logged, and the symptom appeared somewhere far away as data that looked stale. There is
now a test asserting the caller exists, for each.

### The second defect, found while fixing the first

`setup_state` could not support a resume even once something read it. The table had no `context`
column and no `triggered_at_ms`, and `_write_setup_state` dropped both.

`SetupState.context` is where a strategy keeps the facts only it knows — its own docstring calls
them "needed to re-evaluate invalidation": C2's monotone `trail_level` and `extreme_since_entry`,
C3-C's `inside_is_below` (which side price approached the boundary from), C3-C's `breaks_seen`.
Resuming without them would have **reset a trailing stop to its starting width** — loosening a stop
the spec requires never to loosen — and **inverted C3-C's acceptance test**. Both silently, both
producing a plausible suggestion computed from facts that had been thrown away.

So the fix needed a migration before it could need a caller. Migration 3 adds `context` and
`triggered_at_ms` (additive, nullable, no backfill) plus two indexes. Applied to the 6.2 GB
development database in **0.01 s** with 21.9 M bars intact, and verified to upgrade a populated v2
database without touching its rows.

### What resumes, and what deliberately does not

specs/13 §11.1 requires each persisted setup to be re-evaluated rather than trusted. `app/resume.py`
rehydrates state — with the **original `armed_at_ms`**, so `X*` spans the right window and expiry
counts from the right moment — and hands it to the existing evaluation path. It adds no second
evaluator: a resumed setup is managed by exactly the same code as one armed a minute ago, which is
the only way live and backtest can agree.

| Refused when | Why |
|---|---|
| past expiry | specs/13 §11.1 step 4 |
| `parameter_hash` differs | not the setup those parameters would arm now; mixing two parameter sets in one signal's provenance makes the audit record unreplayable |
| `strategy_version` differs | same reasoning |
| `context` is SQL NULL | written before migration 3. The facts were never saved, and **declining is conservative while guessing is not** |
| strategy no longer registered | nothing to evaluate it |
| a second open setup for one `(strategy, asset)` | the runtime holds one by design (`specs/04` §6); the newest wins and the loser is named in the log |

A refused setup is **not transitioned** by the loader. Inventing a terminal state for a setup this
process never observed would be rewriting history it did not witness — and specs/13 §11.2 makes the
same point about the adjacent case, requiring a trigger that fired during an outage to be `MISSED`,
never `TRIGGERED`, "because the user had no opportunity to act". A test asserts the loader contains
no `transition()` call at all.

### And a trap in the migration applier

Migration 3 would not apply: `near "without": syntax error`. The applier split statements on
`sql.split(";")`, and a semicolon **inside a `--` comment** cut a statement in half. Migrations are
forward-only and immutable once released, so they carry long explanatory comments; the next author
to write one with a semicolon in it would have debugged a syntax error pointing at a fragment of
English prose. The splitter now strips line comments first, verified to produce identical statement
lists for both released migrations.

---

## F-063 — The "legacy display date" would overwrite good data, because there is no legacy gap

**Date:** 2026-10-11 · **Problem class:** none (requirement conflict) · **Not applied — awaiting a decision**

Phase 2E of the request asks that legacy suggestions be stamped with a display date of **10 October
2026, 23:11**, on the assumption that older records lack a usable timestamp.

Checked before building it, and the assumption does not hold:

* `signals.created_at_ms` is **`INTEGER NOT NULL`** and has been since migration 2 — the schema that
  created the table. There has never been a signal row without one.
* It is written as the wall-clock time of the first insert, and it is **deliberately absent** from
  `_write_signal`'s `ON CONFLICT DO UPDATE SET` list, so it does not move when a later outcome
  updates the row. It is already the stable issue timestamp Phase 2B asks for.
* `SELECT COUNT(*) FROM signals WHERE created_at_ms IS NULL OR created_at_ms = 0` returns **0**.

So there is no missing-timestamp problem to repair. Writing 10 Oct 23:11 over those values would
replace accurate data with a fabricated constant, which the request's own constraints forbid in four
places — "do not silently alter historical records", "preserve the underlying original data wherever
possible", "do not fabricate results", "legacy suggestions retain their existing rules and
historical values".

**Not applied, and not skipped silently.** The capability is the easy part; the question is whether
it should run. Three options, with the trade-off in each:

1. **Do nothing.** Legacy suggestions display their real issue time. Nothing is lost, and the
   request's literal instruction is not followed.
2. **A display-only compatibility column.** Add nullable `legacy_display_ms`, set it only for rows
   created before a recorded cutoff, publish both, and label it in the UI as a legacy display date
   with the true timestamp beside it. Non-destructive, auditable, reversible — but it still shows a
   reader a date that is not when the suggestion was issued.
3. **Overwrite `created_at_ms`.** Follows the instruction exactly and destroys the real timestamps.
   Refused: it is irreversible, and it would silently move historical suggestions between date
   groups.

Option 2 is the one the request itself points at ("the safest minimal database migration or
compatibility-layer approach"), and it is what will be built if the owner confirms. The owner may
also have meant something this finding has not considered — a *presentational* grouping for records
from before the dashboard existed, say — which is the other reason to ask rather than guess. The
development database holds **zero** signals, so the only records this would touch are on the VM,
where the selection criteria cannot be verified from here either.

---

## F-064 — "Target hit versus stopped" is not a partition, and treating it as one understates both rates ✅

**Date:** 2026-10-11 · **Problem class:** none (design decision, recorded before it could become a defect)

Phase 3 asked for target-hit and stop-loss counts and rates. The obvious implementation is
`TARGET_HIT / (TARGET_HIT + STOPPED)` and stop there. Checked against the lifecycle first, and the
obvious implementation would have been quietly wrong.

`specs/07` §8 defines **eleven** states. Seven are terminal, and **only two of them are a trade
result**:

| Bucket | States | What it means |
|---|---|---|
| open | `SETUP_DETECTED`, `ENTER_NOW`, `TRIGGERED` | still being tracked; no outcome yet |
| **resolved** | `TARGET_HIT`, `STOPPED` | the only two that are a trade result |
| ended, no result | `EXPIRED`, `INVALIDATED`, `SUPPRESSED`, `MISSED`, `HANDED_OFF` | genuine outcomes in which no trade result exists |

On the twelve-row verification fixture those five are **five of twelve suggestions** — more than the
two that resolved. Two ways to get this wrong, and they fail in opposite directions:

* **Put them in the denominator** (`TARGET_HIT / all terminal`) and the win rate reads 3/10 = 30%
  instead of 3/5 = 60%, understating every strategy because most suggestions never produce a result.
* **Drop them entirely** and the page hides most of what the system did — including the case that
  matters operationally, which is everything being `SUPPRESSED` by arbitration or `MISSED` during an
  outage.

So each is counted, named and reported in its own bucket, and the page shows the buckets adding up to
the total so none can be silently dropped from a denominator without the arithmetic visibly failing.

### Five rates, each publishing its own denominator

"The hit rate" is ambiguous and both readings are legitimate, so neither is called "the" rate:

| Rate | On the fixture | Reading |
|---|---|---|
| `target_hit_of_resolved` | 3/5 = **60%** | of the suggestions that reached a trade result, how many worked |
| `target_hit_of_all` | 3/12 = **25%** | of everything issued, how much worked |
| `resolved_of_all` | 5/12 = 42% | how much of the output is measurable at all |
| `triggered_of_all` | 6/12 = 50% | how often a hypothetical entry actually occurred |

Every rate travels with its numerator, denominator and a prose definition, so any number on the page
can be checked against a direct query without trusting the service. **A zero denominator yields
`None`, never `0.0`** — `0%` reads as "nothing ever hit", which is a claim about the market, where
"not defined" is a claim about the sample and is the true one.

`INVALIDATED` is deliberately **excluded** from the triggered count: it is reachable from
`SETUP_DETECTED` as well as from `TRIGGERED`, and the current state alone cannot say which, so
counting it would inflate that denominator by an unknown amount.

### Verification

Every figure cross-checked against a direct `COUNT(*)` over the same fixture rather than against the
service's own output — 32 tests, including the midnight-boundary case (a suggestion at exactly
00:00:00.000 UTC must be counted on one day, or two single-day windows total more than the two-day
window containing them).

No joins anywhere in the service: a join to `signal_events` to pick up a transition time would
multiply rows per signal and inflate every count, which is the classic way an analytics page starts
lying. Demonstrated by inserting four events for one signal and asserting the counts do not move.

### What the page refuses to imply

Nothing in this system has been validated (`specs/09`), outcomes are hypothetical — `TRIGGERED`
tracks a trade nobody was required to take — and no cost or slippage is deducted. The engine
**generates** the limitations list from the data, so the small-sample warning appears because the
sample is small rather than because someone remembered to write it down, and the page renders it
**above** the figures rather than in a footnote. A number on a dashboard acquires authority the
number itself has not earned.

---

## F-065 — Three of the four model filters matched nothing, because the class was never written ✅

**Date:** 2026-10-11 · **Problem class:** application_bug · **Fixed**

Reported symptom, and it is a good one because it is *arithmetically impossible* on its face:

```
model:  All   Correction 0   Persistence 0   Transition 0   Established 13
```

...above a list showing more than thirteen suggestions. The chips could not be reconciled against
the list, which is the sort of discrepancy that means one of the two numbers is lying.

### The cause: a default that looks like a value

`audit/trail.py:_write_signal` read the classification out of the signal's payload:

```python
strategy_class = int(raw.detail.get("strategy_class", 0) or 0)
```

and `strategy/evaluation.py:build_signal` **never put it there.** It published `tier`,
`price_basis`, `validated` and `expansion_seeking`, and not the class.

So the column was written `0` for every signal — *except* for the C4 strategies, which happen to
pass `"strategy_class": 4` in their own `detail=` kwarg (six call sites in `c4_established.py`).
Hence exactly the reported screen: class 4 correct, classes 1–3 stored as 0.

`signals.strategy_class` is what the dashboard's model filter keys on (`WHERE strategy_class = ?`),
so three of the four filters matched **nothing**, their counts read **zero**, and the suggestions
they should have matched were counted under a `"0"` key that the frontend never rendered.

**Nothing raised, at any layer.** The column is `NOT NULL` and `0` satisfies it; the filter returned
an empty set, which is a legitimate answer; the chip showed a legitimate count. A default that is a
*valid-looking value* for a column other code filters on is a bug with no symptom at the point it
happens — it surfaces somewhere else, as arithmetic that does not add up.

### Three fixes, because it needed three

1. **`build_signal` publishes `int(strategy.strategy_class)`** — the strategy object's own
   declaration, which is the authority. Every new signal is now classified correctly.
2. **The writer falls back to the registry, not to zero.** `strategy_class_of()` in
   `strategy/registry.py` is the single implementation: it prefers the registered strategy's
   declared class and falls back to the `C<n>-` prefix for an id this build no longer registers.
   Only a genuinely unrecognisable id stores `0`, and that logs `signal_class_unknown` with a
   remediation.
3. **Migration 4 repairs the rows already written.** A repair, not a reinterpretation: the correct
   value is derivable with certainty from the `strategy_id` in the same row, using the same mapping
   the registry has always had. It introduces no new rule and changes no outcome, status, price or
   timestamp — one column, scoped to rows whose class is not already 1–4 *and* whose id matches
   `C[1-4]-*`. A row matching neither is left exactly as it is rather than guessed at.

Verified on a fixture reproducing the bug exactly: `C1-B`, `C2-A`, `C3-C` stored as 0 become 1, 2, 3;
a `C4-B` already at 4 is untouched; a `LEGACY-X` row stays at 0; states and `created_at_ms` are
unchanged on all five; re-running is idempotent. Applied to the development database in 0.003 s.

### The display half, which is why it was unexplainable

The engine's `_group_by_model` *did* count the orphans, under a `"0"` key. The frontend's
`modelList()` iterated only the four published models, so that bucket was silently dropped — the
count existed in the payload and appeared nowhere on screen.

Both ends now surface it: an unclassified bucket renders as its own chip, and the page prints a red
line saying the counts will not add up to the list and that the VM probably needs updating. **A
count that appears nowhere cannot be reconciled**, and the reader was left to discover the
discrepancy by adding up the chips themselves.

### What let it ship

Every existing test inserted signals **by hand with an explicit `strategy_class`**, so none of them
exercised the write path that was defaulting it. The new
`test_a_signal_written_through_the_audit_trail_carries_its_real_class` goes through the real
`_write_signal` with no class in the detail — the exact shape the bug produced — and asserts all
four land correctly.

---

## F-066 — The live suggestions showed no issue time; the history view did ✅

**Date:** 2026-10-11 · **Problem class:** application_bug (omission) · **Fixed**

`HistoryRow` displayed `created_at_ms` as "issued". `SignalCard` — the live card — displayed no
timestamp at all.

So the **most time-sensitive view was the only one that could not answer "how old is this?"** And
being on screen says nothing about age here: a setup at an hourly horizon legitimately stays live
for hours by design, which is the whole point of F-052's separation of market-data age from
suggestion age.

Nothing was missing from the data. `created_at_ms` has been written since the `signals` table was
created and is deliberately excluded from `_write_signal`'s `ON CONFLICT DO UPDATE SET` list, so it
is stable (F-063 established this while investigating the legacy-date request). **The value was
there the whole time and the live card simply never rendered it** — so every existing suggestion
displays its true creation time the moment the dashboard is redeployed, with no waiting for new
signals.

The card now shows two timestamps when they differ, because they answer different questions:

| Shown as | Column | Means |
|---|---|---|
| suggested | `created_at_ms` | when it was first issued — never moves |
| state changed | `state_since_ms` | when the lifecycle last moved (detected → armed → triggered) |

One number for both would make a setup detected yesterday and triggered an hour ago read as an
hour-old suggestion. Rendered in UTC with the zone named, matching the date grouping (D-55).
## Open questions


| # | Question | Blocking | Decision it leaves open |
|---|---|---|---|
| Q1 | ~~Should CLAUDE.md invariant #3 be rewritten?~~ **Resolved 2026-09-19.** The owner approved the change: "`skw` is not a health flag; judge freshness by `btST`/`bmST` age only." CLAUDE.md is updated. | — | — |
| Q2 | What does `skw` actually mean? | Needs longer observation (quality.jsonl `skw_min`/`skw_max`) | Whether it is usable as a data-quality metric |
| Q3 | ~~Disk growth ~270 MB/month~~ **Resolved 2026-09-19.** The owner accepted it and does not want polling reduced. Completed day files are now gzip-compressed losslessly (sha256-verified); the measured effect is in F-023. | — | — |
| Q4 | ~~Positioning (S2) is wired but disabled.~~ **Resolved 2026-09-19.** The endpoint is public (F-019), S2 is enabled, and history has been accumulating since 11:00:45 UTC. Only P8 part B (signed vs unsigned comparison) waits for `.env`. | — | — |
| Q5 | **CLAUDE.md invariant 12** says `data/stats` needs authentication. F-019 shows it does not. Proposed wording: "Only `conversions` is documented as signed (unused). `data/stats` is public: call it unauthenticated. Keep signed-request code isolated in `cdx_http/signed.py`." | Owner decision | Whether S2 should stay unauthenticated (current default) or be switched to `auth = "signed"` once a key exists |
| Q6 | Futures also serve 15/30/240/480/3D/1M (F-008). S7 gap-fill records only the spec set (1/5/60/1D). Adding 15/30/240/480 costs +12 requests/hour. | Owner decision (spec expansion) | Whether native futures 4h is recorded continuously, or only via the backfill |
| Q7 | ~~`specs/02` Family 4 variance ratio divides by `k` twice (F-031).~~ **RESOLVED 2026-10-03.** The owner confirmed the corrected standard Lo–MacKinlay form and the spec is amended at `specs/02` §5 Family 4 with the measurements that justified it. Tracked briefly as Q-J in `latest-updates.md`; that id is merged into this one. | — | — |
| Q8 | **Raise the recorder's spot book depth from 50 to 100?** F-032 measured 100 levels working and reaching ±1.79 bps versus ±1.01 bps at 50, for one extra request per poll. Futures cannot be improved (>50 returns an empty book). | Nothing; it changes the recorded stream's shape going forward | A recording-policy decision about the irreplaceable dataset |
| Q9 | ~~Which `k` does a bare `MR_VR(1h)` mean?~~ **RESOLVED 2026-10-03.** The owner confirmed **k = 2** as canonical and the spec now states it explicitly; `MR_VR_K4`/`MR_VR_K8` must be named where wanted. | — | — |
| Q10 | ~~Confirm `risk_per_trade` and `portfolio_vol_target`.~~ **RESOLVED 2026-10-03.** The owner confirmed **`risk_per_trade = 0.5%`** and **`portfolio_vol_target = 15%` annualised**. Both remain `status = "user"` in the registry: they are risk appetite, not market properties, so they are never swept (`specs/08` §11). | — | — |
| Q11 | **`lookback_1h_short = 24` cannot host swings at the horizon's own scale.** F-038 measured the conflict: pivot availability on a 24-bar window caps θ at ~1.0, but the median swing spacing only reaches the derived 12-bar target around θ ≈ 2.0, where availability is 0.09. With `k = 3` confirmation, 24 bars is too short for 12-bar swings, so C2-A's structural stop necessarily references smaller swings than its own premise describes. Options: (a) accept it — θ = 0.75 is the availability-respecting end, and this is why θ is `provisional`; (b) read `STR_SWING` at `1h/medium` for the structural stop instead of `1h/short`, which costs no parameter and whose spacing does respond to θ; (c) change `lookback_1h_short`, which alters **every** 1h feature and the regime axes with it. | Nothing — (a) is in force and the system runs | Whether C2-A's structural stop should read the medium window, and whether the shared short lookback is right for structure at all |
