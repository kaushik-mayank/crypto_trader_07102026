# latest-updates.md — Implementation State & Master Plan

**Durable source of truth for implementation progress.** A new session should read `CLAUDE.md`,
`README.md`, then this file, and be able to continue without any conversational context.

| | |
|---|---|
| **Last updated** | 2026-10-03 (all 14 strategies implemented; θ calibrated; backtest harness built; the ingest process and its finalization sweep landed) |
| **Overall status** | **The system runs end to end locally and all 14 strategies are implemented.** `python -m app --once` evaluates 12 tradeable strategies across 3 assets in ~820 ms and writes the audit trail; `python -m ingest` keeps SQLite current; `python -m backtest` has a pre-registered protocol and a complete fill/accounting/metrics stack. **No backtest has produced results yet**, so nothing is validated and every signal still carries `validated: false` and the 0.4 multiplier. |
| **Current phase** | **Hosting**, by owner instruction on 2026-10-04: the system goes to Azure in suggestion-only mode for a 72-hour observation run while the backtest's data problems are fixed in parallel. `PHASE-26` is complete and the protocol is locked; `PHASE-27` (harness integrity, a halt gate) is next for validation and is independent of hosting |
| **Next action** | See [Next Action](#next-action). |
| **Deployment target** | **Azure VM** (engine; API on port 8000 behind its token) + **Vercel** (dashboard, server-side proxy). The Cloudflare tunnel was removed on 2026-10-10 (F-044) — the owner could not operate it, and an unmaintainable security control is not one. GitHub is the source of truth and the VM pulls from it. Step-by-step: **[`HOSTING.md`](HOSTING.md)**; recovery: **[`BUG_SOLVE.md`](BUG_SOLVE.md)**. |
| **Blocking gaps** | **None for implementation.** Three things limit *output*: the regime model is **degraded on BTC and SOL** (F-033), so their dwell-stability conditions are dropped; **Tier B has zero history**, so C1-A and C2-C cannot be backtested at all (specs/09 §13); and nothing has been backtested, so every signal carries `validated: false`. `swing_atr_multiple` is no longer among them — it was calibrated structurally at 0.75 (F-038) and the registry has **zero `unset` parameters**. |
| **Quality gates** | **737 tests passing** · `ruff` clean · `mypy` clean across 133 source files |

---

## 1. What exists today (verified by inspection, 2026-09-27)

### Code — 88 tests passing, ruff clean, mypy clean on new code

| Module | Contents | Spec |
|---|---|---|
| `src/cdx_http/` | `client.py` (per-host adaptive token bucket, backoff, raw capture with header redaction, single-attempt + retry APIs), `backoff.py` (429/503/5xx/transport schedules), `ratelimit.py`, `jsonutil.py` (Decimal parse + **verbatim raw-slice** helpers), `signed.py` (**the only module touching credentials**) | `01` §0 |
| `src/recorder/` | `__main__.py`, `config.py` (TOML), `storage.py` (append-only JSONL, day files, torn-line isolation, **verified gzip of completed days**, single-instance lock, STOP file), `tasks.py` (S1–S7), `runner.py` (scheduler, per-task backoff, gap detection, health, hourly quality rollups) | `01b` |
| `src/probes/` | P1–P17 + `recorder_analysis` (P13–P16), `reliability_outage`, `storage_check`, `p14_book_sampler`, `common.py` | `01` |
| `src/backfill/` | Full historical candle pull → Parquet (`decimal128(38,18)`) + verbatim raw pages + `--verify` | `01b` |
| `config/parameters.toml` + `src/config/` | **THE 30-parameter registry** (cap enforced, every entry justified, `parameter_hash` for signal provenance) and `Settings` (operational config, strictly separate from model parameters, Azure-aware) | `02` §10, `08` §11, `10` §12 |
| `validation/trial_count.json` | Append-only cumulative trial counter, seeded at 0 | `19` §2 |
| `tests/` | 88 tests across 6 files; 8 real captured fixtures + README | — |
| `docs/findings.md` | **28 findings (F-001…F-028)**, incl. 4 spec corrections | working rules |

### Data on disk

| Path | State |
|---|---|
| `data/backfill/` | **1.3 GB, 42 series** (3 assets × {spot 4 res, futures 10 res}), verified: sorted, unique, ≥99.7% complete after each series' continuous start (F-018) |
| `data/raw/` | Recorder JSONL. Recording windows 2026-09-19 → 09-23 (12.1 h uptime of 103 h) and 09-27 → now. Compression verified (F-028) |
| `data/probe_results/` | Every probe's JSON result |
| `.env` | Exists, **both values empty** (nothing in the project needs a key — F-019) |

### Phase 1 acceptance status (`specs/01b`)

| # | Criterion | Verdict |
|---|---|---|
| 1 | Runs unattended ≥72 h | ❌ **NOT MET** — 12.1 h uptime of a 103 h window; laptop slept/off (F-024) |
| 2 | Survives `kill -9` mid-write | ✅ PASS (F-006, repeated 09-19 and 09-23) |
| 3 | Survives a 10-min network outage | ✅ PASS (F-020) |
| 4 | Survives laptop sleep/wake | ✅ PASS — 184 sleep gaps, every one recorded and recovered (F-024) |
| 5 | `quality.jsonl` answers P13 | ✅ PASS (F-024) |
| 6 | Zero credentials in `data/`, logs, commits | ✅ PASS (`.env` empty; P8 scrubber ran; grep clean) |
| 7 | Disk growth measured | ✅ PASS — ~379 MB/mo raw → **~71 MB/mo on disk at 5.3× gzip** (F-028) |

**Probe status:** P1–P17 all run. P14 **partially met** (7 depth rounds across 4–5 hours vs "~20
across hours" — interrupted by host downtime). P8 part B (signed-vs-unsigned comparison) awaits a
key and is optional. Everything else complete and recorded.

---

## 2. Architecture understanding (conclusions that bind the build)

1. **`MarketSnapshot` is the look-ahead boundary** (`10` §2). The strategy engine is a *pure
   function* of an immutable snapshot with no store handle, no clock and no I/O. Look-ahead becomes
   structurally impossible rather than a bug class. `forming_bar` is **type-separated** from
   `closed_bars`; only trigger logic may read it.
2. **One engine, two drivers.** `LiveDriver` and `BacktestDriver` differ only in how they build
   snapshots. Engine, arbitration, risk and cost modules are shared *verbatim* — this is the
   structural form of `08` criterion 10 and `09` criterion 4.
3. **Two processes only** (`10` §3): `recorder` (unrecoverable data, must never die with the app)
   and `app` (everything else); probes/backfill/backtest are offline entry points. Microservices,
   brokers, Redis, Postgres, k8s, WebSocket and React are **explicitly rejected** (`10` §11).
4. **The recorder does not retire** (`11` §1 Revision 2 — reverses the Phase 0 plan). It keeps
   running independently alongside the app, duplicating ~230 req/h. Coupling them is rejected.
5. **SQLite is the single source of truth; Parquet is derived** (`11` §8.1). This conflicts with
   the Phase-1 backfill, which writes Parquet directly — see Decision D-4.
6. **Tier A vs Tier B is the central structural split** (`02` §2). Tier A (OHLCV-derived) is
   backtestable today. Tier B (funding, positioning, book, tape) has **zero history** and begins at
   `recorder_start`. Every signal carries `validated: bool`; the UI must never blur the two.
7. **Recompute from window, never accumulate** (`12` §1) — for features *and* for strategy state
   (`13` §2: `X*` is recomputed from closed 1m bars, not tracked). Guarantees live/backtest parity
   and makes restart and audit replay exact.
8. **ACT is one implementation with nine consumers** (`03` §2, `13` §3). A second copy is a defect.
9. **No cross-strategy composite score, ever** (`07` §1). Two numbers survive: per-strategy
   `strength` and `breakeven_hit_rate`. `confidence` is `null` system-wide until the `07` §9 gate.
10. **The risk engine vetoes; it never improves a signal** (`08` §1). Levels arrive immutable
    (`13` §8.1: `Signal` wraps a frozen `RawSignal`) and every veto is evaluated — no
    short-circuiting, in conditions or vetoes.
11. **The fee unit trap is the highest-consequence bug available** (`10` §5): the API returns
    `taker_fee: 0.059` meaning *percent*. A `Fee` type must make the ÷100 unskippable, with an
    assertion that any fee entering the cost model is `< 0.01`. Nothing would fail visibly.
12. **Reject, do not coerce** (`11` §4.2). A malformed payload makes the system go *quiet*; it never
    produces a guessed value. `NO VALID TRADE` and an empty dashboard-with-explanation are
    first-class outputs (`10` §10, `14` §4).
13. **Pre-registration is mechanical** (`09` §1): `validation/protocol.lock.json` committed before
    the first backtest (checkable in git history), holdout physically separate and **burned on
    second contact**, `trial_count.json` append-only and never reset (`19` §2).
14. **Warm-up is a first-class state** (`14` §5): ~60 days of 1h bars and ~180 days of 1d bars, but
    warm-up counts **available bars including backfill** — which the Phase-1 backfill already
    satisfies. Say so in the UI, since "60 days from now" and "already warm" differ enormously.

---

## 3. Dependency map (what actually gates what)

**Spec numbers are not build order** (`HANDOVER` §2). Specs 02–09 are *design*; 11–14 and 16
*implement* them.

```
00,01,01b  ── DONE ──┐
                     ▼
PHASE-01  branch resolution + parameter registry + repo reconciliation   ✅ DONE
   │
   ├─► PHASE-02A  Azure recorder host (independent of all code — DO NEXT)
   │
   ├─► PHASE-02  canonical types (Fee, Decimal, Pydantic)  ─────────────┐
   │        │                                                          │
   │        ▼                                                          │
   │   PHASE-03  per-endpoint normalizers + 11 hazard tests            │
   │        │                                                          │
   │        ▼                                                          │
   │   PHASE-04  SQLite store (source of truth) ──► PHASE-05 Parquet/backfill import
   │        │                                                          │
   │        ├─► PHASE-06  ingest adapters + async scheduler + ingest DQ │
   │        │        │                                                  │
   │        │        ▼                                                  │
   │        │   PHASE-07  bar finalization + gaps/known_absent_bars ◄────┘
   │        ▼                                                          (gates aggregation)
   │   PHASE-08  MarketSnapshot + Live/Backtest drivers  ◄── the look-ahead boundary
   │        │
   │        ▼
   │   PHASE-09  feature infra (FeatureValue, parity harness, warm-up, registry)
   │        ├─► PHASE-10 Families 1–2   ├─► PHASE-11 Families 3–4
   │        ├─► PHASE-12 Families 5,9   └─► PHASE-13 Tier B 6,7,8 + Family 10
   │        ▼
   │   PHASE-14  regime engine (fit, 3 validity tests, degraded) + redundancy protocol
   │        │         └── resolves C1-B cond 4 and er_min (parameter count → 29 or 30)
   │        ▼
   │   PHASE-15  strategy framework: ACT (once), conditions, lifecycle, SetupState
   │        ├─► PHASE-16 Class 1 ─► PHASE-17 Class 2 ─► PHASE-18 Class 3 ─► PHASE-19 Class 4
   │        ▼
   │   PHASE-20  arbitration ──► PHASE-21 risk engine (shared cost module) ──► PHASE-22 audit+replay
   │        ▼
   │   PHASE-23/24  backend API ──► PHASE-25 dashboard (specs/15 — unblocked 2026-09-27)
   │        ▼
   │   PHASE-26 backtest harness ─► PHASE-27 Stage-1 halt gate ─► PHASE-28 Stages 2–9 ─► PHASE-29 holdout+report
   │        ▼
   └─► PHASE-30 shadow run (calendar-bound 8–12 weeks) ─► PHASE-31 deploy ─► PHASE-32 operate
```

### Cross-cutting dependencies that constrain earlier design

| Consumer (later) | Requires (earlier) | Consequence if missed |
|---|---|---|
| `08` cost model, `09` validation | `Fee` type from `10` §5 / PHASE-02 | Costs 100× understated; every result invalid, silently |
| `02` 4h features, `12` §6 | `known_absent_bars` from `11` §7.2 / PHASE-07 | Aggregating 13 of 15 1m bars yields a silently wrong 15m bar |
| C1-B, C2-A, C3-B dwell conditions | regime model + **degraded mode** / PHASE-14 | Strategies dead if the model fails its validity tests |
| C1-B cond 4 (gate vs strength), `er_min` | redundancy protocol / PHASE-14 | Double-counted evidence; an unjustified parameter |
| 9 strategies | ACT / PHASE-15 | Duplicate implementations diverge |
| `07` merge/conflict, `08` §4.3 allocation | ranked, conflict-resolved set / PHASE-20 | Concentration risk: 3 correlated longs sized as 3 independent trades |
| `09`/`16` backtest | **same** cost/sizing module as live / PHASE-21 | Validation measures a different system than it ships |
| `14` §7 rejections endpoint, H1/H5/H10 | no-short-circuit condition records / PHASE-15, `rejections` table / PHASE-04 | "Class 1 never fired" with no diagnosis |
| `16` Stage 2 | `protocol.lock.json` **committed before** any backtest output | Pre-registration unprovable; holdout worthless |
| `09` §13 Tier B validation date | continuous `recorder_start` | Overstated evidence base (see F-024) |
| `17` shadow run | determinism (`13` §6) + snapshot purity (`10` §2) | Live-only signals cannot be distinguished from bugs |

---

## 3A. Azure architecture (deployment target)

**Decided 2026-09-27.** The project runs on Azure. `specs/18` §2 already concluded "a 1–2 GB VPS in
Mumbai or Singapore, systemd, no Docker, no secrets manager, no monitoring SaaS"; Azure is that
conclusion expressed in Azure services. **The `specs/10` §3 two-process topology is unchanged** —
Azure changes *where* it runs, not *what* it is.

### Component map

| Need | Azure service | SKU / configuration | ~cost/month (verify in the pricing calculator) |
|---|---|---|---|
| **Recorder + app compute** | **Azure VM**, Ubuntu 24.04 LTS, Linux | **B1s** (1 vCPU, 1 GiB, burstable, premium-capable), Central India | ~$8 PAYG; ~$5 with a 1-year reservation or savings plan |
| OS + data disk | **Managed Disk, Standard SSD** | **E4 (32 GiB)** — holds the 1.3 GB backfill, SQLite and ~71 MB/month of raw JSONL with years of headroom | ~$2.4 |
| Outbound + break-glass SSH | **Public IP, Standard, static** | Required: Basic IPs are retired and default outbound access is withdrawn, so a VM needs an IP (or a NAT Gateway at ~10× the cost) | ~$3.7 |
| **Dashboard access** | **Tailscale or WireGuard on the VM** (free tier), or an SSH tunnel | App stays bound to `127.0.0.1`; **no inbound port is ever opened** | $0 |
| **Backups of `data/raw/` + `state.db`** | **Azure Blob Storage**, StorageV2, LRS, **Cool** tier | Nightly `azcopy sync`; lifecycle rule → Cold after 90 days. Auth by **VM system-assigned managed identity** + `Storage Blob Data Contributor` — **no key or connection string exists to leak** | <$0.50 at a few GB |
| **Holdout isolation** | Separate blob **container** with version-level immutability | Enforces `specs/09` §1.2 "physically separate, burned on second contact" at the storage layer, not by convention | included above |
| Alerting when the dashboard is closed | **One Azure Monitor metric alert** on VM availability + **Action Group** (email) | Platform metrics are free; the project's own recorder-heartbeat webhook (`specs/17` §9, `specs/18` §10) stays the primary signal | ~$0.10–0.50 |
| Cost guard | **Azure Budget** with an alert at ~$25/month | Free. Catches a mistaken SKU or a forgotten resize | $0 |
| Clock discipline | **chrony bound to the Azure host PTP clock** (`/dev/ptp_hyperv`) | Directly serves `specs/18` §8 and keeps `DQ_CLOCK` inside the ±3 s tolerance (F-026) | $0 |
| Process supervision | **systemd**, two **independent** units, `Restart=always`, `StartLimitIntervalSec=0` on the recorder | Exactly `specs/18` §5. No Docker in the deploy path | $0 |

**Total ≈ $14–16/month pay-as-you-go, ≈ $11 with a 1-year VM reservation.** If the subscription is
a new Azure free account, 12 months of B1s (750 h) plus 64 GiB of managed disk are included — check
before provisioning, because that makes year one close to free.

### Azure decisions

| ID | Decision | Rationale |
|---|---|---|
| **AZ-1** | **A single B-series VM, not a PaaS product.** | The recorder is a stateful long-running poller whose local SQLite is the source of truth (`specs/11` §8.1). App Service, Container Apps and AKS all add cost and indirection; Container Apps' scale-to-zero would *stop the recorder*, and Functions' execution limits are incompatible with a persistent loop. `specs/10` §11 already rejected orchestration on principle. |
| **AZ-2** | **No Spot VM.** | Eviction is unrecoverable Tier B data loss (F-024 shows exactly what downtime costs). The entire reason for moving off the laptop is uptime; a 60–90% discount that can be evicted defeats it. |
| **AZ-3** | **Standard SSD (E4), not Premium.** | Premium P4 would buy the 99.9% single-instance SLA instead of 99.5%, for ~$3/month more. Our workload is ~230 requests/hour and a few MB of writes per day — IOPS are irrelevant, and a short host-maintenance gap is recorded and (for candles) backfilled. **Revisit if measured availability disappoints**; this is the one place where paying ~$3 more buys a stated SLA. |
| **AZ-4** | **Blob Storage + managed identity for backups; no Azure Backup, no Key Vault.** | `data/raw/` is the only irreplaceable data (`specs/18` §6). A nightly `azcopy sync` to Cool blob storage covers it for cents. Managed identity means **no stored credential at all**, which is strictly better than the file-based key `specs/18` §4 assumed. Azure Backup (~$5/month + storage) buys whole-VM recovery we do not need — the VM is reproducible from git plus cloud-init. Key Vault is unnecessary because, after F-019, the project has **no secret to store**. |
| **AZ-5** | **Do not ship logs to Log Analytics.** | The recorder logs one DEBUG line per poll (tens of MB/day). Log Analytics ingestion is billed per GB, so this would cost real money to store logs that are already on disk, rotated, and queryable with `grep`. `specs/10` §8 is explicit that **logs are not the audit trail**; the audit trail is SQLite. Keep one free platform-metric alert and the project's webhook. |
| **AZ-6** | **No Azure Bastion.** | ~$140/month for a jump host, versus $0 for Tailscale or an SSH tunnel. `specs/18` §3's recommendation (private network, never open the port) is cheaper *and* stronger. |
| **AZ-7** | **Central India (Mumbai) primary, Southeast Asia (Singapore) fallback — measured, not assumed.** | `specs/18` §2 ties region to the 2-second basis tolerance and `DQ_LATENCY`. PHASE-02A measures RTT and `btST`-to-receive latency from the VM before the region is fixed, and records it in `docs/findings.md` next to the laptop's 69–88 ms baseline (F-026). |
| **AZ-8** | **SQLite + Parquet on the VM disk stay the storage layer.** No Azure SQL, Cosmos DB, or managed Postgres. | `specs/11` §8.1 makes SQLite the single source of truth; at ~260k rows/month a managed database adds cost, latency and an operational dependency for nothing. The `StorageInterface` (PHASE-04) keeps migration possible if that ever changes. |
| **AZ-9** | **Heavy validation runs do not size the VM.** | Walk-forward over ~13M 1m bars wants memory the B1s does not have. Run PHASE-26–29 **locally**, or temporarily resize the VM (deallocate → resize → start) and pay for the hours used. Sizing a 24/7 VM for an occasional batch job is the expensive mistake. |
| **AZ-10** | **Disable the dev/test auto-shutdown default; set a budget alert instead.** | Azure's VM creation flow offers auto-shutdown, which would silently stop the recorder every night — precisely the failure F-024 measured. The cost control is a budget alert, not a shutdown schedule. |

### What Azure changes in the plan — and what it does not

**Changes:**
1. **New `PHASE-02A`** deploys the *existing, working* recorder to Azure immediately, so Tier B
   history accumulates while later phases are built. It depends on nothing else.
2. **`PHASE-31`** is rewritten around Azure specifics (NSG, managed identity, chrony PTP, budget
   alert, a host-maintenance reboot drill).
3. **Backups move earlier** — into PHASE-02A — because the moment the VM holds the only copy of
   `data/raw/`, an unbacked-up VM is the single largest data risk in the project.
4. **`Settings` gains `environment: "azure"` and a `Backup` block** (landed in PHASE-01).
5. **`deploy/azure/`** holds cloud-init, the two systemd units, the backup script and the
   provisioning script — committed, reviewable, no secrets.
6. **Holdout isolation** (PHASE-05) also gets an immutable blob container.

**Explicitly unchanged:** the two-process topology; SQLite as source of truth; no WebSocket;
localhost binding with a token-or-refuse guard; no secrets manager; the 30-parameter budget; every
validation requirement in `specs/09` and `specs/16`. Azure is a hosting decision, not an
architectural one.

---

## 4. Master plan

**Context-safety rule:** one phase per session. Each phase ends with tests green, `latest-updates.md`
updated, and a named next action. Stop at the planned checkpoint, never at context exhaustion.

Status values: `NOT STARTED` · `IN PROGRESS` · `BLOCKED` · `COMPLETED`.

---

### PHASE-01 — Pending-branch resolution, parameter registry, repo reconciliation
**Status:** ✅ **COMPLETED 2026-09-27** · **Specs:** `02` §1, §10; `10` §12; `HANDOVER` §2 step 2

**What landed:**
- **All seven `02` §1 branches resolved** and recorded as **F-029**, each citing probe evidence. Two
  consequences recorded for later phases: fold boundaries must use each series' `continuous_from`
  date, and C1-A's funding mechanism has never been observed live (no funding extreme in the sample).
- **`specs/03` §5 amended** with C1-C condition 7 (`MR_VR(1h) < 1`), required by `04` §7 — recorded
  as **F-030**. Mutual exclusivity with C2-B is restored.
- **`config/parameters.toml`**: 30 entries, cap enforced, each with band, justification, source spec
  and overfitting exposure. **`src/config/parameters.py`** enforces the rules mechanically: the cap,
  no duplicate id/name, a non-empty justification, `provisional` values must lie inside their own
  sweep band, `fixed` parameters may not carry a band, and `unset`/`provisional`/`conditional` must
  name the phase that sets them. `parameter_hash` covers values only, so editing prose cannot
  invalidate a past signal's provenance.
- **A `judgment` status was added** during implementation: `specs/08` §11 registers
  `unvalidated_multiplier` as the one *permanent* judgment, so requiring it to name a setting phase
  was wrong. It is swept for sensitivity but never "set". (Decision D-11.)
- **`src/config/settings.py`**: operational settings, Azure-aware, with the `specs/14` §11 bind guard
  (`assert_bind_allowed`) and a test proving a non-localhost bind without a token is refused.
- **`validation/trial_count.json`** seeded at 0, append-only, with the `19` §3 increment rules inline.
- **CLAUDE.md** control panel rewritten (phase lock + pointer to this file); **invariant 12 corrected**
  — nothing the project uses needs authentication (F-019).
- **Tooling:** all `specs/10` §6 dependencies installed (polars 1.44, pydantic 2.13, numpy 2.5,
  scipy, statsmodels, duckdb 1.5, fastapi, jinja2, structlog, hypothesis); ruff + mypy configured.
- **`.gitignore`** extended per the `specs/18` §1 pre-push checklist, with an explicit note that
  `validation/*.json` **must** be committed.

**Validation:** 88 tests pass (62 pre-existing + 26 new). `ruff check` clean. `mypy` clean on all new
code. Every ruff ignore is annotated with why the pattern is deliberate.

**Checkpoint:** next phase = `PHASE-02A` (Azure recorder host); the code path continues at `PHASE-02`.

---

### PHASE-02A — Azure recorder host (deploy what already works) ⭐ DO THIS NEXT
**Status:** NOT STARTED · **Specs:** `18` §2–§6, §8; `17` §9; `01b`; §3A above
**Read:** `specs/18`, `specs/17` §9, `latest-updates.md` §3A, `docs/findings.md` F-024, `recorder.toml`
**Create:** `deploy/azure/provision.sh` (az CLI, idempotent), `deploy/azure/cloud-init.yaml`,
`deploy/azure/suggester-recorder.service`, `deploy/azure/backup-to-blob.sh`,
`deploy/azure/README.md` (runbook: provision, deploy, verify, roll back)
**Modify:** `recorder.toml` (Azure paths), `README.md` (Azure operations section)
**Depends on:** nothing in the code. Needs from the operator: subscription, region confirmation,
and an SSH key (Q-H).
**Why now:** measured recorder uptime on the laptop was **12.1 h of 103 h** (F-024). Every hour
down is funding and positioning history that no endpoint will ever serve again, and C1-A's mechanism
has still never been observed (F-029). This phase depends on no other work, so it should not wait.
**Tasks:**
1. Provision: resource group, VNet/subnet, NSG (**deny all inbound**; SSH from the operator's IP as
   break-glass only), Standard static public IP, B1s VM with a 32 GiB Standard SSD, system-assigned
   managed identity. **Auto-shutdown explicitly disabled** (AZ-10).
2. cloud-init: Python 3.11+, git, `chrony` bound to the Azure host PTP clock, unattended security
   upgrades, a non-root service user, `/opt/suggester` layout.
3. Deploy the recorder from git, install from the lockfile, `systemd` unit per `specs/18` §5
   (`Restart=always`, `StartLimitIntervalSec=0`, no coupling to any app unit).
4. Storage account + containers (`raw`, `state`, `holdout`); grant the VM's managed identity
   `Storage Blob Data Contributor`; nightly `azcopy sync` of `data/raw/` via systemd timer.
5. Budget alert (~$25/month) and one VM-availability metric alert wired to an email Action Group.
6. **Measure and record** (AZ-7): RTT and `received_at_ms − btST` from the VM to both hosts, against
   the laptop baseline in F-026. Confirm or switch region on the evidence.
7. Start the recorder and note the new **continuous** `recorder_start`; recompute
   `earliest_tier_b_validation` from it (F-024's warning).
**Tests / drills (the subset of `specs/18` §7 that applies without the app):**
`kill -9` the recorder → restarts, JSONL intact, gap recorded · reboot the host → unit starts on
boot, gaps detected, candles backfilled · block the API hosts 30 min → backoff, no hot loop, gap
recorded · restore `data/raw/` from blob → archive complete and parseable · kill the recorder and
leave it down → heartbeat alert fires.
**Completion:** recorder running 24/7 on Azure; nightly backup verified **by a restore**; region
choice evidence-based and recorded; the 72 h acceptance run (criterion 1) started and its start time
recorded here.
**Risks:** the laptop recorder must be stopped when Azure takes over, or two instances will write
divergent archives — the lock only protects a shared directory, not two hosts. Record the cutover
time and keep the laptop archive as a separate, labelled dataset.
**Checkpoint:** Tier B clock running properly; continue the code path at `PHASE-02`.
**Read:** `specs/02` §1 and §10, `docs/findings.md` (F-007…F-028), `CLAUDE.md`, `recorder.toml`
**Create:** `config/parameters.toml` (all registered parameters + justification + hash),
`src/config/__init__.py`, `src/config/parameters.py`, `src/config/settings.py`,
`validation/` (dir), `validation/trial_count.json`, `tests/test_config.py`
**Modify:** `CLAUDE.md` (phase status table, invariant 12), `pyproject.toml` (add polars, pydantic,
pydantic-settings, numpy, scipy, statsmodels, duckdb, fastapi, uvicorn, jinja2, structlog,
hypothesis), `docs/findings.md` (branch decisions), `specs/03` §5 (C1-C condition 7 amendment
required by `04` §7)
**Depends on:** nothing (Phase 1 evidence already recorded)
**Tasks:**
1. Resolve all seven `⧗ PENDING` branches of `02` §1 in writing, citing findings (see §5 Decisions
   D-7 for the answers already established by probes).
2. Apply the `specs/03` amendment (C1-C condition 7: `MR_VR(1h) < 1`) and record it.
3. Build the parameter registry: 25 entries from `02`/`03`/`04`/`05` + 5 from `08` §11 = 30, each
   with band + justification + overfitting exposure; expose `parameter_hash`.
4. Settings module (Pydantic Settings) for operational config, separate from model parameters.
5. `trial_count.json` seeded at 0, append-only structure per `19` §2.
6. Record the layout reconciliation decisions (D-1…D-5).
**Tests:** registry has exactly 30 entries; every entry has band + justification; `parameter_hash`
is stable and changes when any value changes; settings load from TOML; no model parameter is
readable from anywhere but the registry.
**Validation:** `pytest` green; `docs/findings.md` contains one entry per resolved branch.
**Completion:** all 7 branches recorded with the taken branch; registry test green; CLAUDE.md
status table reflects the unlocked phase.
**Risks:** resolving a branch from a *partially* met probe (P14) — mark that explicitly rather than
treating it as settled.
**Checkpoint:** branch decisions in findings; registry committed; next phase = PHASE-02.

---

### PHASE-02 — Canonical types: `Fee`, Decimal enforcement, Pydantic models
**Status:** ✅ **COMPLETED 2026-09-28** · **Specs:** `10` §5; `11` §4.3
**Read:** `specs/10` §5, `specs/11` §4, `specs/00` §2 and §5
**Create:** `src/normalize/__init__.py`, `src/normalize/canonical.py`, `src/normalize/errors.py`,
`tests/test_canonical.py`
**Modify:** none
**Depends on:** PHASE-01 (settings)
**Tasks:** `Fee` value object (percent→fraction, unskippable, `< 0.01` assertion); canonical models
`Bar`, `Trade`, `BookSnapshot`, `FuturesRT`, `Positioning`, `InstrumentMeta` with Decimal prices,
int ms timestamps, ascending-bar invariant, book levels as numerically sorted `list[tuple]`;
`NormalizeOutcome = Accepted | Rejected | Partial`.
**Tests:** **construct the 100× fee error deliberately and prove it is caught** (`10` criterion 4);
Decimal preserved through model round-trip; a model rejects rather than coerces bad input; book
levels sorted numerically from an out-of-order payload.
**Completion:** every quantity in `10` §5 has a canonical type; fee test green.
**Risks:** the fee trap is invisible if the assertion is omitted — this test is a halt gate later
(`16` Stage 1).
**Checkpoint:** types frozen; PHASE-03 next.

**Landed:** `Fee` with no bare constructor and a 100x-error guard, frozen Pydantic canonical models with OHLC/alignment validators, `Money` rejecting float, `validate_ms` rejecting seconds-as-ms.

---

### PHASE-03 — Per-endpoint normalizers + all eleven hazards on real payloads
**Status:** ✅ **COMPLETED 2026-09-28** · **Specs:** `11` §4, §5, §5.1; `00` §5
**Read:** `specs/11` §5 table, `specs/00` §5, `tests/fixtures/README.md`, `docs/findings.md`
F-001/F-003/F-008/F-009/F-010/F-014/F-025/F-026
**Create:** `src/normalize/{spot_candles,futures_candles,spot_ticker,futures_rt,spot_book,futures_book,instrument,positioning,trades}.py`,
`tests/test_normalize_hazards.py`, new fixtures extracted from `data/raw/probe/`
**Depends on:** PHASE-02
**Tasks:** one normalizer per endpoint (never generic); row-level `futures_rt` validation in the
`11` §5.1 order (**but per F-004 `skw` is NOT a health flag — see D-6**); descending→ascending
spot candles; seconds→ms futures; volume base-denomination; zero-qty level drop; `m` flag
canonicalisation; spot-book freshness gate (F-026 anomaly).
**Tests:** one per hazard, each on a **real captured payload**; `B-NKN_USDT` stale/incomplete row
rejected with the right reasons while valid rows pass; ascending assertion with newest-last.
**Completion:** all 11 hazards have passing tests on real payloads; every rejection carries a
reason and a raw reference.
**Risks:** `11` §5.1 step 1 contradicts F-004 — implement D-6, not the spec text, and record it.
**Checkpoint:** hazards covered; PHASE-04 next.

**Landed:** One normalizer per endpoint, each rejecting rather than coercing; all eleven hazards covered on real captured payloads (44 tests). Later re-run at scale against the whole recorder archive: **5,741 payloads, zero unexplained rejections** (the single `DQ_STALE` is F-026's 10.5-minute spot book).

---

### PHASE-04 — SQLite store: schema, `StorageInterface`, point-in-time boundary
**Status:** ✅ **COMPLETED 2026-09-28** · **Specs:** `11` §8; `10` §7
**Read:** `specs/11` §8, `specs/10` §7
**Create:** `src/store/{__init__,interface,sqlite_store}.py`, `src/store/migrations/`,
`tests/test_store.py`
**Depends on:** PHASE-02, PHASE-03
**Tasks:** market tables (`bars`, `known_absent_bars`, `futures_rt`, `positioning`,
`book_snapshots`, `trades`, `instrument_meta`, `dq_events`) + operational tables (`signals`,
`signal_events`, `signal_audit`, `rejections`, `arbitration_events`, `runs`); `StorageInterface`
Protocol; migrations; WAL; upsert semantics with `fetch_count`.
**Tests:** **`get_point_in_time` never returns `ts_ms > as_of_ms`**; upsert idempotence; no caller
depends on SQLite specifics (import check); `dq_events` append-only.
**Completion:** interface complete; as_of test green; swapping the backend touches only `src/store/`.
**Checkpoint:** store usable by ingest and snapshot; PHASE-05 next.

**Landed:** Two migrations, 17 tables, WAL + `synchronous=FULL`, every sqlite failure mode classified, two-fetch finalization, bar-mutation detection, `as_of` reads with a look-ahead assertion that re-checks the result.

---

### PHASE-05 — Parquet export, Phase-1 backfill import, holdout separation
**Status:** ✅ **COMPLETED 2026-09-28** · **Specs:** `11` §8.1, §8.2; `09` §1.2; `10` §7
**Read:** `specs/11` §8, `specs/09` §1.2, `src/backfill/__main__.py`, `data/backfill/verify.json`
**Create:** `src/store/parquet_export.py`, `src/store/import_backfill.py`, `tests/test_parquet_export.py`
**Depends on:** PHASE-04
**Tasks:** import the existing 42 verified Parquet series into SQLite `bars` (D-4); regenerate
`data/parquet/` **from** SQLite; carve `data/holdout/` as a physically separate directory the
harness cannot read without a logged flag; record the continuous-start dates per series from F-018
so warm-up and window boundaries use them.
**Tests:** SQLite and regenerated Parquet return identical data for any overlapping range; holdout
directory unreadable without the flag, and the access attempt is logged; imported bar counts match
`verify.json` exactly.
**Completion:** one write path (SQLite); Parquet derived; holdout separated.
**Risks:** double source of truth if the old backfill keeps writing Parquet directly — repoint it.
**Checkpoint:** history queryable through the store; PHASE-06 next.

**Landed:** 20,331,972 backfill rows imported (6.2 GB), atomic Parquet export, holdout guard with a single-use unlock, access log and burn marker.

---

### PHASE-06 — Ingest adapters, async scheduler, priorities, ingest-time DQ
**Status:** ✅ **COMPLETED 2026-09-28** · **Specs:** `11` §2, §3, §9, §10
**Read:** `specs/11` §2–3 and §9, `src/cdx_http/client.py`, `docs/findings.md` F-022 (rate limits)
**Create:** `src/ingest/{__init__,scheduler,authenticated,dq}.py`, `src/ingest/adapters/*.py`,
`tests/test_ingest_scheduler.py`, `tests/test_ingest_dq.py`
**Depends on:** PHASE-03, PHASE-04
**Tasks:** one adapter per endpoint returning `IngestResult` (never parses); asyncio scheduler with
per-task last-run state and restart catch-up; P0–P3 priorities with preemption and P3 dropped
first; dropped polls → `dq_events`; ingest DQ metrics (`DQ_AGE`, `DQ_LATENCY`, `DQ_CLOCK`,
`DQ_COMPLETE`, `DQ_DISAGREE` with the 2 s tolerance); reuse `cdx_http` as transport (D-2).
**Tests:** priority preemption under a constrained bucket; restart catch-up with no duplicate rows;
`503` handled distinctly from `429` without rate reduction; `DQ_DISAGREE` only on aligned pairs.
**Completion:** measured request rate recorded and compared against F-022's 5,000/60 s budget.
**Checkpoint:** live data flowing into the store; PHASE-07 next.

**Landed:** Ingest client mapping every transport/status outcome to a classified error, priority scheduler with per-class backoff, budget shedding, non-retryable stops (never an endless retry), and the ingest-time DQ monitor.

---

### PHASE-07 — Bar finalization, mutation detection, gaps and `known_absent_bars`
**Status:** ✅ **COMPLETED 2026-09-28** · **Specs:** `11` §6, §7
**Read:** `specs/11` §6–7, `docs/findings.md` F-013 (aggregation exactness), F-018 (holes)
**Create:** `src/pipeline/{__init__,finalization,gaps,backfill}.py`, `tests/test_finalization.py`,
`tests/test_gaps.py`
**Depends on:** PHASE-06
**Tasks:** two-fetch finalization rule (`is_final` only after two identical fetches, 5-bar
re-fetch window); mutation detection → ERROR + `dq_events.BAR_MUTATION` + flag affected signals;
gap detection on startup and hourly; **`known_absent_bars` with two confirmations**; aggregation
**refuses** to span an unconfirmed gap; resumable backfill at P3 priority.
**Tests:** bar not final on first fetch, final on identical second; mutation flagged; missing vs
non-existent distinguished; aggregation refusal; features consume only final bars.
**Completion:** `11` acceptance 5–7 met.
**Risks:** this is the load-bearing gate for every aggregated timeframe (`02` §1 P12 branch).
**Checkpoint:** bar store trustworthy; PHASE-08 next.

**Landed:** Gap detection, two-sighting absence confirmation, and aggregation that **refuses** across an unconfirmed gap with the reason attached.

---

### PHASE-08 — `MarketSnapshot` and the two drivers
**Status:** ✅ **COMPLETED 2026-09-28** · **Specs:** `10` §2
**Read:** `specs/10` §2, `specs/12` §2
**Create:** `src/snapshot/{__init__,snapshot,live_driver,backtest_driver}.py`, `tests/test_snapshot.py`
**Depends on:** PHASE-04, PHASE-07
**Tasks:** immutable `MarketSnapshot` (`as_of_ms`, `closed_bars` ascending, `forming_bar`
**type-separated**, `point_in_time` Tier B values with their own timestamps, `instruments`, `dq`,
`snapshot_id` = hash of (as_of, code_version, param_hash)); `LiveDriver`; `BacktestDriver`.
**Tests:** same `as_of` through both drivers produces structurally identical snapshots; no bar with
`close_time > as_of`; snapshot is immutable (mutation raises); a function accepting `forming_bar`
cannot be passed a `closed_bars` container and vice versa.
**Completion:** `10` criteria 1–3 met.
**Checkpoint:** the look-ahead boundary exists; PHASE-09 next.

**Landed:** `MarketSnapshot` (immutable, no store handle), `FormingBar` type-separated from `Bar`, Live and Backtest drivers with an asserted parity test, holdout wall on the backtest window. D-17…D-20.

---

### PHASE-09 — Feature infrastructure: `FeatureValue`, parity harness, warm-up, registry, cache
**Status:** ✅ **COMPLETED 2026-09-28** · **Specs:** `12` §2–§8, §13
**Read:** `specs/12` §1–8, `specs/02` §4, §9
**Create:** `src/features/{__init__,base,numeric,windows,registry}.py`, `tests/test_feature_infra.py`
**Depends on:** PHASE-08
**Tasks:** `FeatureValue` (value + status + reason + window + bars_used + version); `FeatureSet`
that **raises** on access to a non-OK feature; Decimal↔float boundary as two named functions;
window extraction with gap checks and warm-up accounting; registry (name → impl, version,
`min_bars_required`, tier); cache keyed with `feature_version`; the **automated registry-wide
look-ahead scan**; the `compute_series` vs `compute_at` parity harness.
**Tests:** warm-up flips to OK exactly at threshold; `GAP_IN_WINDOW` on unconfirmed gap; cache
invalidated by a version bump; parity harness fails a deliberately mismatched feature.
**Completion:** infrastructure ready for family modules; parity harness proven to catch a mismatch.
**Checkpoint:** PHASE-10 next.

**Landed:** `FeatureValue`/`FeatureSet` contract (reading a non-OK value raises), the Decimal/float boundary as two named functions, every `specs/12` §4 guard, window extraction with the §6 gap rules and 1m synthesis, registry + engine + cache.

---

### PHASE-10 — Families 1–2: trend and volatility
**Status:** ✅ **COMPLETED 2026-09-28** · **Specs:** `02` Families 1–2; `12` §4, §4.1
**Create:** `src/features/trend.py`, `src/features/volatility.py`, `tests/test_features_trend_vol.py`
**Depends on:** PHASE-09
**Tasks:** `TRD_TSTAT` (OLS β/SE via statsmodels), `TRD_ER`, `TRD_RET`; `VOL_CC`, `VOL_PARK`,
`VOL_GK`, `VOL_RATIO`, `VOL_OF_VOL`, `ATR` (**SMA, not Wilder**); every numerical guard from
`12` §4.
**Tests:** hand-computed fixtures per feature; `compute_series == compute_at`; **negative
Garman–Klass raises `INVALID_OHLC` and is never clamped**; `ZERO_RESIDUAL_VARIANCE`,
`NO_MOVEMENT`, `ZERO_VARIANCE` constructed; Hypothesis: `TRD_ER ∈ [0,1]`, z-scores scale-invariant.
**Completion:** both families pass hand-computed + parity + guard tests.
**Checkpoint:** PHASE-11 next.

**Landed:** Families 1-2 (9 features). Scalar/vectorised parity asserted per feature, table-driven over the registry.

---

### PHASE-11 — Families 3–4: structure and deviation (incl. Lo–MacKinlay VR)
**Status:** ✅ **COMPLETED 2026-09-28** · **Specs:** `02` Families 3–4; `02` §8.5
**Create:** `src/features/structure.py`, `src/features/deviation.py`, `tests/test_features_struct_dev.py`
**Depends on:** PHASE-10
**Tasks:** `STR_SWING` (ATR-scaled fractal pivot, **timestamped at the confirmation bar**),
`STR_SEQ`, `STR_DIST_PDH`, `STR_RANGEPOS`; `DEV_Z`, `DEV_ATR`, `DEV_VWAP`, `MR_AC1`, `MR_VR`
(heteroskedasticity-robust overlapping estimator exactly as `02` §Family 4 states, `k ∈ {2,4,8}`).
**Tests:** VR against a hand-computed value **and** against a synthetic random walk (≈1), trending
series (>1) and mean-reverting series (<1); pivot lag test — the feature timestamp is the
confirmation bar, never the pivot bar; `MR_VR > 0` property.
**Completion:** `MR_VR` is the load-bearing gate for C1-B/C2-A — its correctness test is explicit.
**Checkpoint:** PHASE-12 next.

**Landed:** Families 3-4 (11 features). **F-031**: the `specs/02` variance-ratio formula divides by `k` twice; implemented as standard Lo-MacKinlay with the deviation recorded (Q-J). Family 3 ships dormant on `swing_atr_multiple` and reports `PARAMETER_UNSET` (D-21).

---

### PHASE-12 — Families 5 and 9: cross-asset and volume
**Status:** ✅ **COMPLETED 2026-09-28** · **Specs:** `02` Families 5, 9
**Create:** `src/features/cross_asset.py`, `src/features/volume.py`, `tests/test_features_cross_vol.py`
**Depends on:** PHASE-11
**Tasks:** `X_CORR`, `X_BETA`, `X_RS` (beta-adjusted), `X_DISP`; `VLM_ZROBUST` (median/MAD),
`VLM_TREND`; `VLM_AGGR` left as a Tier B stub (tape is a 30-row snapshot — F-011).
**Tests:** hand-computed; correlation ∈ [−1,1]; `ZERO_MAD` guard; **volume units never compared
across sources without conversion** (F-010).
**Checkpoint:** PHASE-13 next.

**Landed:** Families 5 and 9 (6 features). Cross-asset series aligned **by timestamp**, not by position. Measured on real data: ETH/BTC correlation 0.84, SOL/BTC 0.81, betas 1.06 and 1.26 — exactly the concentration `specs/02` Family 5 warns about. D-24.

---

### PHASE-13 — Tier B families 6–8 + Family 10 wiring
**Status:** ✅ **COMPLETED 2026-09-28** · **Specs:** `02` Families 6–8, 10; `09` §13
**Create:** `src/features/funding.py`, `src/features/positioning.py`, `src/features/liquidity.py`,
`tests/test_features_tierb.py`
**Depends on:** PHASE-12, PHASE-06 (live Tier B data)
**Tasks:** `FND_*` on the 8h grid (**`FND_CUM` only over closed intervals**; settlement
approximation labelled at every use), `BAS_BPS`/`BAS_Z` with the **hard 2 s gate** and a logged
discard rate, `POS_*` (public per F-019), `LIQ_*` from the **1/2/5 bps bands only** (F-025);
Family 10 DQ consumed from ingest; compute `earliest_tier_b_validation` from *continuous*
`recorder_start` (F-024).
**Tests:** funding look-ahead test (open interval excluded); basis discard on a >2 s pair; bands
beyond book reach return `None`, never a truncated number; every Tier B feature marks itself
unvalidated.
**Checkpoint:** PHASE-14 next.

**Landed:** Tier B families 6-8 (21 features), point-in-time history in the snapshot, and `store/import_raw.py` bringing the recorder archive into SQLite. **F-032**: the futures book is capped at 50 levels spanning ±0.74 bps, so `specs/08` §7.1's 25 bps depth veto is not observable at this venue. Tier B features have no timeframe (`@pit`) and are checked at startup, not mid-pass.

---

### PHASE-14 — Regime engine + redundancy protocol (offline), with degraded mode
**Status:** ✅ **COMPLETED 2026-09-28** · **Specs:** `02` §6, §7; `12` §9–§11
**Create:** `src/regime/{__init__,model,fit,transitions,degraded}.py`,
`src/analysis/redundancy.py`, `tests/test_regime.py`, `tests/test_redundancy.py`
**Depends on:** PHASE-10…PHASE-13
**Tasks:** offline fit → `regime_model.json` (grid, trailing quantile lookback, transition matrix,
dwell stats, occupancy, all three validity-test results); runtime classification that **never
refits**; 3×3 → 2×2 escalation → **degraded mode** (drop dwell conditions, keep raw-axis
thresholds); redundancy protocol → `feature_selection.json` (Spearman, clusters at `|ρ| ≥ 0.8`,
representative chosen by **stability, never performance**, VIF).
**Tests:** classification at bar `t` uses no data after `t`; dwell resets on cell change; degraded
mode keeps raw-axis conditions and drops dwell ones; redundancy protocol finds the H9 clusters on a
synthetic set where they exist by construction.
**Completion:** both artifacts produced and versioned; validity results written to findings
**including a failure**; C1-B cond 4 and `er_min` decisions recorded (parameter count → 29 or 30).
**Risks:** a failed regime model is project-threatening but survivable — engage degraded mode,
do **not** relax the tests.
**Checkpoint:** PHASE-15 next.

**Landed:** Offline fit with all three validity tests, the 3x3 -> 2x2 -> degraded escalation, and the live classifier. **F-033**: fitted on real history, **ETH is usable at 2x2 while BTC and SOL are degraded** (median dwell 2.0 bars against a required 3). Separation passes everywhere at the spec-anchored 24-bar horizon. New `ProblemClass.MODELLING`, because "the model's assumption is not supported" is a different triage answer from a bug, a config error or a feed problem.

---

### PHASE-15 — Strategy framework: ACT, conditions, lifecycle, `SetupState`
**Status:** ✅ **COMPLETED 2026-09-28** · **Specs:** `03` §2; `07` §8; `13` §1–§6
**Create:** `src/strategies/{__init__,base,act,conditions,lifecycle}.py`, `tests/test_act.py`,
`tests/test_lifecycle.py`
**Depends on:** PHASE-14
**Tasks:** `Strategy` Protocol (+`expansion_seeking`, `tradeable`, `tier`); `StrategyOutcome`
variants carrying **full condition evaluation even on `NoSetup`**; `Condition`/`ConditionResult`
with a **no-short-circuit** evaluator; **ACT once** (pure, no state/IO/clock); lifecycle state
machine incl. `MISSED` via `remaining_rr`, `STALE` as non-terminal, `MISSED_OUTAGE`;
`SetupState` persisted, with `X*` **recomputed** from closed 1m bars.
**Tests (Hypothesis):** ratchet monotonicity, mirror, **no trigger on a monotone decline**,
symmetry under negation, unit scaling; no-short-circuit; `X*` reconstruction equals live tracking;
restart re-arms with the original `armed_at_ms`; outage crossing → `MISSED_OUTAGE`; static check:
no clock import under `src/strategies/`.
**Completion:** one ACT, all five properties green.
**Checkpoint:** PHASE-16 next.

**Landed:** ACT as one implementation with all five `specs/13` §3 properties under Hypothesis, conditions as first-class objects with **no short-circuiting**, the lifecycle state machine with illegal transitions refused, and `SetupState` derived from closed bars rather than accumulated.

---

### PHASE-16 — Class 1: C1-A, C1-B, C1-C
**Status:** ✅ **COMPLETED 2026-10-03** · **Specs:** `03` (+ `04` §7 amendment)
**Create:** `src/strategies/{c1a,c1b,c1c}.py`, `tests/test_class1.py`
**Depends on:** PHASE-15
**Tasks:** C1-B first (**the only immediately backtestable strategy**), then C1-A (funding, Tier B,
`FND_MULT` baseline anchor), then C1-C (two-leg, beta-neutral, ACT on the **spread**, condition 7
from the amendment); dual invalidation (price **and** mechanism) on all three; signal schema per
`03` §9 with `confidence: null`.
**Tests:** `NO VALID TRADE` names the failing gate; dual invalidation exits when the restoring force
switches off; C1-C publishes per-leg sizes; C1-A vetoes on funding/basis sign disagreement.
**Checkpoint:** PHASE-17 next.

**Landed:** All three. C1-B (the reference strategy, Tier A) and C1-A (Tier B, funding, anchored to
`FND_MULT` while `FND_Z` has no history) landed 2026-09-29. **C1-C** landed 2026-10-03 in
`src/strategy/c1c_relative.py`: a two-leg relative-value trade with ACT on the **residual spread**,
whose sigma comes from the variance decomposition `var(a − βb) = var(a) + β²var(b) − 2βρ·sd(a)sd(b)`
rather than from the residual's own history, and which publishes a per-leg table in `detail["legs"]`.
It needs peer features at the same `as_of`, which is what `MarketView` and the two-pass runner exist
for.

---

### PHASE-17 — Class 2: C2-A, C2-B, C2-C
**Status:** ✅ **COMPLETED 2026-10-03** · **Specs:** `04`
**Create:** `src/strategies/{c2a,c2b,c2c}.py`, `tests/test_class2.py`
**Depends on:** PHASE-16
**Tasks:** C2-A (structural stop, T1 at prior swing, **monotone trail**), C2-B (beta-normalised
expression score, **at most one concurrent position enforced in the strategy engine**), C2-C (Tier B
funding/positioning partition + `HANDOFF C2-C → C1-A`).
**Tests:** **C1-B ⊕ C2-A** and **C1-A ⊕ C2-C** asserted over generated market states; trail never
loosens (Hypothesis); expression scores recorded for all three assets incl. rejected.
**Checkpoint:** PHASE-18 next.

**Landed:** `src/strategy/c2_participation.py`. C2-A (7 conditions, structural stop at the last
confirmed swing ± s·ATR, T1 at the prior swing extreme, monotone trail, three invalidations), C2-B
(8 conditions; needs the market view, enforces **at most one position across all assets in the
strategy engine** as specs/04 §4 requires, with factor-flip and correlation-break invalidations) and
C2-C (10 conditions, Tier B, inherits C2-A's pullback and structure bodies, logs
`HANDOFF C2-C -> C1-A`). `assert_c1a_partition()` **raises** rather than deferring to arbitration,
because specs/04 §5 calls both arming a bug, not a conflict.

The monotone trail was extracted to `strategy/evaluation.update_trail` / `effective_stop` so C2-A/B/C,
C3-B and C4-A/B share **one** implementation — for the same reason ACT has one. A trail that loosens
in one strategy and not another is a bug that only shows up in one backtest.

---

### PHASE-18 — Class 3: C3-A, C3-B, C3-C
**Status:** ✅ **COMPLETED 2026-10-03** · **Specs:** `05`
**Create:** `src/strategies/{c3a,c3b,c3c}.py`, `tests/test_class3.py`
**Depends on:** PHASE-17
**Tasks:** `breakeven_hit_rate` (`p* = (1 + c/R)/(k+1)`) on **every** Class 3 signal; C3-A
directionless bracket with both triggers live; C3-B `[0,1.0)` ATR pullback band + `HANDOFF C3-B →
C2-A` as a success state; C3-C acceptance/rejection with the full decision trace;
`risk_classification: "elevated"`.
**Tests:** `p*` against the hand-computed table in `05` §2.1; `k ≥ 3.0` gate logged as
`NO VALID TRADE` when it fails; **C3-B ⊕ C2-A** on both `t_min` and pullback band; handoff preserves
entry and counts as one trade.
**Checkpoint:** PHASE-19 next.

**Landed:** `src/strategy/c3_transition.py`.

* **C3-A** — the only strategy that **does not use ACT**, and the only one declaring
  `expansion_seeking = True`. It arms a *directionless bracket* with both triggers live and no
  entry/stop/target published while pending, because a bracket has two candidate entries and one of
  them is wrong. The expected-payoff gate is `ATR_long / contraction_range ≥ 3.0`, computed from
  measured quantities **before any target is chosen** — so a loose contraction fails arithmetically
  rather than being rescued by a stretched target. Stop is the **opposite boundary**: full structural
  invalidation.
* **C3-B** — hard-partitioned from C2-A on `t_min` *and* on pullback depth, taking the contiguous
  `[0, 1.0)` ATR band below C2-A's `[1.0, 3.0]`. Both partitions derive from constants that already
  exist, so C3-B consumes **zero new parameters**. The crossing is read from the two VR horizons
  (`MR_VR_K2 > 1 ≥ MR_VR_K8`) rather than from stored history, keeping it a function of the snapshot.
  `HANDOFF C3-B -> C2-A` is recorded as **success, not invalidation**.
* **C3-C** — arms pending and lets acceptance or rejection assign the direction. Resolution is judged
  **relative to the side price approached from**, which is the bug worth naming: without that
  reference, "three closes below the boundary" reads as acceptance of a downside break even when
  price never went above it, leaving the strategy permanently short of every level above the market.
  The epistemic limit is published in the signal (`epistemic_caveat`): the stop-clustering premise is
  **inferred from structure, not measured** — there is no liquidation feed and no order flow — which
  is weaker footing than C1-A's funding cash flow.

---

### PHASE-19 — Class 4: C4-A, C4-B, C4-C + controls X1, X2
**Status:** ✅ **COMPLETED 2026-10-03** · **Specs:** `06`
**Create:** `src/strategies/{c4a,c4b,c4c}.py`, `src/strategies/controls/{c4x1,c4x2}.py`,
`tests/test_class4.py`
**Depends on:** PHASE-18
**Tasks:** C4-A Donchian + volatility-scaled trail, C4-B dual-timeframe momentum with ACT entry,
C4-C short-horizon reversal with a hard stop; **dual-mode** (benchmark never suppressed, suggestion
deduplicated); X1 random-entry across many seeds → a **distribution**; X2 weak-evidence control;
both `tradeable = False`.
**Tests:** benchmark mode bypasses arbitration; `suppressed_by` set in suggestion mode; **no API
path can return `tradeable = False`**; no external-literature performance figure anywhere.
**Checkpoint:** PHASE-20 next.

**Landed:** All five. Both controls landed 2026-09-29, firewalled three ways (`tradeable=False`, a
backtest-only assertion on every evaluation, and the runner's published-signal filter). **C4-A, C4-B
and C4-C** landed 2026-10-03 in `src/strategy/c4_established.py` at **zero new parameters**, as
specs/06 §8 requires.

* **C4-A** — Donchian breakout read from `STR_RANGEPOS`'s own detail so the channel a condition tests
  and the channel a level is placed at are the same two numbers. **No target at all**: trailed from
  entry, because capping a trend strategy's winner is the canonical way to destroy its expectancy.
  Exactly two filters, and the signal says so — specs/06 §4.1 warns that breakout systems are easy to
  over-filter into non-existence. The rejection record distinguishes "no breakout" from "a breakout
  against the daily trend", which would otherwise both read as the direction filter failing.
* **C4-B** — state-driven where C4-A is event-driven, which is what isolates whether an edge comes
  from the trend premise or from the breakout mechanic. ACT's **fourth consumer**. Volatility scaling
  is *declared* for the risk engine, never applied here: a strategy that pre-scaled its own size
  would be sized twice.
* **C4-C** — deliberately unconditioned, which is the point: C1-B is the conditioned version of the
  same observation, and gating C4-C would collapse the comparison. Its `EVALUATION_REQUIREMENTS`
  (max drawdown, worst trade, loss tail) travel **on every signal**, so the backtest cannot quietly
  report the flattering pair — specs/06 §13 criterion 6 rejects hit rate and Sharpe as sufficient.

**Dual-mode deduplication is `arbitration.merge_same_direction`, not a second rule inside the
strategies.** `_standing` gained a third component — bespoke outranks benchmark — because both are
Tier A and unvalidated on 1h, so without it `max()` kept whichever was evaluated first and the
benchmark would sometimes suppress the strategy it exists to measure. The suppression reason now
names the superseding **signal id**, which is what specs/06 §10's `suppressed_by` needs.

**`src/strategy/registry.py`** is the one list of what runs. It exists because the live runner and
the backtest harness must run the *same* strategies or every comparison between them is meaningless;
it validates ids, duplicates and the tradeable flag at construction, and `live_strategies()` never
returns a control.

---

### PHASE-20 — Arbitration pipeline and portfolio exposure
**Status:** ✅ **COMPLETED 2026-09-29** · **Specs:** `07` §3–§8; `13` §7
**Create:** `src/arbitration/{__init__,pipeline,conflict,merge,portfolio}.py`, `tests/test_arbitration.py`
**Depends on:** PHASE-19
**Tasks:** the six ordered stages as pure functions; conflict hierarchy (evidential standing →
timeframe seniority → suppress both); same-direction **merge with one risk allocation**;
Class 4 dedup **after** conflict and merge; rank within class only; `portfolio_concentration` with
`effective_independent_positions`.
**Tests:** stage order enforced (dedup after conflict+merge); **corroboration provably does not
change `strength`**; no cross-strategy composite score exists anywhere; both `risk_reward` and
`remaining_rr` published.
**Checkpoint:** PHASE-21 next.

**Landed:** The six-stage pipeline in the one correct order (dedup after conflict resolution, per `specs/07` §3). Opposing signals resolve on evidential standing then timeframe seniority, and **suppress both** on a tie. Same-direction signals merge to one risk allocation with strength explicitly non-additive.

---

### PHASE-21 — Risk engine: costs (shared), sizing, leverage, veto ladder
**Status:** ✅ **COMPLETED 2026-09-29** · **Specs:** `08`; `13` §8
**Create:** `src/risk/{__init__,engine,sizing,leverage,costs}.py`, `src/risk/vetoes/*.py`,
`tests/test_risk.py`
**Depends on:** PHASE-20
**Tasks:** `costs.py` **shared with backtest**; `size = min(size_stop, size_vol) × class × validation`
with `sigma_forward()` (**C3-A uses expanded ATR**); signed-weight correlation constraint;
`recommended_leverage` = the **minimum** that works; liquidation from live margin tiers; four veto
tiers with **all** vetoes evaluated; tick/lot rounding then `k_net` re-gated;
`min_equity_for_this_trade` on `MIN_NOTIONAL`.
**Tests:** levels byte-identical through the engine; vol-target binds on a synthetic C3-A;
long/short at high ρ treated as hedged; `recommended_leverage ≠ max_safe_leverage`; **no hardcoded
fee/tick/min_notional/margin** (grep the cost path); `k_net` recomputed after rounding.
**Checkpoint:** PHASE-22 next.

**Landed:** `Signal` **wraps** `RawSignal` and `assert_levels_unchanged` enforces it; all four veto tiers always evaluated and logged; `k_net` and the class floors (3.0 / 1.8) implemented as the derived constants the spec says they are, with the breakeven hit rate published on every signal; `sigma_forward` isolated in its own function so the C3-A substitution cannot be made by accident; funding is a veto via the same floor rather than an invented threshold.

---

### PHASE-22 — Audit trail and replay
**Status:** 🟡 **PARTIAL 2026-09-29** — the trail is written; **replay not started** · **Specs:** `10` §8; `13` §9–§10
**Create:** `src/audit/{__init__,builder,store,replay}.py`, `tests/test_audit_replay.py`
**Depends on:** PHASE-21
**Tasks:** `AuditBuilder` accumulating through the pipeline, written **atomically with the signal**;
`rejections` with 30-day full detail then daily condition-failure roll-up; `replay_exact` and
`replay_current`; `BAR_MUTATED` handling.
**Tests:** `replay_exact` reproduces a stored sample (runs in CI); `replay_current` produces a
readable diff after a deliberate version bump; roll-up still answers H1/H5/H10.
**Checkpoint:** engine complete end-to-end; PHASE-23 next.

**Landed:** Every evaluation persists atomically: signals, lifecycle events, the full decision trace, **rejections with all condition results**, arbitration events and setup state. Verified on real data — one pass wrote 6 rejections, each naming its binding condition. `replay_current` (specs/13 §10) is not implemented; the provenance it needs (`as_of`, `snapshot_id`, `code_version`, `parameter_hash`, `content_digest`) is already recorded, so it is additive rather than a redesign.

---

### PHASE-23 — Backend API part 1: services, envelope, JSON routes, bind guard
**Status:** ✅ **COMPLETED 2026-09-29** · **Specs:** `14` §1–§4, §9–§12
**Create:** `src/api/{app,envelope,cache,errors}.py`, `src/api/services/*.py`,
`src/api/routes/json.py`, `src/api/models/*.py`, `tests/test_api.py`
**Depends on:** PHASE-22
**Tasks:** one service layer; `FreshnessEnvelope` on every response with **data age, not cache age**;
five distinguishable system states; three signal detail levels; cursor pagination; localhost bind
with a token-or-refuse guard; `tradeable` filtering **at the service layer**.
**Tests:** cache reports data age; store failure → `DOWN`, never an empty list; quiet market vs
broken system distinguishable; non-localhost bind without a token refuses to start; controls never
returned.
**Checkpoint:** PHASE-24 next.

**Landed:** One service layer behind both renderings, the freshness envelope on every response, the bind guard as a startup failure, cursor pagination, and a cache that recomputes age from `data_as_of_ms` so it cannot report itself fresher than its data. A store failure renders **DOWN with a correlation id**, never an empty list.

---

### PHASE-24 — Backend API part 2: warm-up, events, rejections, fragments
**Status:** 🟡 **PARTIAL 2026-09-29** — routes, rejections and fragments landed; SSE events not started · **Specs:** `14` §5–§8
**Create:** `src/api/routes/fragments.py`, `src/web/templates/` (skeleton only)
**Depends on:** PHASE-23
**Tasks:** `/api/warmup` (per timeframe, per feature, per strategy, counting **backfilled** bars,
plus `earliest_tier_b_validation`); `/api/events?since=` cheap enough for 2–3 s polling;
`/api/assets/{asset}/rejections` with the **binding condition**; HTMX fragment endpoints calling
the same services.
**Tests:** every lifecycle transition appears exactly once in `/api/events`; warm-up counts
available bars not wall-clock; fragments and JSON share service methods.
**Checkpoint:** API complete; PHASE-25 is blocked — go to PHASE-26.

**Landed:** `/api/warmup`, `/api/assets/{asset}/rejections` and the HTMX fragments are live and call the same service methods the JSON routes do. `/api/events` polls rather than streaming; the browser-notification path of specs/14 §6 is not built.

---

### PHASE-25 — Dashboard ✅ UNBLOCKED (spec arrived 2026-09-27)
**Status:** 🟡 **PARTIAL 2026-09-29** — the honesty rules are implemented; charts not started · **Specs:** `15` (complete); `14` §4–§8; `10` §6
**Read:** `specs/15` in full, `specs/14` §4 (five system states) and §7 (rejections), `specs/07` §2
(no cross-class ranking — the reason for class-as-panel), `specs/09` §11 (nothing validated yet)
**Create:** `src/web/templates/` (base, status bar, action bar, asset strip, exposure strip, class
panel, signal card, detail view, warm-up), `src/web/static/` (one stylesheet with the §3 token set,
`lightweight-charts` vendored, a small HTMX helper for the 200 ms change-flash),
`tests/test_dashboard.py`
**Depends on:** PHASE-24 (fragment endpoints)
**Tasks:** implement §3's token system exactly (colour reserved for meaning only: long/short, `--act`
for `ENTER_NOW`, `--stale`, `--down`); tabular figures everywhere; class-as-panel / asset-as-row;
the price-position bar (stop — trigger — T1 with a live dot); **`ENTER_NOW` physically relocating to
the action bar** with position + size + the word "enter" before colour; honest-absence rendering for
`confidence: null`, `validated: false` (separate labelled sub-section with a dashed border and the
`earliest_tier_b_validation` date), warm-up progress, and empty panels showing the binding
condition; the exposure strip's plain-language concentration statement; the five degraded renderings;
§10's refresh cadences via `hx-trigger`; charts in the **detail view only**.
**Tests (all 14 from `15` §14):** no `<form>`, no POST target, no exchange trade link · no
`tradeable = False` rendered · `confidence: null` shows reason + breakeven, never blank or "—" ·
Tier B structurally separated · `ENTER_NOW` relocation snapshot · empty action bar and empty panels
render copy, never blank · healthy-vs-stale snapshot diff is **large** · stale + triggered still
shows stop and invalidation · warm-up counts backfilled bars · exposure statement appears with three
same-direction signals · `font-variant-numeric: tabular-nums` on every numeric element ·
`prefers-reduced-motion` suppresses the flash · **greyscale snapshot preserves every state
distinction**.
**Completion:** all 12 `15` §15 acceptance criteria met; **no build step** — templates and static
assets only.
**Risks:** the greyscale and no-affordance tests are the ones that actually prevent harm; write them
before the templates, not after.

**Landed:** All six specs/15 §1 failure modes are covered and tested: a quiet market reads differently from a blind one, `confidence: null` shows the breakeven hit rate in its place, unvalidated signals get their own labelled dashed section, `ENTER_NOW` relocates to the action bar with the word spelled out, the exposure strip states the correlation plainly, and stale is desaturated **and** relabelled with its age. lightweight-charts panels are not built.

---

### PHASE-26 — Backtest harness: driver wiring, fill model, metrics, protocol lock
**Status:** ✅ **COMPLETED 2026-10-03** (built and locked; **no results produced yet**) · **Specs:** `09` §1–§5; `16` Stage 2
**Create:** `src/backtest/{__init__,runner,fills,metrics,folds,protocol_lock}.py`,
`tests/test_backtest_fills.py`, `tests/test_metrics.py`
**Depends on:** PHASE-21 (shared costs), PHASE-22, PHASE-05 (holdout separation)
**Tasks:** walk-forward folds from **absolute UTC** boundaries with the **final 25% reserved** as
revision folds; fill model (entry at the worse of trigger/next open; **gap-through-stop fills at the
bar open**; intrabar stop-before-target); trade accounting (handoff = one trade); the full `09` §5
metric suite incl. Wilson intervals, R histogram, profit concentration with
strategy-appropriate reading; `protocol.lock.json` writer + hash.
**Tests:** gap-through-stop hand-computed (the `09` §3.2 worked example: stop 100, open 96 → −1.33R);
intrabar assumes stop first; handoff counts once; import graph proves **one** cost module.
**Checkpoint:** harness ready; PHASE-27 next (halt gate).

**Landed:** `src/backtest/{fills,accounting,metrics,protocol,runner,__main__}.py` and
`tests/test_backtest.py` (52 tests).

* **`fills.py`** — the four conservative rules, each unconditional so they cannot be softened on the
  bars where it matters: entry at the **worse** of {trigger, next 1m open} plus slippage; a stop that
  gaps fills at the bar **open**; a target fills at its price with no slippage and a favourable gap
  **not credited**; and one bar containing both levels resolves to the **stop, always**.
* **`accounting.py`** — a **handoff is one trade spanning two strategies**, with `attribute()` splitting
  R at the handoff price so the legs sum to the trade exactly. Counting it as two would distort both
  strategies' statistics in opposite directions. Non-trades are counted per kind and per strategy,
  because the rates are the diagnosis and the `suppressed` count is the only measure of what
  arbitration costs.
* **`metrics.py`** — hit rate never travels without its Wilson interval and `n`; mean **and** median R
  always; the R histogram on fixed buckets so two runs are comparable; profit concentration reported
  **with the reading its payoff shape implies**, because high concentration is correct for Class 2/3
  and a red flag for Class 1/C4-C. `risk_adjusted()` emits `sufficient: false` beside every Sharpe —
  in the payload, not only in the document. Groups under 20 trades report their count and **no
  statistics**.
* **`protocol.py`** — the lock file, and it refuses: an edited lock fails its own hash, a changed
  `parameter_hash` is refused outright, a relative or naive window boundary raises, an undeclared
  window cannot be run ad hoc, a lock cannot be silently overwritten, and `amend()` demands a reason
  and records whether the change came **after results**, which burns the holdout. No `git_commit`
  field — this is not a git checkout, so `code_version` carries a digest of `src/**/*.py`, which is
  strictly more precise about what ran.
* **`runner.py`** — drives the **production** `AppRunner` over history. `AppRunner.evaluate` was split
  so `evaluate_snapshot()` takes a snapshot the driver already built: a harness with its own copy of
  the two-pass ordering or the arbitration call would grade a different system than the one that runs.
  All forward-looking reads are confined to `_future_bars`, which is what makes the specs/09 §9
  look-ahead audit a reading rather than an investigation.
* **`__main__.py`** — `--preregister`, `--coverage`, `--window`, `--sensitivity` (the 2× slippage
  re-run), `--controls`, `--seeds`. The holdout is **not reachable**: `--window holdout` raises, and
  the flag that would read it is absent rather than present and defaulted off.

**The protocol is locked** at `validation/protocol.lock.json` (version 1, 38 declared trials, 5
pre-registered hypotheses). **Slippage is an assumption, not a measurement** — 5 bps pending P14 —
and every result says so; the 2× sensitivity run is what covers the error in it.

**⚠ The first run produced zero trades, and the reason is a decision rather than a bug.** The harness
drove 700 daily steps over the train window without a single step failure, and strategies armed
normally (C3-C, C4-B and others, 3–5 signals per step). **Every signal was then vetoed by the risk
engine on `DQ_STALE` + `DQ_META`**, so nothing reached a trade:

* **`DQ_STALE`** fires because no point-in-time source exists for a 2023 `as_of` — Tier B has no
  archive. For a Tier A strategy this is vetoing on the absence of a *recording*, not on a market
  condition.
* **`DQ_META`** fires because `instrument` is `None`: instrument metadata has no history either, so
  `tick` and `taker_fee` are unavailable and costs genuinely cannot be computed.

Both vetoes are *correct for live* and both are fatal to any historical backtest. Resolving them
decides **what the validation measures**, which is why it is recorded as **Q-N** rather than settled
here: a veto that could not be evaluated is not the same thing as a veto that fired, and treating the
first as the second makes the system unbacktestable, while treating it as "not fired" produces numbers
that overstate deployability unless the result says so on its face.

---

### PHASE-27 — Validation Stage 1: harness integrity ◀ HALT GATE
**Status:** NOT STARTED · **Specs:** `16` Stage 1; `09` §9
**Depends on:** PHASE-26
**Tasks:** permutation test (must **collapse to the control distribution**); C4-X2 must perform
poorly; backfill/live equivalence; `compute_series == compute_at` across the registry;
`replay_exact` sample; fee `< 0.01` assertion incl. the deliberate 100× error; gap-through-stop
check; shared-cost-module import graph; registry-wide look-ahead scan.
**Completion:** every check passes **before any strategy P&L is viewed** — verifiable by commit
ordering. Any failure halts the phase and is reported, not worked around.
**Checkpoint:** integrity proven; PHASE-28 next.

---

### PHASE-28 — Validation Stages 2–9: lock, feature analysis, controls, walk-forward, decisions
**Status:** NOT STARTED · **Specs:** `16` Stages 2–9; `09` §7–§12
**Depends on:** PHASE-27
**Tasks:** commit the lock (before any output); Stage 3 feature analysis (**halt gate**: H9 clusters,
C1-B cond 4, `er_min`, regime fit with all three validity tests); C4-X1 baseline distribution;
walk-forward on all folds **except reserved**; sensitivity sweeps incl. the **±20% rule**;
H1–H12 verdicts; benchmark comparisons with BH-FDR at q=0.10; **PROMOTE / REVISE / CUT** per
strategy, cuts removing code.
**Completion:** every red flag in `16` §3 explicitly checked; trial count updated honestly.
**Risks:** three strategies are *predicted* to be cut (C1-C, C3-C, C2-B) — that is a success.
**Checkpoint:** decisions recorded; PHASE-29 next.

---

### PHASE-29 — Validation Stages 10–12: holdout (once), calibration, report
**Status:** NOT STARTED · **Specs:** `16` Stages 10–12; `09` §15
**Depends on:** PHASE-28
**Tasks:** holdout **once**, after decisions are final, with the access logged; recalibrate
`class3_multiplier` and `liq_safety` from measurements; decide the ranking basis against the
confidence gate; fill `docs/validation_report.md` from the `16` §6 template.
**Completion:** report states plainly which strategies may enter shadow and which may not.
**Checkpoint:** PHASE-30 next.

---

### PHASE-30 — Shadow run tooling, then the 8–12 week run
**Status:** NOT STARTED · **Specs:** `17`
**Create:** `src/shadow/{parallel_replay,divergence,funnel,alerts}.py`, `tests/test_shadow.py`
**Depends on:** PHASE-29 (≥1 PROMOTE), PHASE-31 preferably first (24/7 host)
**Tasks:** weekly parallel replay with matched/live-only/replay-only sets; five divergence classes;
`ΔR` distribution per strategy; `missed_signal_rate` attributed by cause; the **signal funnel with
binding-condition breakdown**; alerts with **recorder heartbeat loudest**; the freeze rule.
**Tests:** replay reproduces live on a clean window; injected outage excluded; injected
non-determinism caught; funnel arithmetic balances; heartbeat fires within one cycle; window
duration cannot be extended mid-window.
**Note:** calendar-bound — minimum 4 weeks, target 8–12, ≥30 matched closed trades.
**Checkpoint:** go/no-go per strategy.

---

### PHASE-31 — Full Azure deployment and security hardening
**Status:** NOT STARTED · **Specs:** `18`; §3A above · **Partly delivered early by PHASE-02A**
**Create:** `deploy/azure/suggester-app.service`, `deploy/azure/nsg-rules.md`,
`docs/deployment.md` (the operator runbook)
**Depends on:** PHASE-02A (VM, recorder, backups, alerts already live), PHASE-24 (an app to deploy)
**Tasks:**
1. **Pre-push checklist** (`18` §1), including the **full git-history secret scan**
   (`git log -p --all | grep -iE '(api[-_]?key|secret|x-auth)'`) and confirming `data/` is untracked
   because it contains the holdout. If a secret was ever committed, **rotate first, rewrite history
   second** — in that order.
2. Deploy the app as a **second, independent** systemd unit (no `Requires=`, no `BindsTo=`): if the
   app crash-loops, the recorder keeps recording.
3. Keep the app bound to `127.0.0.1`; reach the dashboard over Tailscale/WireGuard or an SSH tunnel.
   **The NSG stays deny-all inbound** (AZ-6). Verify the bind guard refuses a non-localhost bind
   without a token.
4. `.env` at `0600`, owned by the service user, loaded via `EnvironmentFile` — even though it is
   currently empty (F-019), so the path exists if `conversions` is ever needed.
5. Extend backups to `state.db` and the holdout container (immutable, separate destination).
6. Verify chrony/PTP skew against P15's tolerance; assert `DQ_CLOCK` agrees.
7. Commit the lockfile; deploy installs from it, never resolves fresh. No CI/CD to production.
8. **All eight `18` §7 drills**, plus an Azure-specific ninth: a **host-maintenance reboot**
   (trigger a VM restart) → both units return on boot, gaps recorded, candles backfilled.
9. Recorder-heartbeat webhook enabled for hosted operation (`18` §10).
**Completion:** both processes run 24/7 and independently; the dashboard port is never open; every
drill recorded in `docs/findings.md` — including a **real key rotation** and a **real restore**.
**Risks:** drill 7 (rotation) is an architecture test as much as a security one — if rotation touches
anything beyond `.env`, the secret has leaked somewhere it should not be.

---

### PHASE-32 — Operations and iteration governance
**Status:** NOT STARTED · **Specs:** `19`
**Tasks:** `trial_count.json` append-only in the operations view; change classification A–E with
Type A defects named **before** the fix; 30-closed-trade cooling-off; drift checkpoints every 50
closed trades (two consecutive failures **demote, never tune**); runbooks; annual re-validation
against a fresh holdout; API-change drill.

---

## 5. Decisions

| ID | Decision | Rationale |
|---|---|---|
| **D-1** | Keep `src/cdx_http/` as the shared transport; `specs/10` §4's `src/http/` name is not used | A top-level `http` package shadows the stdlib and breaks `requests`. Already recorded in README/CLAUDE.md |
| **D-2** | `src/ingest/adapters/*` use `cdx_http` rather than a new `src/ingest/client.py` | `11` §3.2's requirements (per-host bucket, 429 halving, distinct 503 path, raw capture, redaction) are already implemented and tested. A second client would be a divergence risk |
| **D-3** | Existing `src/probes/` and `src/backfill/` stay as offline entry points | Matches `10` §3 (offline tools, same modules, different entry point). No rewrite |
| **D-4** | Phase-1 Parquet backfill is treated as an **immutable input artifact**; PHASE-05 imports it into SQLite, after which Parquet is regenerated **from** SQLite only | Reconciles the existing artifact with `11` §8.1's single-source-of-truth rule without discarding 1.3 GB of verified history |
| **D-5** | The recorder keeps its own `recorder.toml` and JSONL layout unchanged | `11` §1 Revision 2 makes it permanent and independent; changing it would risk the one irreplaceable dataset |
| **D-6** | `11` §5.1 step 1 (`skw != 0` → reject row) is **not implemented as written**. Rows are judged on `btST`/`bmST` age only | F-004: `skw` is one value shared by every fresh row and drifts over time. Rejecting on it would discard all live rows. CLAUDE.md invariant 3 already carries the corrected rule; `08` §8's `DQ_SKW` veto and `02` Family 10's `DQ_SKW` need the same correction |
| **D-7** | All seven `02` §1 PENDING branches are resolved by Phase-1 evidence (to be formally recorded in PHASE-01) | P1 → **≥3 years** on 1h ⇒ full walk-forward protocol (F-018). P11 → UTC-aligned ⇒ no boundary redefinition (F-012). P12 → aggregation exact, no omitted bars observed ⇒ 4h and futures 15m retained (F-013), with `known_absent_bars` still built as the safety mechanism. P6 → base-denominated ⇒ no conversion (F-010). P3 → all three spot markets exist (F-007). P8 → positioning public ⇒ **Family 7 and C2-C retained** (F-019). P10 → basis measurable at ~5σ ⇒ Family 6 basis retained, but ~4 bps vs 11.8 bps cost ⇒ consistency-check use only (F-023) |
| **D-8** | Futures also serve 15/30/240/480/3D/1M natively (F-008); `240` = native 4h | `02` §3's 4h-by-aggregation becomes a *spot-only* requirement. Aggregation still needed for spot 4h and is proven exact (F-013) |
| **D-9** | Slippage model built on the **1/2/5 bps** depth bands; wider bands reported as unmeasurable | F-025: 200 spot levels span only ±3.6 bps on BTC. `08` §7.1's "25 bps depth" veto cannot be computed for BTC/ETH and must degrade explicitly rather than silently |
| **D-10** | `DQ_CLOCK` tolerance ≥ ±3 s; spot-book responses get their own freshness gate | F-026: skew within ±2 s, but one spot book arrived 10.5 minutes stale with HTTP 200 |
| **D-11** | The registry has a **`judgment`** status, distinct from `provisional` | `specs/08` §11 registers `unvalidated_multiplier` as a permanent judgment — "it exists precisely because data is absent". Demanding it name a phase that will set it would be a false promise. It is still swept for sensitivity (`specs/09` §8) |
| **D-12** | Uncalibrated thresholds ship as **provisional band-midpoints**, never as bare numbers | `specs/02` §10 forbids setting thresholds in the *specs*; the system still needs values to run before Phase 16. `status` + `is_calibrated` + `parameter_hash` make the provisional nature explicit and auditable, so no number can be mistaken for a measured one |
| **D-13** | `parameter_hash` covers `(id, name, value)` only — **not** prose | Editing a justification must not invalidate a past signal's provenance; changing any value must. Tested both ways |
| **D-14** | Heavy validation (PHASE-26–29) runs **locally or on a temporarily resized VM**, not on the 24/7 host | AZ-9. Sizing an always-on VM for an occasional batch job is the expensive mistake |
| **D-15** | mypy is enforced on **new** code; the four Phase-1 packages are ignored and un-ignored by whichever phase next touches them | 78 pre-existing errors in code covered by 62 passing tests. Fixing them now is churn outside PHASE-01's scope; leaving them silently unchecked would be dishonest. The override list in `pyproject.toml` is the visible ledger |
| **D-17** | The **forming bar is always aggregated from closed finer bars** (1m by default), never read as a partial coarse bar | In live they are the same object; in a backtest the stored 1h bar at `as_of` is already complete, so reading it as "the bar so far" hands the strategy the rest of the hour. One code path for both drivers makes the `specs/10` §2.2 parity criterion structural rather than disciplinary |
| **D-18** | `FormingBar` exposes only the fields a trigger needs and **holds no `Bar`** | `specs/10` §2.1 asks for type separation, not a convention. A `FormingBar` cannot satisfy a feature function's `list[Bar]` signature, so the mistake is a type error instead of a silent look-ahead |
| **D-19** | `snapshot_id` hashes `(as_of, code_version, parameter_hash)` per spec; a separate **`content_digest`** hashes the data | Replay needs to distinguish "the code changed" from "a recorded bar mutated" (`specs/13` §9). Same id + different digest is exactly a bar mutation; the spec's single hash cannot express that |
| **D-20** | `code_version` is a digest of `src/**/*.py`, not a hand-maintained string | The repo is not a git checkout, so no commit sha is available, and a version someone must remember to bump is a version that will be wrong in the audit trail |
| **D-21** | The feature layer adds a **`PARAMETER_UNSET`** status beyond the six in `specs/12` §2 | `swing_atr_multiple` ships uncalibrated. Reporting that as `INSUFFICIENT_DATA` blames the data for a calibration nobody ran, and an operator would look in the wrong place. Using its placeholder `0.0` would make every local extreme a pivot |
| **D-22** | A combination a formula cannot support at a given lookback is **skipped and reported once at startup**, not computed and failed | `MR_VR` at k=8 needs more than 8 returns and the 1d short lookback is 7. Time cannot fix that, so an hourly `NUMERICAL_FAILURE` would be noise that trains the operator to ignore failures |
| **D-23** | Gaps are **recorded by the snapshot and adjudicated by the feature layer**, per window | `specs/12` §6 gates gaps per computation window with a different rule per timeframe. A single snapshot-level verdict would either disable a whole series for one old hole or hide a hole from the window that contains it |
| **D-24** | Cross-series windows use a **separate cache** from per-feature windows | Found by test: sharing one cache let a shallow entry satisfy a 720-bar feature, so a long-horizon statistic was quietly computed on 300 bars. Depth is part of a window's identity |
| **D-25** | mypy targets **Python 3.13** (the interpreter in use) rather than the 3.11 floor | numpy's shipped stubs use 3.12 `type` statements; targeting 3.11 makes mypy abort before checking any project code. ruff still enforces the 3.11 syntax floor for code we write |
| **D-26** | There is **one strategy registry** (`src/strategy/registry.py`), and the live runner and the backtest harness both read it | They must run the same strategies or every comparison between them is meaningless. A hardcoded list in two places is a divergence waiting to happen, and it would surface as a backtest result that cannot be reproduced live — the failure this project treats as worst, because nothing about it looks like an error |
| **D-27** | The monotone trail is **one shared implementation** (`strategy/evaluation.update_trail`), used by C2-A/B/C, C3-B and C4-A/B | Same reasoning as ACT having one implementation. A trail that loosens in one strategy and not another is a bug that shows up in exactly one backtest, and `effective_stop` is the load-bearing half: early in a trade the trail sits *behind* the structural stop, and taking it unconditionally would widen risk below the `R` the signal was sized against |
| **D-28** | Class 4's suggestion-mode deduplication is **arbitration's merge stage**, not a second rule inside the strategies | `specs/06` §3 describes dedup on (asset, direction) with the bespoke signal winning, which is exactly what `merge_same_direction` already does. A second implementation could disagree with the first. `_standing` gained a bespoke-over-benchmark tiebreak because both are Tier A and unvalidated on 1h, so without it `max()` kept whichever was evaluated first and **the benchmark would sometimes suppress the strategy it exists to measure** |
| **D-29** | `SetupNotReady` is a **`StrategyError` subclass carrying `problem_class = data_quality`** | "The strategy is fine and the inputs are not there yet" and "our code is wrong" demand opposite responses — wait for the recorder versus read the code. Reporting the first as the second is how a fresh deployment looks broken on its first evaluation, and how a genuine bug hides among fifty warming notices. Subclassing keeps condition bodies absorbing it exactly as before |
| **D-30** | Series staleness is checked **before** window depth, and the shortfall is named as its consequence | Both can be true at once, but `WARMING` reads as "the system is fine, give it time" and `EDGE_BEHIND` as "ingestion is dead". Reporting the reassuring one for a dead feed is how a stopped process goes unnoticed for a fortnight (F-035) |
| **D-31** | Bar content is compared **by value**, with the hash as a fast path | `str(Decimal("105")) != str(Decimal("105.0"))`, so hashing the strings made the same price at a different precision look like a revision of history — a false `BAR_MUTATION` at ERROR, which also prevented the two-fetch rule from ever firing. The venue is already known to send inconsistent precision (invariant 6). Comparing values also means rows written before the fix do not all read as mutations the first time they are re-fetched (F-037) |
| **D-32** | Backtest coverage reports a **Tier A fraction separately** from the strict one | Tier B has zero history, so every step of a historical window is "incomplete" for it — permanently and by design. A single `usable_fraction` reads 0% and makes a sound Tier A backtest look impossible, which is what the first real run reported (F-039). The Tier A *reasons* are shown in full because there are at most three and they are the actionable ones |
| **D-33** | The hosted dashboard reaches the engine through a **Cloudflare tunnel**, not an opened port | The owner wants the UI on Vercel, which is public, while `specs/18` requires the Azure network security group to stay **deny-all inbound**. A tunnel resolves the conflict rather than trading one requirement for the other: `cloudflared` dials *outward* from the VM, so the API keeps its `127.0.0.1` bind and no inbound port is ever opened. The alternatives were worse — exposing 443 makes the token the only barrier, and Tailscale would mean the dashboard could not be hosted at all |
| **D-34** | A declared tunnel makes the auth token **mandatory**, even on a loopback bind | The old guard demanded a token only for a non-loopback bind, because binding `127.0.0.1` *meant* unreachable. Behind a tunnel the process still binds loopback and is reachable from the internet, so that guard would have passed while the API sat open. `CT_SERVER__PUBLIC_VIA_TUNNEL` is the operator's declaration of what is in front of the process, because only they know. An unguessable tunnel URL is obscurity, not authentication |
| **D-35** | The Vercel app is a **server-side proxy**, never a browser calling the engine | The shared token is read inside the proxy route, which runs on Vercel's server, so it never reaches the browser — a `NEXT_PUBLIC_` variable would be compiled into the JavaScript a visitor downloads. Three consequences fall out: the engine needs no CORS, the engine can stay on loopback, and the proxy forwards **GET only against an explicit allow-list**, so a future engine endpoint cannot become publicly reachable by accident |
| **D-36** | The frontend duplicates the `specs/15` display rules, confined to one file and cross-checked from Python | Duplication is refused everywhere else in this project (one ACT, one trail, one cost model), and this is a real exception. It is accepted because the alternative is passing HTML strings across a network boundary into `dangerouslySetInnerHTML`, which cannot be type-checked at all. The mitigation is that it lives only in `frontend/lib/honesty.ts`, and `tests/test_frontend_contract.py` reads that file as text and asserts its thresholds and colour tokens match `src/api/render.py` — a check that runs in the suite that runs on every change, rather than in a Node toolchain that may not be installed |
| **D-37** | A fresh host **rebuilds price history from the API**; local data is never copied up | The development database is 5.8 GB, holds the holdout, and is gitignored for that reason. Copying it by hand is a step that can be done wrong silently. `python -m bootstrap` is idempotent by construction — the plan is recomputed from the database each run, so there is no completion marker to go stale — and chunks are fetched **newest-first** so an interrupted run leaves the *recent* history complete, which is the half the live system needs to run at all |
| **D-38** | A log file the process cannot write **degrades** it; it does not kill it | The first deployment died for this exact reason and crash-looped for hours (F-042). The console handler was working and journald had every line, so the process refused to run because it could not *write about* running. A failure to log is not a failure to work. It is announced at WARNING on every start, naming the path, `journalctl`, and the `chown` that restores the files — loud, but not fatal |
| **D-39** | The bootstrap's post-run check re-plans at the **same** `now_ms` the run used | Re-planning at the current clock moves the goalposts: a run lasting a minute ends with its window a minute longer, so the bar that closed *during* the run counts as missing. The first deployment reported "3 bars missing" on a complete run, and since the exit code follows that comparison, the command reported failure permanently. The honest question is "did I fetch what I set out to fetch" |
| **D-40** | Diagnosis on a host is a **script that walks the chain**, not a better error message | F-042's classification was correct at every layer and nobody read it, because the only message reaching the operator — "the engine did not answer" — was four layers downstream of a crash loop. `diagnose.sh` tests ownership, the token, each service *with its restart count* (a crash loop reads as `active` without it), the local API both ways, the tunnel's registration, data, disk and errors, in that order, so the first `FAIL` is the thing to fix |
| **D-41** | `--serve` runs uvicorn on a **worker thread** and keeps the evaluation loop on the main thread | The first version evaluated once and then blocked in `uvicorn.run()` forever, so a 72-hour run would have served one frozen evaluation (F-043). The thread *direction* matters: the writable sqlite connection is opened on the main thread and may only be used there, so evaluating on a worker would hit the cross-thread error `store/reader.py` exists to prevent |
| **D-42** | The API reports **its own** read connection's health, never the writer's | `AppRunner.health(include_store=False)` for request threads. A sync FastAPI handler runs in a worker, so querying the writer's connection raised a cross-thread error and the dashboard showed the store `DOWN` while the engine wrote fine (F-043). It is also the better number: the writer being healthy says nothing about whether a request can read |
| **D-43** | The Cloudflare tunnel is removed; the API is exposed directly behind its token | The owner could not get it working, and **an unmaintainable security control is not a security control**. The existing bind guard already made this safe — the app refuses to start on a non-loopback address without a token — so no new mechanism was needed. The cost is TLS: the token crosses the internet in clear. Acceptable here because the API reads only the local database, makes **no outbound calls**, holds no credential and cannot trade; the worst case of a stolen token is someone reading public prices (F-044). `HOSTING.md` documents the HTTPS upgrade for when that trade stops being acceptable |
| **D-44** | Refused requests are counted and logged **in aggregate**, never one line each | Opening a port without noticing who knocks is careless; one line per refusal lets a scanner fill the disk, which is a denial of service handed over free. A summary every 50 also makes the distinction visible that matters — background scanning is a handful from many addresses, token-guessing is a rising count from one |
| **D-45** | The dashboard proxy waits **20 s**, not 8 | The owner reported the dashboard flipping between connected and not connected while the engine was demonstrably healthy. A cold Vercel function plus a round trip to a B-series VM in another region can exceed 8 s with nothing wrong, so the timeout was manufacturing an outage. Still bounded: a dashboard that waits forever cannot be told apart from a dead engine, and the reader would assume the numbers on screen were current |
| **D-46** | A caller asking for something that does not exist is `BadRequest` / **HTTP 400**, not `ServiceUnavailable` / 503 | The problem class is what tells the operator where to look, so getting it wrong wastes the most time at the worst moment. A chart requesting a `2h` interval this system never stores would have been reported `infrastructure` with a 503 — sending someone to check the VM, the firewall and the service logs for a mistake in a query string. It is also the only error in the API that is not 503, so monitoring that counts 503s no longer records an outage for a bad parameter |
| **D-47** | The chart draws its own SVG; **no charting library** | A library would be fewer lines, but Node is not installed on the development machine, so its build cannot be verified here — and a chart that fails to compile on Vercel is worse than one written longhand. SVG also makes every candle a real DOM node, so the chart is inspectable and prints. Two properties it buys explicitly: candles positioned by **time** rather than index, so an outage leaves a visible hole instead of being closed up; and non-final bars drawn hollow, so a bar the feature layer refuses to compute on is never shown as settled |
| **D-48** | Published levels are rounded to the instrument tick in `build_signal`, **not** at the display boundary and **not** in the risk engine | The risk engine computes `k_net`, the breakeven hit rate and the R:R gate from these numbers, so rounding later would show a reader a price the arithmetic never used — a trade displayed as clearing its R:R floor might not, by a tick, silently. Rounding in the risk engine was refused because it may veto but never alters what a strategy published. `build_signal` is the moment a strategy's arithmetic *becomes* a published level, and is one place rather than fourteen (F-054) |
| **D-49** | `Levels.price_basis` is a field, not a sentence in `detail` | C1-C's payload has said "residual (spread) units, not price" since it was written, and the dashboard rendered `entry -0.0414` anyway for weeks. **Prose in a payload is advice; a field is a contract** — nothing can branch on a sentence. The same reasoning covers `pending_direction`, which C3-C already published and which was also ignored (F-051, F-053) |
| **D-50** | The API lifts `detail` to the top level of each signal rather than leaving it nested in `payload` | Three readers, two got the nesting wrong: `render.py` read `payload["detail"]` correctly while the Vercel dashboard read `signal.detail` and therefore read `undefined` on **every display rule**, silently. A shape that one reader in three gets right is the wrong shape, so it is flattened at the source instead of corrected in each reader. `payload` is still published for the audit path (F-056) |
| **D-51** | Ignore rules for the holdout are **anchored to the repository root** (`/data/`, not `data/`) | A bare `data/` matches a directory of that name at any depth, and silently excluded `frontend/app/data/` — so the commit that added the Data page added everything except it, through four stages that each reported success (F-057). The holdout is still covered, because it only ever lived at the root. A test now runs `git check-ignore` over every deployable file, because the next person to add a route called `data`, `logs`, `build` or `dist` will not be thinking about `.gitignore` |
| **D-52** | Each side of the deployment must be able to **state its own version**, and one must check the other's | Two sessions were spent debugging a VM that was working correctly, because every diagnostic asked "is the engine healthy" and none asked "is the engine the version the dashboard expects" (F-059). `diagnose.sh` §4b probes the routes the dashboard calls and §4c prints the running commit; `update.sh` refuses to let "already up to date" read as success. More careful deployment instructions were not the answer — the drift is the default state of a two-sided deployment, so it has to be observable rather than avoided |
| **D-53** | A display rule that depends on a **new** engine field also reads the old payload's evidence | The dashboard and the engine deploy separately, so a fix that needs both halves simultaneously is wrong for the entire window anyone is watching it — and stays wrong afterwards for every pre-upgrade row still in the signals table. `priceBasis()` prefers `price_basis` and falls back to `two_leg` / `levels_are`; `directionPending()` prefers `pending_direction` and falls back to `pending` / `entry_triggers` (F-058). The fallbacks are the case that actually occurs, not padding |
| **D-54** | Model filters key on the persisted `signals.strategy_class`; the API publishes the labels | The classification is written by each strategy and stored on every row, so a filter that re-derived it from a strategy id or a display string would be a second source of truth to keep in step. The API publishes `STRATEGY_CLASSES` with the canonical `specs/03`-`specs/06` titles, and the dashboard renders what it is given — the frontend's fallback list exists only for an engine too old to publish them (D-53's reasoning applied again) |
| **D-55** | Date grouping and display are both **UTC**, and the UI labels the zone | Bars are UTC-aligned (F-012) and every stored timestamp is epoch milliseconds. Grouping by the viewer's local day would move the boundary per reader, so a suggestion issued at 23:30 UTC would sit under different dates for two people and "look at the 10th" would stop being a shareable statement. Ranges are **half-open** `[start, start+86400000)`, so a suggestion at exactly midnight is counted once |
| **D-56** | A restart **re-evaluates** persisted setups rather than trusting them, and refuses six cases by name | specs/13 §11.1. Resuming a setup armed under a different `parameter_hash` or without its stored `context` would produce a plausible suggestion computed from two rule sets or from facts that were thrown away — worse than not resuming, because it looks like it worked. A refused setup is **not transitioned**: inventing a terminal state for a setup the process never observed would rewrite history it did not witness (F-062) |
| **D-57** | Analytics publishes **five** rates, each with its numerator, denominator and a prose definition; a zero denominator is `None` | "The hit rate" has two legitimate readings that differ by a lot — 3 of 5 resolved is 60%, 3 of 12 issued is 25% — so neither is called "the" rate and both are shown. The fraction travels with every figure so any number can be checked against a direct query. `0.0` on an empty sample would read as "nothing ever hit", a claim about the market; `None` is a claim about the sample and is the true one (F-064) |
| **D-58** | The five terminal states that are **not** a trade result are counted in their own bucket, never folded into a rate's denominator and never dropped | `specs/07` §8 has seven terminal states and only `TARGET_HIT`/`STOPPED` are a trade result. On the verification fixture the other five are *more than half* the suggestions. In the denominator they understate every strategy; dropped, they hide the operationally important cases — everything being `SUPPRESSED` by arbitration, or `MISSED` during an outage. The page shows the buckets summing to the total so none can be lost silently (F-064) |
| **D-59** | Analytics aggregates in SQL over a bounded window and the page does not poll | Four `GROUP BY` queries over an index range, ≤ 6 SELECTs per page (asserted through sqlite's trace callback), window capped at 366 days. Counts over whole days change when a suggestion resolves, not every fifteen seconds, so a timer would spend the VM's cycles redrawing identical numbers — and aggregating in the browser would also move the UTC day boundary into the viewer's timezone |
| **D-60** | The strategy class is published by `build_signal` from the strategy's own declaration, and the writer falls back to the registry rather than to `0` | A default of `0` satisfied a `NOT NULL` column that other code filters on, so three of four model filters matched nothing and nothing raised anywhere — the symptom appeared as chip counts that could not be reconciled against the list below them. **A default that is a valid-looking value for a filtered column is a bug with no symptom where it happens.** `strategy_class_of()` is the one derivation, and an unclassifiable id logs rather than storing a plausible lie (F-065) |
| **D-61** | The signal row's UPSERT updates every column that describes the signal's **current** state, and freezes only identity and provenance | `direction` was frozen while `entry`/`stop`/`targets` were updated, so a C3-C that armed pending as a placeholder LONG and resolved SHORT was stored as a long whose stop sat above its entry — the reported suggestion. `as_of_ms`, `snapshot_id` and the regime were frozen the same way, making the row claim a provenance its levels did not come from. `created_at_ms` and `armed_at_ms` stay frozen, because a suggestion must not migrate between date groups when its outcome is recorded (F-067) |
| **D-62** | Where a strategy publishes no fixed target, the UI shows the **live trail** rather than the word "none" | `specs/06` §4.1 mandates no T1 for the trend strategies *and* `trail_level` published live; `c2_participation.py` says "the UI shows the live trail instead". The trail was computed and published in the detail, never persisted and never displayed, so a correct design read as a half-built suggestion with no exit. Fixed at both ends rather than by manufacturing a target, which would contradict the one thing those specs are most emphatic about (F-068) |
| **D-63** | Suggestion timestamps display in **IST via a fixed +05:30 offset**; the UTC day grouping and every market-data timeline stay UTC | IST has no DST, so the arithmetic is exact and needs no browser timezone database; shifting the instant and reading `getUTC*` is viewer-independent, where `getHours()` would double-convert. The grouping stays UTC because moving it would change which suggestions fall in which group — a data change rather than a presentation one — so a late-UTC-day suggestion shows an IST date one day later, and the UI states that and offers the UTC reading on hover (F-069) |
| **D-64** | The Telegram notifier is a **separate read-only process**, not a hook in the evaluation path | `AuditTrail.record_evaluation` writes the signal row, its event, its audit trace and the arbitration events in one transaction, "together or not at all". An outbound HTTPS call inside it would let a slow Telegram delay the evaluation loop and a raised exception roll back the audit record for the whole pass — the suggestion engine must not be able to fail because a chat app is down. The notifier therefore opens `file:...?mode=ro` (SQLite refuses a write), runs as its own systemd unit with no `Requires=`, and keeps its cursor in its own JSON file rather than a new table. A test asserts no existing module imports it (F-070) |
| **D-65** | The notifier reads `/etc/crypto-trader/env` itself when a value is absent from its environment, with the real environment always winning | `EnvironmentFile=` is loaded by systemd when it starts a unit and by nothing else, so a hand-run `--check` saw none of the operator's settings and reported them unset (F-071). Reading the file as a *fallback* makes the command and the service agree; keeping the environment ahead of it means the running service never consults the file and its behaviour is unchanged |
| **D-16** | Every ruff ignore carries an inline reason | The ignores are deliberate patterns (long-lived recorder handles, pairwise `zip`, `Kind(str, Enum)`), not unfixed warnings. An unexplained ignore is indistinguishable from neglect |

---

## 6. Open questions

| ID | Question | Blocks | Why it cannot be resolved from the repo |
|---|---|---|---|
| ~~Q-A~~ | ~~`specs/15-dashboard.md` is missing.~~ **RESOLVED 2026-09-27** — the spec arrived and is complete (tokens, layout rationale, card, `ENTER_NOW` relocation, honest absence, degraded rendering, cadences, 14 tests, 12 acceptance criteria). PHASE-25 is unblocked and planned against it. | — | — |
| ~~Q-C~~ | ~~24/7 host?~~ **RESOLVED 2026-09-27** — **Azure**. Architecture, SKUs and rejections in §3A; delivery is PHASE-02A. | — | — |
| ~~Q-D~~ | ~~CLAUDE.md control panel wording.~~ **RESOLVED in PHASE-01** — the status table is now a phase lock pointing at this file. | — | — |
| ~~Q-E~~ | ~~CLAUDE.md invariant 12.~~ **RESOLVED in PHASE-01** — corrected to "nothing this project uses needs authentication" (F-019). | — | — |
| ~~Q-G~~ | ~~Re-run the 72 h acceptance test?~~ **SUPERSEDED** — PHASE-02A starts it on the Azure host, where continuous uptime is actually achievable. | — | — |
| **Q-B** | `specs/14-backend-api (1).md` is a **byte-identical duplicate** of `specs/14-backend-api.md`. Delete the duplicate? | Nothing; hygiene | Cannot know which filename is canonical |
| ~~Q-F~~ | **RESOLVED 2026-10-03** — the owner confirmed `risk_per_trade = 0.5%` and `portfolio_vol_target = 15%` annualised. Both stay `status = "user"` and are never swept. Recorded as Q10 in `docs/findings.md`. | — | — |
| **Q-H** | **Azure provisioning inputs** for PHASE-02A: (a) subscription to use, (b) region — Central India recommended, confirmed by measurement (AZ-7), (c) whether this is a **new free-tier account** (12 months of B1s free changes the cost picture), (d) an SSH public key, (e) budget-alert threshold (~$25/month suggested), (f) where the recorder-heartbeat webhook should post. | PHASE-02A | Account-level facts and spend authority |
| ~~Q-J~~ | **Merged into Q7** (`docs/findings.md`). The variance-ratio question was recorded twice under two ids; `docs/findings.md` is the project's memory per CLAUDE.md, so **Q7 is canonical** and this row exists only so a reader who knows the Q-J label is not left looking for it. | — | — |
| ~~Q-K~~ | **RESOLVED 2026-10-03** — the owner confirmed **k = 2** as the canonical `MR_VR(1h)`, and `specs/02` §5 Family 4 now states it. Recorded as Q9 in `docs/findings.md`. | — | — |
| **Q-N** | **The risk engine's data-integrity vetoes make a historical backtest produce zero trades.** `DQ_STALE` fires because Tier B has no archive before 2026-09; `DQ_META` fires because instrument metadata has no history, so `tick` and `taker_fee` are unavailable and costs cannot be computed. Both are correct live. Options: (a) **treat an unevaluable veto as "not fired" in backtest mode only**, recording it as `unavailable` on every signal — honest about what was and was not checked, and the mechanism already exists (`VetoResult.unavailable`); (b) **seed historical instrument metadata from the earliest recorded row** and declare it on every result — fees change rarely, but a fee change inside the window would bias every `k_net`, so the declaration is load-bearing; (c) both, which is my recommendation: (b) makes costs computable at all, (a) stops Tier B's absence vetoing Tier A. Whichever is chosen, the result must say on its face which vetoes were never evaluated, or the numbers will read as more deployable than they are. | **PHASE-27/28** — no trades means no validation | A decision about what the validation is permitted to measure, not a fact in the repo |
| **Q-L** | **`lookback_1h_short = 24` cannot host swings at the horizon's own scale** (F-038, recorded as Q11 in `docs/findings.md`). Pivot availability on a 24-bar window caps θ at ~1.0, but median swing spacing only reaches the derived 12-bar target around θ ≈ 2.0, where availability is 0.09. With `k = 3` confirmation, 24 bars is too short for 12-bar swings, so C2-A's structural stop necessarily references smaller swings than its premise describes. Options: (a) accept it — θ = 0.75 is the availability-respecting end, and this is why θ is `provisional`; (b) read `STR_SWING` at `1h/medium` for the structural stop, which costs no parameter and whose spacing *does* respond to θ; (c) change `lookback_1h_short`, which alters every 1h feature and both regime axes. | Nothing — (a) is in force and the system runs | Whether C2-A's stop should read the medium window, and whether the shared short lookback is right for structure at all |
| **Q-M** | **Tier B strategies cannot be backtested.** C1-A and C2-C read funding, basis and positioning, which are point-in-time with no archive before 2026-09. `specs/09` §13 anticipates this and sets a separate Tier B timeline, but it means two of twelve strategies reach shadow with **no historical evidence whatsoever** — not weak evidence, none. Accept that, or hold them out of shadow until the live archive is long enough to test them? | PHASE-29/PHASE-30 scope | A judgement about how much absence of evidence is acceptable before a strategy is suggested to a person |
| **Q-I** | When Azure takes over recording, **stop the laptop recorder** and keep its archive as a separate labelled dataset? Two hosts writing independent archives would produce divergent histories that are awkward to merge later. | PHASE-02A cutover | An operational preference |

---

## 7. Validation status

**Test suite:** `88 passed` (`.venv/Scripts/python -m pytest -q`, 2026-09-27). No failures, no skips.
**Lint:** `ruff check src tests` → clean (every ignore annotated, D-16).
**Types:** `mypy` → clean across 34 source files; the four Phase-1 packages are explicitly ignored
per D-15 and un-ignored as later phases touch them.

| Area | Evidence |
|---|---|
| Probes P1–P17 | All run; results in `data/probe_results/`, conclusions in `docs/findings.md` F-001…F-028. P14 partially met (7 rounds vs ~20); P8 part B awaits a key |
| Recorder acceptance | 6 of 7 criteria met; criterion 1 (72 h unattended) **not met** — host, not code (F-024) |
| Backfill | 42/42 series verified sorted+unique; ≥99.7% complete after continuous start; 14,900 requests, 0 failures (F-018) |
| Storage/compression | 42 gz files sha256-verified; ratio 5.3×; ~71 MB/month on disk (F-028) |
| Known defects | None open. Fixed during Phase 1: stale-STOP handling, silent sampler deaths (now logged), band-depth truncation, 3D gap arithmetic, misleading `skw` quality counter |

---

## 8. Completed work (chronological)

| Date (UTC) | Work |
|---|---|
| 2026-09-18 | Repo scaffold; `cdx_http` (client, backoff, ratelimit, jsonutil, signed); recorder (storage, tasks, runner); 8 real fixtures; F-001…F-006. Corrections: spot `depth` lowercase (F-001), `skw` is not a health flag (F-004) |
| 2026-09-19 | Gzip compression of completed days + single-instance lock + STOP file; P1–P17 executed; P8 found positioning **public** → S2 enabled; P2 found the 5,000/60 s budget; outage test passed; backfill of all 42 series; F-007…F-023 |
| 2026-09-19→23 | Recorder ran with heavy host downtime (12.1 h of 103 h); candle gap-fill covered every gap losslessly |
| 2026-09-27 | Recorder restarted (compressing 14 stale day files at startup); P13–P16 + storage analysis recorded as F-024…F-028; **all specs 02–19 + HANDOVER read**; this plan written |
| 2026-09-29 | **The vertical slice closed.** Tier B families + raw-archive import (5,741 real payloads, zero unexplained rejections), regime engine with the escalation and degraded mode (F-033), strategy framework + ACT under Hypothesis, C1-A/C1-B/both controls, arbitration, the risk engine with `k_net` and the four veto tiers, the audit trail, the service layer, the HTTP boundary and the dashboard. Findings F-032 and F-033. Two real bugs caught by their own tests: an O(n²) basis alignment, and a SQLite connection shared across threads that would have taken the dashboard down in production. 366 -> 551 tests |
| 2026-09-28 | **PHASE-02…PHASE-11 implemented locally** (owner deferred Azure). Canonical models + normalizers for every endpoint (hazards 1-7), SQLite store with migrations/two-fetch finalization/point-in-time reads/holdout guard, 20.3 M backfill rows imported (6.2 GB), Parquet export, ingest client + scheduler + DQ monitor, gap detection and refusing aggregation, `MarketSnapshot` + Live/Backtest drivers, feature engine + Families 1-4 (20 features, 139 values per asset). Findings F-031 (variance-ratio formula). 88 -> 366 tests |
| 2026-09-27 (later) | `specs/15-dashboard.md` received and read → PHASE-25 unblocked. **Azure chosen as the deployment target**; §3A architecture and AZ-1…AZ-10 recorded; PHASE-02A inserted, PHASE-31 rewritten. **PHASE-01 completed**: F-029 (seven branch resolutions), F-030 (`specs/03` amendment), 30-parameter registry + config layer + trial counter, CLAUDE.md corrections, ruff/mypy configured. 62 → 88 tests |

---

## 9. Changed files

### Analysis session (earlier today)
`docs/findings.md` (F-023…F-028) · `latest-updates.md` (new) · `data/raw/**` (recorder restarted,
14 day files compressed) · `data/probe_results/` (storage + analysis results). No source changed.

### PHASE-01 (this session)

| File | Change |
|---|---|
| `config/parameters.toml` | **New** — the 30-parameter registry |
| `src/config/{__init__,parameters,settings}.py` | **New** — registry loader with rule enforcement, `parameter_hash`, Azure-aware settings, bind guard |
| `tests/test_config.py` | **New** — 26 tests (budget, calibration status, hash semantics, rule violations, settings) |
| `validation/trial_count.json` | **New** — append-only cumulative trial counter |
| `docs/findings.md` | Added **F-029** (seven branch resolutions) and **F-030** (`specs/03` amendment) |
| `specs/03-class1-correction.md` | C1-C **condition 7** added per `specs/04` §7 |
| `CLAUDE.md` | Phase-lock status table + pointer to this file; **invariant 12 corrected**; Azure noted |
| `pyproject.toml` | All `specs/10` §6 dependencies; ruff + mypy configured with annotated exemptions |
| `.gitignore` | `specs/18` §1 entries; explicit note that `validation/*.json` must be committed |
| `src/probes/p12_aggregation.py`, `src/recorder/tasks.py` | Two lint nits fixed (dead local, `dict()` → literal); behaviour unchanged, tests green |
| `tests/test_backfill.py`, `tests/test_storage_compression.py`, `tests/test_recorder*.py` | Ruff-safe fixes (import order, `datetime.UTC`); two over-broad `pytest.raises(Exception)` narrowed |

---

### PHASE-02…PHASE-11 (local build, 2026-09-28)

| File | Change |
|---|---|
| `src/core/{errors,logging,health,version}.py` | **New** — error taxonomy with `ProblemClass` (application / configuration / infrastructure / external / data-quality), redacting JSON logger, health report, code-version digest |
| `src/normalize/` | **New** — `canonical.py` (frozen Pydantic models, `Fee` with no bare constructor, ms validation) and one normalizer per endpoint, each rejecting rather than coercing |
| `src/store/` | **New** — `migrations.py` (2 migrations, 17 tables), `sqlite_store.py` (WAL, every sqlite failure classified, two-fetch finalization, bar-mutation detection, `as_of` reads with a look-ahead assertion), `import_backfill.py`, `parquet_export.py`, `holdout.py` (single-use guard, access log, burn marker) |
| `src/ingest/` | **New** — `client.py`, `adapters.py`, `scheduler.py` (priorities, per-class backoff, non-retryable stops rather than loops, budget shedding), `dq.py`, `pipeline.py` |
| `src/pipeline/` | **New** — `gaps.py` (absence confirmation, aggregation that **refuses** across an unconfirmed gap), `drivers.py` (Live + Backtest) |
| `src/snapshot/` | **New** — `models.py` (immutable snapshot, `FormingBar` type-separated, refusals carry reasons), `builder.py` (warm-up + edge + gap accounting, Tier B freshness) |
| `src/features/` | **New** — `value.py`, `convert.py` (the Decimal/float boundary, rounding direction per `specs/08` §10), `numerics.py` (every `specs/12` §4 guard), `windows.py` (§6 gap rules, 1m synthesis), `spec.py`, `registry.py`, `engine.py`, `f1_trend.py`, `f2_volatility.py`, `f3_structure.py`, `f4_deviation.py` |
| `tests/` | **New** — `test_canonical.py`, `test_normalize_hazards.py`, `test_store.py`, `test_store_export_holdout.py`, `test_ingest.py`, `test_gaps_aggregation.py`, `test_snapshot.py`, `test_features_core.py`, `test_features_families.py`, `test_features_structure_deviation.py` (+5 real fixtures) |
| `docs/findings.md` | **F-031** — the variance-ratio double division, with measurements; Q7 added |
| `pyproject.toml` | mypy targets 3.13 (D-25) |

### PHASE-12…PHASE-25 (the vertical slice, 2026-09-29)

| File | Change |
|---|---|
| `src/features/f5_cross.py`, `f9_volume.py` | **New** — Families 5 and 9; cross-asset series aligned by timestamp |
| `src/features/f6_funding.py`, `f7_positioning.py`, `f8_liquidity.py` | **New** — Tier B, point-in-time (`@pit`), each carrying its own caveat into every value |
| `src/store/import_raw.py`, `src/store/reader.py` | **New** — the recorder archive into SQLite; a thread-safe read-only view for the API |
| `src/regime/{model,fit,classifier}.py` | **New** — offline fit, three validity tests, 3×3 → 2×2 → degraded escalation, live classifier |
| `src/strategy/{act,conditions,base,lifecycle,evaluation,c1_correction,controls}.py` | **New** — ACT (one implementation), conditions with no short-circuiting, the lifecycle machine, C1-A, C1-B, C4-X1, C4-X2 |
| `src/risk/engine.py` | **New** — wrapping (never rewriting), `k_net` + class floors, four veto tiers, `sigma_forward` isolated |
| `src/arbitration/pipeline.py` | **New** — six stages in the one correct order |
| `src/audit/trail.py` | **New** — atomic per-evaluation record, rejections included |
| `src/api/{services,app,render}.py` | **New** — one service layer, the freshness envelope, the bind guard, the dashboard's six honesty rules |
| `src/app/{runner,__main__}.py` | **New** — the evaluation pass, loud preflight, `--check`/`--once`/`--serve` |
| `src/core/errors.py` | Added `SnapshotError`, `SnapshotIncomplete`, `ClockError`, `ProblemClass.MODELLING` |
| `config/regime_model_B-{BTC,ETH,SOL}_USDT.json` | **New** — fitted artifacts, two of them degraded and saying so |
| `docs/findings.md` | **F-031** (variance ratio), **F-032** (book depth), **F-033** (regime validity); Q7, Q8 |
| `tests/` | **New** — `test_features_{cross_volume,tierb}.py`, `test_regime.py`, `test_strategy_framework.py`, `test_risk_arbitration.py`, `test_runner_audit.py`, `test_api.py` |

### PHASE-16…PHASE-19, PHASE-26, PHASE-27 (2026-10-03)

| Path | Change |
|---|---|
| `src/strategy/c1c_relative.py` | **New** — C1-C, two-leg relative value, ACT on the residual with sigma from the variance decomposition |
| `src/strategy/c2_participation.py` | **New** — C2-A/B/C, shared pullback and structure bodies, the one-position constraint, `HANDOFF C2-C -> C1-A`, `assert_c1a_partition` |
| `src/strategy/c3_transition.py` | **New** — C3-A (directionless bracket, no ACT, `expansion_seeking`), C3-B (`[0,1.0)` band, handoff as success), C3-C (acceptance/rejection relative to the approach side) |
| `src/strategy/c4_established.py` | **New** — C4-A/B/C at zero new parameters, the `specs/06` §10 signal additions, C4-C's evaluation requirements on every signal |
| `src/strategy/registry.py` | **New** — the one list of what runs; validates ids, duplicates and the tradeable flag at construction (D-26) |
| `src/strategy/market_view.py` | **New** — the cross-asset view for C1-C and C2-B, built once per pass so peers are read at the same `as_of` |
| `src/strategy/evaluation.py` | Added the shared `update_trail` / `effective_stop` (D-27); ACT's data gates reclassified to `SetupNotReady` / `ConfigError` (D-29) |
| `src/backtest/{fills,accounting,metrics,protocol,runner,__main__}.py` | **New** — the whole PHASE-26 harness |
| `src/ingest/__main__.py` | **New** — `python -m ingest`, the missing process that keeps SQLite current (F-035) |
| `src/ingest/pipeline.py` | Added `poll_settle`, the finalization sweep, and one `settle:` task per series at P2 (F-036) |
| `src/probes/swing_calibration.py` | **New** — the structural θ calibration, with the criteria conflict reported rather than averaged away (F-038) |
| `src/store/sqlite_store.py` | Added `non_final_bar_times`; `canonical_decimal` + `same_content` so precision is not a mutation (D-31, F-037) |
| `src/snapshot/builder.py` | Staleness checked before depth (D-30, F-035) |
| `src/pipeline/drivers.py` | Coverage split by tier, Tier A reasons reported in full (D-32, F-039) |
| `src/app/runner.py` | `evaluate_snapshot()` split out for the harness; `strategy_gates` separated from `strategy_errors` |
| `src/arbitration/pipeline.py` | `_standing` gained the bespoke-over-benchmark tiebreak; suppression names the superseding signal id (D-28) |
| `src/features/f3_structure.py` | `MIN_PIVOT_BARS` declared on `STR_SWING`/`STR_SEQ`, so four impossible combinations are skipped rather than failed per bar (F-038) |
| `src/core/errors.py` | Added `SetupNotReady` (D-29) |
| `config/parameters.toml` | `swing_atr_multiple` = **0.75, provisional**, with the measured band. **Zero `unset` parameters remain** |
| `validation/protocol.lock.json` | **New** — the pre-registration, version 1, 38 declared trials, 5 hypotheses |
| `docs/findings.md` | **F-035** (stale edge reported as warming), **F-036** (non-final history invisible), **F-037** (precision as mutation), **F-038** (θ calibration + the criteria conflict), **F-039** (coverage by tier); Q11 |
| `tests/` | **New** — `test_strategies.py` (62), `test_backtest.py` (52), `test_swing_calibration.py` (13); extended `test_store.py`, `test_ingest.py`, `test_snapshot.py`, `test_risk_arbitration.py` |

### Hosting (2026-10-04)

| Path | Change |
|---|---|
| `HOSTING.md` | **New** — the beginner-level, step-by-step hosting guide: Azure VM, GitHub deploy key, the two services, the tunnel, Vercel, and what to watch during the 72-hour run |
| `src/bootstrap/{plan,fetch,__main__}.py` | **New** — rebuild price history from the venue into SQLite. Idempotent, resumable, newest-first (D-37) |
| `frontend/` | **New** — the Next.js dashboard for Vercel. Server-side proxy, allow-listed GET only, token never in the browser (D-35) |
| `deploy/azure/` | **New** — three systemd units (ingest / app / tunnel, deliberately independent), `env.example`, `tunnel-env.example`, `nsg-rules.md`, `update.sh`, `health-check.sh` |
| `src/config/settings.py`, `src/api/app.py` | `public_via_tunnel` guard (D-34); the token comparison is now constant-time |
| `src/core/errors.py` | Added `BootstrapError` (`external_dependency`, retryable) |
| `.gitignore` | Frontend build output, `node_modules`, `.vercel`, `.env.local` |
| `tests/` | **New** — `test_bootstrap.py` (19), `test_frontend_contract.py` (12); extended `test_api.py` with the tunnel-guard tests |

### Deployment fixes (2026-10-10)

| Path | Change |
|---|---|
| `BUG_SOLVE.md` | **New** — the plain-language recovery guide for the first deployment's failure, written for a non-technical reader |
| `src/core/logging.py` | File logging degrades instead of raising (D-38) — the fix for the crash loop |
| `src/bootstrap/__main__.py` | The post-run check uses the run's own `now_ms` (D-39) |
| `deploy/azure/diagnose.sh` | **New** — walks the whole chain and names the first broken link (D-40) |
| `deploy/azure/update.sh` | Takes root and steps down to `crypto` for Git and pip; re-asserts ownership every run |
| `deploy/azure/*.sh` | Committed `100755`; `core.filemode=false` on Windows had swallowed the `chmod` |
| `HOSTING.md` | The `--profile full` command carries its `sudo -u crypto` prefix; the ownership rule is stated explicitly; the Vercel redeploy requirement is in the step, not only in troubleshooting |
| `tests/` | Four tests: logging degrades and still announces itself, a writable directory still gets its file handler, the bootstrap's window does not move, and the CLI exits 0 on a complete run under an advancing clock |
| `docs/findings.md` | **F-042** — the deployment, the four defects, and what the diagnosis layer got wrong |

### Direct exposure and the serve-loop fix (2026-10-10)

| Path | Change |
|---|---|
| `src/app/__main__.py` | `_serve()` — uvicorn on a worker thread, the evaluation loop on the main thread, failures survived, clean shutdown (D-41). A thread-safe `api_health` source (D-42) |
| `src/app/runner.py` | `health(include_store=False)` for request threads (D-42) |
| `src/api/app.py` | Aggregate logging of refused requests (D-44) |
| `deploy/azure/` | `crypto-tunnel.service`, `tunnel-env.example` and `health-check.sh` **deleted**; `env.example` and `nsg-rules.md` rewritten for a direct bind; `diagnose.sh` section 5 now asks the kernel what is listening |
| `HOSTING.md` | Step 7 is the firewall rule rather than the tunnel; Vercel's base URL is `http://<ip>:8000`; a DuckDNS + Caddy HTTPS appendix |
| `BUG_SOLVE.md` | Rewritten for round 2: the switchover, what the exposure costs, and the daily checks |
| `tests/` | Five tests: a public bind without a token refuses to start, refusals are counted and summarised, `health` can omit the writer's store, the serve loop keeps evaluating, and it survives both a classified and an unclassified failure |
| `docs/findings.md` | **F-043** (the frozen serve loop and the health thread bug), **F-044** (removing the tunnel, and what the exposure does and does not risk) |

### The chart page, and five dashboard defects (2026-10-10)

**Why this session exists.** The owner could not tell from the dashboard whether data was arriving.
That turned out to be literally true rather than a matter of presentation: the dashboard's default
for anything it did not recognise was *everything is fine*. Five defects, four of them mine, all in
the layer between the engine and the reader — the engine itself was healthy throughout.

| Path | Change |
|---|---|
| `frontend/app/chart/page.tsx` | **New** — the Chart page. BTC/ETH/SOL and 1m/15m/1h/4h/1d, live at 15 s, with the freshness envelope, `drawn` vs `stored_total`, `pending_confirmation`, and four distinct failure states (unreachable / wrong shape / warming / genuinely empty) |
| `frontend/components/CandleChart.tsx` | **New** — SVG candlesticks, no charting library (D-47). Positioned by **time**, so a gap stays a gap; non-final bars hollow and dashed; non-finite prices dropped and **counted on screen**; crosshair with OHLCV |
| `frontend/components/Nav.tsx` | **New** — the left-hand page nav. Active state marked by border and weight, not colour alone |
| `frontend/app/layout.tsx`, `app/globals.css` | The shell: nav + content, `min-width: 0` so a wide chart cannot push the nav off, segmented controls, and a 720 px breakpoint that moves the nav on top |
| `frontend/lib/honesty.ts` | `panelState` **requires a freshness envelope** (F-050). A JSON object without one is `down`, not `ready` |
| `frontend/lib/api.ts` | An unparseable body is a **transport failure** naming the HTTP status, not a null payload (F-047). Added `candles()` |
| `frontend/app/page.tsx` | The signal accessor fixed and guarded (F-048); `HealthSection` classifies the health reading into four outcomes before rendering it (F-047) |
| `frontend/components/Panels.tsx` | `HealthPanel` separates three nothings — no payload, wrong shape, empty list — each with its own remediation (F-047) |
| `frontend/app/api/proxy/[...path]/route.ts` | Wraps a non-2xx body that is not one of the engine's own errors, naming the status, the path and the first 200 bytes (F-050). `api/candles/` allow-listed. Timeout 8 s → **20 s** (D-45) |
| `src/api/services.py` | `MarketService.candles()`; `SignalService.live()` falls back to the newest stored bar's close and reports `freshness_basis` (F-045) |
| `src/api/app.py` | `GET /api/candles/{asset}`; `status_code_for` returns **400** for `BadRequest` (D-46) |
| `src/core/errors.py` | Added `BadRequest` (`configuration`) — a caller asking for an interval that does not exist is not an outage (D-46) |
| `src/ingest/pipeline.py` | The spot poll's row cap is derived from the span requested, so a gap wider than 30 bars is repaired instead of being permanent (F-049) |
| `src/ingest/__main__.py` | `_fill_history()` runs the idempotent bootstrap plan at startup (F-046) |
| `src/snapshot/request.py` | **New** — `build_request()` centralised, so ingest and bootstrap no longer import the API app to find out what to fetch |
| `BUG_SOLVE.md` | Rewritten for round 3: what the calm screen actually meant, the `CT_API_BASE` check, the Chart page as the data check, and the five-step escalation |
| `tests/` | 25 tests: ten on `/api/candles` (ordering, decimal strings, non-final flagging, close-based age, clamping, empty, HTTP, 400), three on the freshness fallback, two on spot gap repair, and ten contract tests reading the TypeScript as text |
| `docs/findings.md` | **F-045** … **F-050** |

**Answering the owner's two direct questions.**

1. *Does it fetch missing history by itself, or must a command be run?* **By itself, no command.**
   Verified: empty database → `history_incomplete missing_bars=17580` → `history_filled requests=15`
   in ~20 s → second run `history_complete` with no requests. Wipe `data/state.db`, restart
   `crypto-ingest`, and recent history rebuilds and then stays current.
2. *Is "sometimes connected, sometimes not" an error?* **It was real and it was not the VM.** An 8 s
   proxy timeout could not cover a cold Vercel function plus a round trip to a B-series VM in another
   region, so a healthy engine read as intermittent. Now 20 s — still bounded, because a dashboard
   that waits forever cannot be distinguished from a dead engine.

**What is still not fixed, and cannot be here.** Node is not installed on the development machine, so
the TypeScript cannot be compiled or executed. The contract tests read it as text — they catch an
accessor or threshold diverging from the Python, not a type error. **The first Vercel build is still
where a TypeScript mistake would surface.** This is the one standing limitation of the frontend work
and it has now caused one real defect (F-048).

### The data page, and six display defects (2026-10-11)

**The engine was right about everything and the dashboard was wrong about most of it.** Every defect
this round sat in the boundary between a correct payload and a renderer that assumed its shape. The
engine had published `price_basis`, `pending_direction`, `execution_note` and a leg table; the
dashboard read none of them, and showed a spread as a price, a placeholder as a recommendation, and a
healthy feed as too stale to trade.

| Path | Change |
|---|---|
| `frontend/app/data/page.tsx` | **New** — the Data page: every source, its row count, when it last delivered and the last value it delivered. Series lateness in **intervals**, Tier A/B permanence stated per source, data-quality counts on the same screen because fresh is not correct |
| `src/api/services.py` | `MarketService.sources()` — series enumerated **from the data** so a series we think we record but do not is simply absent; `rows_last_hour` **measured, not configured**; per-source error isolation; `dq` events by kind. `SignalService.live()` publishes `market_data` separately (F-052). `_row_to_signal` lifts `detail` to the top level (F-056) |
| `src/api/app.py` | `GET /api/sources`, which takes **no parameters at all** — it is what someone opens before they know what is wrong |
| `src/strategy/evaluation.py` | `Levels.price_basis`; `publishable_levels()` rounds published levels to the instrument's tick at the moment they become published, before the risk engine computes `k_net` from them (F-054, D-48) |
| `src/features/convert.py` | `quantize_price()` — the rounding rule on a Decimal, with `to_price` delegating, so specs/08 §10 has one implementation and no float round-trip |
| `src/strategy/c1c_relative.py` | Declares `price_basis=RESIDUAL_BASIS`. Its `detail` had said so in prose since it was written and was ignored for weeks — prose is advice, a field is a contract (F-051) |
| `frontend/components/Panels.tsx` | `SignalCard` renders a pending setup as **DIRECTION NOT YET DECIDED** with the level it watches, and a spread as its **leg table** with the levels row suppressed; `ExposureStrip` counts directional, spread and pending separately; `ActionBar` refuses `at —` (F-051, F-053) |
| `frontend/lib/honesty.ts` | `signalDetail()` — one accessor, with a `payload.detail` fallback for an engine a few commits behind (F-056) |
| `frontend/app/page.tsx` | Three ages, each labelled for what it is, with only market data judged against the staleness thresholds (F-052) |
| `frontend/app/api/proxy/[...path]/route.ts` | The 404 message leads with **version skew** and states the discriminator; `api/sources` allow-listed (F-055) |
| `frontend/components/Nav.tsx`, `app/globals.css` | The Data page in the nav; dense `.data-table` styles |
| `src/api/render.py` | The **same** two fixes on the VM's own dashboard: a pending setup renders as "direction not yet decided", a spread renders its legs with no `entry` heading, the action bar refuses an undecided entry, and the exposure strip counts directional only. Both dashboards claim the same specs/15 rules, so a fix in one is half a fix |
| `tests/` | 34 tests: eight on tick rounding (hand-computed in both directions, residual skipped, missing metadata not guessed), nine on `/api/sources`, two on the freshness split, ten contract tests on the display rules |
| `docs/findings.md` | **F-051** … **F-056** |

**What was actually wrong with the screen the owner sent.**

| Symptom | Cause | Status |
|---|---|---|
| `entry -0.0414` on a LONG | C1-C's levels are a beta-adjusted spread, rendered as a price | fixed (F-051) |
| `stop 83287.42500000000160` | specs/08 §10's tick rounding was implemented and had no callers | fixed (F-054) |
| `C3-C LONG` with `entry —` | a structural placeholder direction rendered as a recommendation | fixed (F-053) |
| "long 5, short 4", concentration warning | pending and market-neutral signals counted as directional | fixed (F-053) |
| "market data 12m ago — too old to act on" | the newest *setup's* age judged against a *feed's* thresholds | fixed (F-052) |
| chart page 404 | **the VM is running an older engine** — not `CT_API_BASE`, which was correct | needs `update.sh` on the VM; the message now says so (F-055) |

**The one thing the owner must do:** `sudo bash /opt/crypto-trader/deploy/azure/update.sh`. The 404
is version skew, and no change here can fix an engine that does not have the route yet.

### Deployment, and why two sessions of it debugged the wrong machine (2026-10-11)

**Nothing was wrong with the VM.** `diagnose.sh` reported OK on all eight sections and was correct;
the engine code had never been committed. Two commits had been made and both contained only
`frontend/` files, so `update.sh` was fetching a branch without the routes, reporting "already up to
date", and restarting the same code. I gave that instruction twice without checking what it pulls.

| Path | Change |
|---|---|
| `.gitignore` | `data/` → **`/data/`** and `logs/` → `/logs/`. The unanchored rule matched `frontend/app/data/`, so the Data page could never be committed and Vercel served a 404 for a route whose source had never left the laptop (F-057) |
| `deploy/azure/diagnose.sh` | **§4b** asks the running engine which routes it serves, and **§4c** prints its commit and distance from the remote. Every existing section asked "is the engine working"; none asked "is it the version the dashboard expects" (F-059) |
| `deploy/azure/update.sh` | "already up to date" no longer reads as success — it says that means nothing has been *pushed*; after restarting it probes the dashboard's routes and names any that 404 |
| `frontend/lib/honesty.ts` | `priceBasis()` and `directionPending()` — one decision each, with fallbacks to what an **older** engine already publishes (`two_leg`, `levels_are`, `pending`, `entry_triggers`). Without them the F-051/F-053 fixes are wrong for the whole period the two sides are out of step, and for every pre-upgrade row still in the signals table (F-058) |
| `src/api/render.py` | The same two helpers and the same fallbacks, so the dashboards cannot disagree about one signal |
| `frontend/components/Panels.tsx` | Reads both through the helpers rather than the raw fields |
| `BUG_SOLVE.md` | Rewritten round 5: **Part A is the push procedure**, which every previous round assumed and none explained; Part D is safe restart, clean-slate rebuild and what each costs |
| `tests/` | 9 tests: `git check-ignore` over every deployable file (proven to catch the original bug by restoring the old pattern), the holdout still ignored, the old-engine fallbacks on both dashboards, and the two deploy scripts' new checks |
| `docs/findings.md` | **F-057**, **F-058**, **F-059** |

**The standing lesson.** A two-sided deployment drifts apart by default and neither side can see the
other's version, so every symptom of the drift looks like a configuration problem on whichever side
is being looked at. The answer is not more careful instructions — it is each side being able to
state its own version, and one of them checking the other's.


### PHASE-33 — Health interpretation and C1-C verification (2026-10-11) — **Phase 1 of 4 complete**

Multi-phase task: health/C1-C diagnosis (1), model filters + timestamps + history + outcome updates
(2), an Analytics page (3), integration and docs (4). **Phase 1 only** in this entry; phases 2–4
are not started. The canonical model names found during Phase Zero are recorded below because every
later phase depends on them.

#### The four strategy classes — canonical names (from `specs/03`–`specs/06`)

| Class | Canonical spec title | Mechanism | Strategies | DB column |
|---|---|---|---|---|
| 1 | **Correction & Rebalancing** | restoring force — fade dislocations | C1-A, C1-B, C1-C | `signals.strategy_class = 1` |
| 2 | **Trend Persistence & Risk-Normalized Participation** | risk-adjusted drift — participate | C2-A, C2-B, C2-C | `= 2` |
| 3 | **Anticipatory Transition** | state change — anticipate, risk quantified | C3-A, C3-B, C3-C | `= 3` |
| 4 | **Established Methodologies & the Benchmark Suite** | external evidence — published, independently documented edges, and the benchmark controls | C4-A, C4-B, C4-C (+ controls X1, X2) | `= 4` |

Class 4 is the one the request called "external evidence"; its authoritative name is **Established
Methodologies & the Benchmark Suite** (`specs/06`). The classification is **already persisted** as
`signals.strategy_class`, written from `raw.detail["strategy_class"]`, so filters must key on that
column and never on display text.

#### Phase 1 changes

| Path | Change |
|---|---|
| `src/core/health.py` | `operational_status`, `faults()`, `modelling_components`, `modelling_findings`; `degraded_reasons` is faults-only; `to_dict()` publishes `operational_status`, `fault_count`, `modelling_findings` (F-060) |
| `src/api/services.py` | The envelope carries `operational_status` and `modelling_findings`, so the finding travels with every response; `HealthService.health()` no longer overrides it with the overall status |
| `src/api/render.py` | A distinct "REDUCED SELECTIVITY" notice, styled apart from the fault banners |
| `frontend/components/Panels.tsx` | `HealthPanel` counts faults over operational components and gives findings their own panel, naming the asset and the failing test |
| `frontend/app/globals.css` | `.card.model-finding` |
| `tests/` | 15 tests: health separation (4), the endpoint and both dashboards (6), C1-C's sign convention (5) |
| `docs/findings.md` | **F-060**, **F-061** |

**No schema change, no threshold change, no model change.** The regime classifier still reports
`DEGRADED` for a degraded model and a test asserts it does.

#### Phase 2–4 investigation already banked (do not redo)

* **Timestamps.** `signals.created_at_ms` is the wall-clock write time, is **not** in the
  `ON CONFLICT DO UPDATE SET` list, and is therefore already the stable suggestion-generation
  timestamp. `as_of_ms` is the market-data/evaluation time; `state_since_ms` updates on transition.
  No new column is needed for Phase 2B. Defects found: `_write_signal` inserts `expires_at_ms=None`
  and `original_entry_price=None`, and sets `armed_at_ms = as_of_ms` rather than the setup's own
  arming time.
* **Outcome evaluation already exists.** `strategy/lifecycle.py:_manage_triggered` checks stop and
  T1 against the current price and transitions to `STOPPED` / `TARGET_HIT`; `Lifecycle` already has
  both as terminal states. **Reuse it; do not build a second evaluator.**
* **The Phase 2D defect is a wiring gap.** `AuditTrail.open_setups()` exists, is documented "for a
  restart to resume from (specs/13 §11.1)", and **has no callers**. `AppRunner.states` starts as
  an empty dict every process start, so every restart abandons every armed setup: those rows stay
  `SETUP_DETECTED`/`ENTER_NOW`/`TRIGGERED` in the `signals` table forever and can never reach
  `TARGET_HIT`, `STOPPED` or `EXPIRED`. `setup_state` **is** written (`audit/trail.py`), so the
  persistence half is present and only the resume half is missing. specs/13 §11.1 requires
  re-evaluating each persisted setup against the current snapshot — re-arm with the original
  `armed_at_ms`, `INVALIDATED` if conditions no longer hold, `EXPIRED` if past expiry — and
  §11.2 requires a trigger that fired during an outage to be `MISSED_OUTAGE`, never `TRIGGERED`.
  This is the third instance of the "implemented, never called" pattern after F-043 and F-054.
* **Indexes.** `idx_signals_created (created_at_ms, signal_id)` already supports date-scoped
  history pagination. There is no index on `strategy_class`.
* **Local database has zero signals**, so Phase 2–3 verification must use controlled fixtures
  rather than live data — which the task prefers anyway.


### PHASE-34 — Model filters, suggestion history and restart resume (2026-10-11) — **Phase 2 of 4: A–D complete, E blocked**

Phase 2A (model filters), 2B (timestamps), 2C (live vs history) and 2D (historical outcome updates)
are implemented and verified. **2E is deliberately not applied** — see F-063 and §Decisions needed.

#### Schema change: migration 3 (the only one in this task)

```sql
ALTER TABLE setup_state ADD COLUMN context TEXT;          -- additive, nullable, no backfill
ALTER TABLE setup_state ADD COLUMN triggered_at_ms INTEGER;
CREATE INDEX idx_setup_state_lifecycle ON setup_state (lifecycle, armed_at_ms);
CREATE INDEX idx_signals_class_created ON signals (strategy_class, created_at_ms);
```

Applied to the 6.2 GB development database in **0.01 s**, 21.9 M bars intact; separately verified to
upgrade a populated v2 database without touching its rows, and to be idempotent on re-open.
`python -m ingest` opens the store with `migrate=True` and runs **before** `app --check` in
`update.sh`, so the deploy path applies it — an ordering the deployment now depends on.

#### Changes

| Path | Change |
|---|---|
| `src/app/resume.py` | **New** — rehydrate persisted setups on startup per specs/13 §11.1, with six named refusals and a report. Pure core (`resume_setups`) takes rows and facts and returns states, so every refusal is testable without a database (F-062) |
| `src/app/__main__.py` | Calls `resume_into` **before the first evaluation** — the caller that was missing |
| `src/audit/trail.py` | `_write_setup_state` persists `context` and `triggered_at_ms`; without them the table could not support the resume it was built for |
| `src/store/migrations.py` | Migration 3 |
| `src/store/sqlite_store.py` | `_statements()` — the applier split on `sql.split(";")`, so a semicolon inside a `--` comment cut a statement in half |
| `src/api/services.py` | `STRATEGY_CLASSES` (canonical spec titles), `day_bounds` (half-open UTC), validated `_strategy_class` / `_lifecycle_state`; `live(strategy_class=…)`; `history(day, strategy_class, state, asset)` filtered **in SQL**; new `days()` aggregate; `_group_by_model` |
| `src/api/app.py` | `/api/signals/days`; `history` takes the filters; the live cache key now carries every parameter that changes the result; the cache copies `modelling_findings` |
| `frontend/components/Suggestions.tsx` | **New** — `ModelFilter`, `DayPicker`, `StatusFilter`, `HistoryRow`, `utcStamp`, `modelList`. Reuses the existing `.seg`/`.card`/`.row` classes |
| `frontend/app/page.tsx` | Live/History switch, model + status filters, date strip, paged history. The live poll stops when History is open; the day index is not polled |
| `frontend/lib/api.ts` | `liveSignalsFiltered`, `signalDays`, `signalHistory` |
| `frontend/app/globals.css` | `.day-strip`, `.outcome-*`, `.select`, `.chip-count` |
| `tests/test_resume.py` | **New** — 16 tests, weighted to the refusals |
| `tests/test_api_suggestions.py` | **New** — 21 tests on a controlled 8-row fixture, every count hand-computed |
| `tests/test_frontend_contract.py` | +9 on the filters, UTC stamps, paging and poll discipline |
| `tests/test_store.py` | The schema-version assertion derives from `MIGRATIONS` instead of hard-coding `[1, 2]` |
| `docs/findings.md` | **F-062**, **F-063** |

#### Timestamp semantics (2B) — no new column was needed

| Column | Means | Moves? |
|---|---|---|
| `created_at_ms` | **when the suggestion was issued** | no — absent from the `DO UPDATE SET` list |
| `as_of_ms` | the market data the evaluation ran on | on each republish |
| `state_since_ms` | when the lifecycle last changed | on each transition |

History orders and filters on `created_at_ms`, so an outcome recorded today does not move a
suggestion from yesterday into today. A test asserts the SQL keeps it out of the update list, and
another asserts the issue time is byte-identical after a state change.

**Day boundaries are UTC and the UI says so.** Every bar is UTC-aligned (F-012) and `findings.md`
dates everything in UTC; grouping by a local day while storing UTC would move the boundary with the
viewer, so "look at the 10th" would mean different sets of suggestions to two people.

#### Efficiency (measured, not asserted)

`EXPLAIN QUERY PLAN` on all five new queries — none scans the signals table:

| Query | Plan |
|---|---|
| history by day | `SEARCH … USING INDEX idx_signals_created` |
| history by day + model | `SEARCH … USING INDEX idx_signals_class_created` |
| history by model | `SEARCH … USING INDEX idx_signals_class_created` |
| day index | `SCAN … USING COVERING INDEX` (never touches the table) |
| resume | `SEARCH setup_state USING INDEX idx_setup_state_lifecycle` |

#### Decisions needed before 2E

**F-063.** `signals.created_at_ms` is `NOT NULL`, has been since the table was created, and holds a
real issue time for every row (0 rows with NULL or 0). The requested 10 Oct 23:11 legacy stamp would
therefore **overwrite accurate data with a fabricated constant**, which four of the task's own
constraints forbid. Three options are written out in F-063; option 2 (a non-destructive, labelled
`legacy_display_ms` column scoped by a recorded cutoff) is the one the request itself points at and
is what will be built on confirmation. The development database has **zero** signals, so the only
affected rows are on the VM.

#### Not started

Phase 3 (Analytics page) and Phase 4 (integration, regression, docs). The Phase 3 denominators are
the part to get right: "share of all suggestions that hit a target" and "target-hit rate among
*resolved* suggestions" are different numbers, and the lifecycle has seven terminal states, not two
— `EXPIRED`, `INVALIDATED`, `SUPPRESSED`, `MISSED` and `HANDED_OFF` are resolutions that are neither
a hit nor a stop, and a rate that buries them in the denominator understates both.


### PHASE-35 — The Analytics page, and Phase 4 integration (2026-10-11) — **Phases 3 and 4 complete**

Fourth page, integration-tested, documented. The task's four phases are now 1 ✅, 2 ✅ (A–D; **E
awaiting a decision**, F-063), 3 ✅, 4 ✅.

#### Changes

| Path | Change |
|---|---|
| `src/api/services.py` | `AnalyticsService` — `overview()` plus the state buckets (`OPEN_STATES`, `RESOLVED_STATES`, `CLOSED_WITHOUT_RESULT`, `TRIGGERED_STATES`), five rates each carrying its own numerator, denominator and definition, four SQL breakdowns, a generated `limitations` list. `build_analytics()` is separate from `build_services()` so the existing three-tuple contract is untouched |
| `src/api/app.py` | `GET /api/analytics`; `analytics=` is an **optional** keyword, so every existing `create_app()` call still works, and a missing dependency answers an honest 503 naming the wiring rather than a 404 |
| `src/app/__main__.py` | Builds and passes the analytics service |
| `frontend/app/analytics/page.tsx` | **New** — window / model / asset filters, counts, five rates with their fractions visible, model / asset / day breakdowns, and the limitations rendered **above** the figures |
| `frontend/components/Nav.tsx`, `lib/api.ts`, `app/globals.css`, proxy allow-list | The fourth page wired in; `.metric*` styles |
| `tests/test_analytics.py` | **New** — 32 tests, every figure cross-checked against a direct `COUNT(*)` |
| `tests/test_frontend_contract.py` | +8 on the analytics page |
| `docs/findings.md` | **F-064** |

#### Metric definitions (the part that had to be got right)

`specs/07` §8 has eleven states, seven terminal, and **only two are a trade result**. On the
verification fixture the five terminal states that are *not* a result are five of twelve
suggestions — more than the two that resolved. Putting them in the denominator understates every
rate; dropping them hides most of what the system did. So each is counted and named, and the page
shows the buckets summing to the total.

| Rate | Fixture | Question it answers |
|---|---|---|
| `target_hit_of_resolved` | 3/5 = **60%** | of suggestions that reached a trade result, how many worked |
| `target_hit_of_all` | 3/12 = **25%** | of everything issued, how much worked |
| `resolved_of_all` | 5/12 = 42% | how much of the output is measurable at all |
| `triggered_of_all` | 6/12 = 50% | how often a hypothetical entry occurred |

A zero denominator gives **`None`, never `0.0`**. `INVALIDATED` is excluded from the triggered count
because it is reachable both before and after a trigger. See F-064.

#### Phase 4 — integration and regression

**907 tests pass**; ruff and mypy clean on 135 source files. Up from 806 at the start of this task
(+101). No test was weakened or skipped.

Full HTTP surface exercised against a fixture, as the deployed engine serves it:

| Route | Status | Observed |
|---|---|---|
| `/api/health` | 200 | `envelope=HEALTHY faults=0 findings=1` — the F-060 fix in action |
| `/api/signals/live`, `?strategy_class=2` | 200 | 1 and 1 |
| `/api/signals/days` | 200 | 1 day |
| `/api/signals/history?day=…` | 200 | 3 |
| `/api/analytics` | 200 | `all=3 resolved=2 win=0.5` |
| `/api/sources`, `/api/candles/…`, `/api/regime`, `/` | 200 | |

Four malformed inputs (inverted range, class 9, `day=bad`, `timeframe=2h`) all answer **400** with
`problem_class: configuration` and the allowed set named — never an empty page that reads as a quiet
day.

**Query efficiency, measured.** `EXPLAIN QUERY PLAN` on all eight new queries: every one uses an
index, none scans `signals`, and the day index is a covering-index scan. One analytics page issues
**≤ 6** SELECTs, asserted through sqlite's trace callback rather than claimed.

**Regression surface checked.** `create_app`'s new parameter and `build_analytics` are additive, so
no existing call site changed. The live cache key now carries every parameter that varies the result
— without that, one model's suggestions would have been served under another's heading for the
cache's lifetime, a wrong answer rather than a stale one. The cache also now copies
`modelling_findings`, which it would otherwise have dropped for two seconds at a time.

#### Known limitations, carried forward

* **Phase 2E is not applied.** `created_at_ms` is `NOT NULL` with a real value on every row, so the
  requested legacy stamp would overwrite good data. Three options are written out in F-063; the
  non-destructive one needs a word from the owner.
* **Node is not installed here**, so the TypeScript cannot be compiled or executed. The 60-odd
  contract tests read it as text — they catch an accessor or a threshold diverging from the Python,
  not a type error. **The first Vercel build is still the compiler.**
* **The local database holds zero signals**, so every Phase 2–3 figure is verified against
  controlled fixtures rather than live data. The fixtures are the stronger check for deterministic
  rules, but they cannot catch a disagreement between the fixture's shape and what the engine
  actually writes — which is what F-056 was.
* **The analytics numbers are not performance evidence.** Nothing is validated, outcomes are
  hypothetical and uncosted, and the engine says so on the page.


### PHASE-36 — The model classification was never written, and the live card had no timestamp (2026-10-11)

Two issues reported from the deployed dashboard. One was cosmetic, one was a data-correctness defect
that made the filters arithmetically impossible.

#### The filters: `Correction 0, Persistence 0, Transition 0, Established 13` over a longer list

`_write_signal` read `raw.detail["strategy_class"]` with a default of **0**, and `build_signal`
never published it. Only the C4 strategies pass the class in their own `detail=` kwarg, so **class 4
was stored correctly and classes 1–3 were stored as 0** — and the dashboard filters on that
column. Three of four filters matched nothing, their counts read zero, and the orphaned suggestions
were counted under a `"0"` key the frontend never rendered (F-065).

| Path | Change |
|---|---|
| `src/strategy/evaluation.py` | `build_signal` publishes `int(strategy.strategy_class)` — the strategy's own declaration |
| `src/strategy/registry.py` | `strategy_class_of()` — one implementation: the registered strategy's declared class, falling back to the `C<n>-` prefix for an unregistered id |
| `src/audit/trail.py` | The writer falls back to the registry instead of to `0`, and logs `signal_class_unknown` when even that fails |
| `src/store/migrations.py` | **Migration 4** repairs rows already written: scoped to `strategy_class NOT IN (1,2,3,4) AND strategy_id GLOB 'C[1-4]-*'`, derived from the id in the same row |
| `frontend/components/Suggestions.tsx` | `modelList` appends any bucket outside the four instead of dropping it; `unclassifiedCount()` |
| `frontend/app/page.tsx` | A red line when the counts cannot add up to the list, naming the likely cause |
| `frontend/components/Panels.tsx` | The live card shows **suggested** (`created_at_ms`) and, when it differs, **state changed** (`state_since_ms`) |

Migration 4 is a **repair, not a reinterpretation**: the correct value was always determined by the
`strategy_id` in the same row under the mapping the registry has always had, and it changes no
outcome, status, price or timestamp. Verified on a fixture reproducing the bug — C1/C2/C3 corrected,
C4 untouched, an unparseable id left at 0 rather than guessed, states and `created_at_ms` unchanged,
idempotent on re-run, 0.003 s on the development database.

#### The live card's missing timestamp

`HistoryRow` showed the issue time and `SignalCard` showed none, so the most time-sensitive view was
the one that could not say how old a suggestion was — and a setup at an hourly horizon stays live
for hours by design. **Nothing was missing from the data**: `created_at_ms` has always been written
and is stable (F-063), so every existing suggestion displays its real creation time as soon as the
dashboard is redeployed (F-066).

#### Why it shipped

Every existing test inserted signals **by hand with an explicit class**, so none exercised the write
path that was defaulting it. The new test goes through the real `_write_signal` with no class in the
payload — the exact shape the bug produced — and asserts all four land correctly.

**917 tests pass**, ruff and mypy clean on 135 files.


### PHASE-37 — C3-C resolution defects, the missing trail, IST display and a Specs page (2026-10-12)

A specification audit of C1-C, C2-C, C3-C and C4-C against `specs/03`–`specs/06`, three confirmed
calculation/persistence defects, an IST presentation change and a fifth page.

#### Confirmed root causes

| Reported symptom | Verified cause | Finding |
|---|---|---|
| `B-SOL_USDT LONG C3-C`, stop 113.91 **above** entry 112.73 | `direction` was **not in the UPSERT**. C3-C arms pending with a placeholder LONG; the resolution assigned SHORT, the levels updated, the direction did not. Reproduced byte-identically before fixing | **F-067** |
| the same suggestion recorded "Target hit" immediately | the rejection target took `SMA_N` raw without checking it was on the opposite side of entry. A target behind the entry satisfies `_manage_triggered`'s comparison at the entry price itself | **F-067** |
| — (found while tracing) | `_levels` ran **before** `_trigger_state`, which caches `failed_extreme`, so the first resolved pass persisted a stop measured from the boundary instead of "beyond the failed extreme" | **F-067** |
| "most suggestions show entry and stop but no target" | the missing target is **correct** — `specs/06` §4.1 mandates no T1 for the trend strategies. The defect was that `trail_level`, which the spec requires "published live", **was not in the INSERT at all** and the UI read "none — trailed" | **F-068** |
| C1-C negative entry and stop | **no calculation defect.** Traced against `specs/03` §5: these are residual (spread) units, negative for about half of all LONG setups by construction, with `stop < entry < target = 0` correct. The display fix is F-051/F-058; it needs the VM updated | F-061 (re-verified) |
| C3-C blank entries | **correct.** A pending C3-C has no entry by design — the direction decides it — and the dashboard renders "DIRECTION NOT YET DECIDED" | — |

#### Files changed

| Path | Change |
|---|---|
| `src/audit/trail.py` | `direction`, `trail_level`, `as_of_ms`, `snapshot_id`, `regime_cell`, `regime_dwell`, `original_entry_price` now follow a republish; `trail_level` added to the INSERT. Identity and provenance stay frozen |
| `src/strategy/c3_transition.py` | **two edits only**: `_trigger_state` before `_levels`, and the rejection target falls back to the measured move when the mean is behind the entry |
| `src/api/spec_map.py` | **New** — `SPEC_MAP` (series to spec file, section, heading) merged with the live registry for the implementation half |
| `src/api/app.py` | `GET /api/specs` |
| `frontend/components/Suggestions.tsx` | `istStamp()` — fixed +05:30, UTC getters, browser-independent; `utcStamp` kept for reconciliation |
| `frontend/components/Panels.tsx` | IST on the live card; the trailing stop shown where there is no fixed target, with one line saying why |
| `frontend/app/specs/page.tsx` | **New** — the fifth page, grouped by class, engine-served |
| `frontend/components/Nav.tsx`, `lib/api.ts`, proxy allow-list | Specs wired in |
| `tests/test_spec_map.py`, `tests/test_signal_fields.py` | **New** — 13 + 10 tests |
| `tests/test_frontend_contract.py` | +14 on IST, the Specs page and the trail |

#### Regression protection for C3-C and C4-C

`git diff HEAD` is **empty** for `c4_established.py`, `c1_correction.py`, `c1c_relative.py`,
`c2_participation.py`, `risk/engine.py` and `strategy/lifecycle.py`. C3-C has exactly two changes,
and the target change is **strictly narrowing**: where the mean is on the correct side the output is
byte-identical to before. All four quadrants (long/short x mean ahead/behind) are asserted, and a
test pins that a pending C3-C still publishes no levels at all.

#### Specification mapping (all 14 series, verified against the files)

| Class | Series | Specified in |
|---|---|---|
| 1 Correction & Rebalancing | C1-A, C1-B, C1-C | `specs/03-class1-correction.md` §3, §4, §5 |
| 2 Trend Persistence & Participation | C2-A, C2-B, C2-C | `specs/04-class2-participation.md` §3, §4, §5 |
| 3 Anticipatory Transition | C3-A, C3-B, C3-C | `specs/05-class3-transition.md` §3, §4, §5 |
| 4 Established Methodologies | C4-A, C4-B, C4-C | `specs/06-class4-established.md` §4.1, §4.2, §4.3 |
| 4 (controls, never tradeable) | C4-X1, C4-X2 | `specs/06-class4-established.md` §5.1, §5.2 |

`tests/test_spec_map.py` opens each file and asserts the **literal heading** is present, so a
renumbered section fails a test instead of misdirecting a reader. The implementation half is read
from the registry at runtime and cannot drift from the code.

#### IST

A fixed +05:30 offset applied to the instant, then read through `getUTC*` — exact (IST has no DST)
and independent of the viewer's browser and its ICU data. Acceptance criterion verified:
2026-10-12 06:21 UTC to 2026-10-12 11:51 IST, and the midnight crossing 20:00 UTC to **next day**
01:30 IST.

**The UTC day grouping is unchanged**, deliberately: it is the engine's, every bar is UTC-aligned,
and moving it would change which suggestions fall in which group — a data change, not a
presentation one. So a suggestion issued late in a UTC day shows an IST date one day after its
heading; the history view says so in one line and every timestamp carries its UTC reading on hover.
Candle axes, the analytics query parameters and `day_bounds` are untouched, asserted by test.

#### Open

**Q12.** `specs/05` §5 specifies C3-C's rejection entry as the ACT trigger; the implementation uses
`boundary`. A confirmed deviation, and after the ordering fix it is a one-line change — but it moves
every C3-C rejection entry and with it `k_net`, R:R and the risk gates, on a strategy the owner has
identified as working. Left unchanged pending a decision. Every other strategy uses `act_state.e_t`
as entry, so `boundary` is the outlier.

**954 tests pass**, ruff and mypy clean on 136 files.


### PHASE-38 — Telegram notifications for BTCUSDT C3-C (2026-10-17)

One isolated, read-only capability. **No file under `src/` was modified** — the feature is a new
package, and a test asserts no existing module imports it, so `src/notify/` can be deleted with no
other effect.

| Path | Change |
|---|---|
| `src/notify/config.py` | **New** — `CT_TELEGRAM__*` from the environment, deliberately *not* added to `config/settings.py` (which every working process imports). Token redaction |
| `src/notify/telegram.py` | **New** — one POST, failures classified by whether a retry could help, never raises, never puts the URL (and so the token) in a message |
| `src/notify/watcher.py` | **New** — read-only poll, SQL-level filter, per-signal dedupe, atomic state file, IST message formatting |
| `src/notify/__main__.py` | **New** — `--check`, `--once`, `--dry-run`, `--test-message`, and a poll loop that survives every failure |
| `deploy/azure/crypto-notify.service` | **New** — third unit, no `Requires=` on the other two |
| `deploy/azure/env.example` | Optional `CT_TELEGRAM__*` section appended |
| `deploy/azure/update.sh` | Restarts the notifier **only if installed**; a deployment without it is not an error |
| `TELEGRAM_HOSTING.md` | **New** — BotFather, chat id, env placement, deploy, verify, operate, troubleshoot |
| `tests/test_notify.py` | **New** — 36 tests, weighted to the filter and to isolation |
| `docs/findings.md` | **F-070** |

**Integration point.** A hook in `AuditTrail.record_evaluation` was rejected: that method runs inside
one atomic transaction, so an HTTPS call there would let a slow Telegram delay evaluation and a raised
exception roll back the whole pass's audit record. A separate read-only process cannot do either.

**Change detection.** `signals.state` compared against the last state sent for that `signal_id`.
`signal_events` was rejected because `_write_events` inserts on every managed pass rather than per
transition, and `state_since_ms` because it is re-stamped on every republish — either would have
notified once a minute for the life of a setup.

**990 tests pass**, ruff and mypy clean on 141 files.

### PHASE-38a — The preflight could not see the configuration it reported on (2026-10-17)

The owner entered the Telegram variables in `/etc/crypto-trader/env` correctly, and PHASE-38's
Step 6 still said `CT_TELEGRAM__BOT_TOKEN is not set`. **`EnvironmentFile=` is a systemd
directive** — systemd reads that file when it starts a unit, and a shell never does. The guide's
verify-before-you-install step could not work by construction, and the error named the variable, so
it read as the operator's mistake. Full account in `docs/findings.md` **F-071**.

| Path | Change |
|---|---|
| `src/notify/config.py` | Reads the environment file itself as a **fallback**: real environment first (so the running service is unchanged), then the file, then the default. `parse_env_file`, `load_env_file`, and `_missing`, which distinguishes four causes that previously collapsed into one dead-end sentence |
| `src/notify/__main__.py` | `--env-file`, and a `values from: <path> (N setting(s) in it)` preflight line — threaded in from the resolved path, not re-derived |
| `TELEGRAM_HOSTING.md` | Step 6 corrected, with a note for anyone holding an older copy; the troubleshooting entry for that exact message is now a cause → fix table |
| `tests/test_notify.py` | 36 → **52** tests: the reported failure reproduced, env-wins precedence, the four distinct causes, and the forms an operator might plausibly type |

Still **no file under `src/` outside `src/notify/` modified**. **1006 tests pass**, ruff and mypy
clean on 141 files.

The general lesson, recorded because it applies beyond this feature: *a verification step must load
configuration by the same path as the thing it verifies.* The other `--check` commands in the
hosting guides share the gap and only avoid the symptom because every setting they read has a
default — they answer quietly and wrongly rather than erroring.

## Next Action

> **Everything in the build plan up to validation is done. The next step produces numbers, and the
> order it happens in is load-bearing.**
>
> 1. **`PHASE-27` — harness integrity, and it is a halt gate.** Run it **before looking at any
>    strategy's P&L**, because once a number has been seen, every subsequent choice is contaminated
>    and the holdout is one amendment away from burning. The checks are specs/16 Stage 1: the
>    permutation test must collapse to the control distribution; **C4-X2 must backtest badly** (if it
>    does well, the harness is wrong, not the strategy); `compute_series == compute_at` across the
>    registry; a deliberate 100× fee error must be caught; the shared-cost import graph must show one
>    module. Any failure halts and is reported rather than worked around.
> 2. **`PHASE-28`** — the walk-forward, the C4-X1 baseline distribution across many seeds, the
>    sensitivity sweeps, and then **PROMOTE / REVISE / CUT** per strategy. Three strategies are
>    *predicted* to be cut; that is the protocol working, not a setback.
> 3. **`PHASE-29`** — the holdout, **once**, after the decisions are final.
>
> **Two decisions are wanted but block nothing** (both are live and defaulted):
> **Q-L** — should C2-A's structural stop read `STR_SWING` at `1h/medium` instead of `1h/short`? The
> short window cannot hold swings at the trading horizon's scale (F-038), so today the stop
> references smaller structure than its premise describes.
> **Q-M** — C1-A and C2-C cannot be backtested at all (no Tier B history). Do they reach shadow with
> no historical evidence, or wait for the live archive to grow?
>
> **Operationally**, two processes must now both run: `python -m ingest` (keeps SQLite current — this
> was the gap behind F-035/F-036) and `python -m app --serve`. The recorder remains the raw archive
> and is a third, independent thing.
>
> **Azure (`PHASE-02A`, `PHASE-31`) stays deferred** per the owner's instruction until validation has
> run. The outstanding Azure inputs are **Q-H**; the DB is now ~6.3 GB, so the disk recommendation is
> **64 GiB (E6)**, not the 32 GiB in §3A.
