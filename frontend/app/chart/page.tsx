'use client';

/**
 * The chart page: the prices the suggestions are drawn from.
 *
 * This page exists to answer one question the rest of the dashboard cannot: **is the data actually
 * arriving?** Every other panel reports a conclusion — no suggestions, a regime cell, a health
 * status — and all of them look identical whether the prices behind them are current, stale or
 * absent. A candle chart is the one view where wrong data is obvious at a glance.
 *
 * So it reports the things that would otherwise hide:
 *
 *  - how old the newest bar is, and how many intervals behind "now" that puts it;
 *  - how many bars are stored in total versus how many are drawn;
 *  - how many have not yet been confirmed by a second fetch, which the chart draws hollow;
 *  - and, when a refresh fails, **the previous data marked stale** rather than a blank page. A blank
 *    throws away the last thing we knew; marking it keeps the information and the warning together.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { CandleChart, type Candle } from '@/components/CandleChart';
import { Down } from '@/components/Panels';
import { api } from '@/lib/api';
import { ageState, ageText, panelState } from '@/lib/honesty';

const ASSETS = [
  { id: 'B-BTC_USDT', label: 'BTC' },
  { id: 'B-ETH_USDT', label: 'ETH' },
  { id: 'B-SOL_USDT', label: 'SOL' },
] as const;

/** Only the intervals the engine stores, so a selector cannot ask for something that cannot exist. */
const TIMEFRAMES = [
  { id: '1m', label: '1m', ms: 60_000, bars: 600 },
  { id: '15m', label: '15m', ms: 900_000, bars: 600 },
  { id: '1h', label: '1h', ms: 3_600_000, bars: 800 },
  { id: '4h', label: '4h', ms: 14_400_000, bars: 700 },
  { id: '1d', label: '1d', ms: 86_400_000, bars: 700 },
] as const;

/**
 * 15 s, matching the engine's own fastest poll. Faster would redraw the same candles and spend the
 * proxy's budget for nothing.
 */
const REFRESH_MS = 15_000;

/**
 * Only `candles` is required, and that asymmetry is deliberate: it is the one field this page
 * validates on arrival, so it is the only one the rest of the component may assume. The counters are
 * optional here because an engine on a different version might not send them, and a type that
 * claimed otherwise would just be a comment the compiler believed.
 */
type Payload = {
  asset?: string;
  timeframe?: string;
  candles: Candle[];
  count?: number;
  stored_total?: number;
  oldest_ms?: number | null;
  newest_ms?: number | null;
  pending_confirmation?: number;
};

export default function ChartPage() {
  const [asset, setAsset] = useState<string>(ASSETS[0].id);
  const [tf, setTf] = useState<(typeof TIMEFRAMES)[number]>(TIMEFRAMES[2]);

  const [payload, setPayload] = useState<Payload | null>(null);
  const [envelope, setEnvelope] = useState<Record<string, any>>({});
  const [problem, setProblem] = useState<Record<string, any> | null>(null);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  /** Set when the newest read failed but an earlier one succeeded, so the view is last-known. */
  const [stale, setStale] = useState(false);

  // Kept in a ref so a failed refresh can leave the previous data on screen without the effect
  // depending on it and re-subscribing on every tick.
  const lastGood = useRef<Payload | null>(null);

  const load = useCallback(async (quiet: boolean) => {
    if (!quiet) setLoading(true);
    try {
      const res = await api.candles(asset, tf.id, tf.bars);
      const body = res.payload as Record<string, any> | null;

      // Three failures that must not look alike, and the reason the branches are separate:
      //
      //  - the request never arrived (`transportError`) — infrastructure;
      //  - the engine answered with a problem (`body.error`) — whatever the engine says it is;
      //  - the engine answered, with no candles — a real, reportable fact about the database.
      //
      // Collapsing the first two into "no data" would draw an empty chart for an unreachable
      // engine, which is precisely the reading this page exists to make impossible.
      if (res.transportError) {
        setProblem({
          code: 'DASHBOARD_TRANSPORT',
          message: res.transportError,
          likely_cause:
            'the dashboard could not get a readable answer from its own server, which sits in' +
            ' front of the engine',
          remediation:
            'on the VM, run: bash /opt/crypto-trader/deploy/azure/diagnose.sh — it names the first' +
            ' broken link. Nothing drawn below is known to be current.',
        });
        setStale(lastGood.current !== null);
        return;
      }
      if (!body || body.error) {
        setProblem(body?.error ?? { message: 'the dashboard received no readable response' });
        // Keep whatever we last had, marked. Discarding it would lose the only evidence on screen.
        setStale(lastGood.current !== null);
        return;
      }

      const data = body.data as Payload | null;
      if (!data || !Array.isArray(data.candles)) {
        // A 200 whose body is not the shape this page was written against is an application bug,
        // not a market condition, and saying so is the difference between reading the code and
        // restarting a service.
        setProblem({
          code: 'DASHBOARD_UNEXPECTED_SHAPE',
          problem_class: 'application_bug',
          message: 'the engine answered, but not with a candle list',
          likely_cause: 'the engine and the dashboard disagree about the shape of /api/candles',
          remediation: 'the VM and this deployment are on different versions; redeploy both from' +
            ' the same commit',
        });
        setStale(lastGood.current !== null);
        return;
      }

      setPayload(data);
      lastGood.current = data;
      setEnvelope(body.envelope ?? {});
      setProblem(null);
      setStale(false);
      setFetchedAt(Date.now());
    } catch (e) {
      // `api.candles` does not throw, so reaching here means a defect in this component. It is
      // caught anyway: an uncaught throw inside an effect blanks the page, and a blank page is the
      // one outcome that tells the reader nothing at all.
      setProblem({
        code: 'DASHBOARD_BUG',
        problem_class: 'application_bug',
        message: e instanceof Error ? e.message : 'the dashboard failed while reading candles',
        likely_cause: 'a defect in the chart page itself, not in the engine',
        remediation: 'open the browser console and report what it says; the engine may be fine',
      });
      setStale(lastGood.current !== null);
    } finally {
      setLoading(false);
    }
  }, [asset, tf]);

  useEffect(() => {
    // Not quiet on an asset or interval change: the previous asset's candles must not sit on screen
    // under the new asset's label even for a moment.
    setPayload(null);
    lastGood.current = null;
    void load(false);
    const timer = setInterval(() => void load(true), REFRESH_MS);
    return () => clearInterval(timer);
  }, [load]);

  const shown = payload ?? lastGood.current;
  const hasCandles = (shown?.candles?.length ?? 0) > 0;
  const envState = panelState({ envelope, error: problem });

  // Age from the newest bar's close, expressed in intervals as well as in time. "3 intervals
  // behind" is the number that says whether ingestion is keeping up; seconds alone do not, because
  // what counts as late depends entirely on the interval.
  const newest = shown?.newest_ms ?? null;
  const ageMs = newest === null ? null : Date.now() - (newest + tf.ms);
  const intervalsBehind = ageMs === null ? null : Math.max(0, Math.floor(ageMs / tf.ms));

  return (
    <main className="wrap">
      <h1>Chart</h1>
      <p style={{ color: 'var(--muted)', marginTop: 0 }}>
        The prices the suggestions are drawn from. If this looks wrong, nothing else on the dashboard
        can be trusted either.
      </p>

      <div className="card">
        <div className="controls">
          <div className="control">
            <span className="label">asset</span>
            <div className="seg" role="group" aria-label="Asset">
              {ASSETS.map((a) => (
                <button key={a.id} type="button" aria-pressed={asset === a.id}
                        onClick={() => setAsset(a.id)}>{a.label}</button>
              ))}
            </div>
          </div>
          <div className="control">
            <span className="label">interval</span>
            <div className="seg" role="group" aria-label="Interval">
              {TIMEFRAMES.map((t) => (
                <button key={t.id} type="button" aria-pressed={tf.id === t.id}
                        onClick={() => setTf(t)}>{t.label}</button>
              ))}
            </div>
          </div>
          <div className="control">
            <span className="label">newest bar</span>
            <span className={`age age-${ageState(ageMs === null ? null : ageMs / 1000)}`}>
              {ageMs === null
                ? 'no bars stored'
                : `${ageText(ageMs / 1000)}${intervalsBehind !== null
                    ? ` · ${intervalsBehind} interval${intervalsBehind === 1 ? '' : 's'} behind`
                    : ''}`}
            </span>
          </div>
          {fetchedAt && (
            <div className="control">
              <span className="label">page read</span>
              <span className="age age-ok">{ageText((Date.now() - fetchedAt) / 1000)}</span>
            </div>
          )}
        </div>
      </div>

      {/* A failed refresh does not blank the page. The previous candles stay, labelled. */}
      {problem && (
        <>
          <Down
            state={{
              kind: 'down',
              message: String(problem.message ?? 'the engine did not answer'),
              likelyCause: problem.likely_cause,
              remediation: problem.remediation,
              correlationId: problem.correlation_id,
            }}
          />
          {stale && shown && (
            <div className="card warming">
              <strong>Showing the last data that arrived</strong>
              <p style={{ margin: '6px 0 0' }}>
                The candles below are from the most recent successful read, not from now. They are
                kept on screen because discarding them would throw away the only evidence here — but
                they are <strong>not current</strong>.
              </p>
            </div>
          )}
        </>
      )}

      {!problem && envState.kind === 'warming' && (
        <div className="card warming">
          <strong>Warming up</strong>
          <p style={{ margin: '6px 0 0' }}>{envState.detail}</p>
        </div>
      )}

      {loading && !shown && <div className="card empty">Reading candles…</div>}

      {/* An engine that answers with zero candles is a *successful* read of an empty database, so
          `shown` is a real payload and the counters below are worth seeing (stored_total: 0 is the
          answer to "is anything there at all"). Only the chart itself is conditional. */}
      {shown && (
        <>
          {hasCandles ? (
            <div className="card" style={{ paddingBottom: 6 }}>
              <CandleChart candles={shown.candles} timeframeMs={tf.ms} />
            </div>
          ) : (
            <div className="card empty">
              <strong style={{ color: 'var(--text)' }}>No bars stored for this interval yet</strong>
              <p style={{ margin: '6px 0 0' }}>
                The engine answered — this is not a connection problem. It has nothing stored for{' '}
                <span className="mono">{asset}</span> at <span className="mono">{tf.id}</span>.
              </p>
              <p style={{ margin: '6px 0 0' }}>
                History is filled when the ingest service starts, so the first thing to try is{' '}
                <span className="mono">sudo systemctl restart crypto-ingest</span>. If it stays
                empty, run{' '}
                <span className="mono">
                  sudo -u crypto ./.venv/bin/python -m bootstrap --check
                </span>{' '}
                on the VM to see what it thinks is missing.
              </p>
            </div>
          )}

          <div className="card">
            <div className="row">
              {/* Each read with `??`: a field the engine stopped sending must not become the
                  string "undefined" on screen, and `undefined.toLocaleString()` would take the
                  whole page down rather than one number. */}
              <span>
                <span className="label">drawn</span>
                <span className="mono">{shown.count ?? shown.candles.length}</span>
              </span>
              <span>
                <span className="label">stored for this interval</span>
                <span className="mono">{(shown.stored_total ?? 0).toLocaleString()}</span>
              </span>
              <span>
                <span className="label">awaiting confirmation</span>
                <span className={(shown.pending_confirmation ?? 0) > 0 ? 'mono age-amber' : 'mono'}>
                  {shown.pending_confirmation ?? 0}
                </span>
              </span>
            </div>
            <p style={{ margin: '10px 0 0', color: 'var(--muted)' }}>
              Hollow, dashed candles have been fetched once and not confirmed by a second fetch. The
              engine will not compute on them — they are drawn so the most recent interval does not
              look missing. Gaps are real: candles sit at their own timestamps, so a missing interval
              leaves a hole rather than being closed up.
            </p>
            <p style={{ margin: '8px 0 0', color: 'var(--muted)' }}>
              <strong style={{ color: 'var(--text)' }}>What to look for.</strong> Candles up to the
              current interval and an age under one interval means ingestion is keeping up. An age of
              many intervals means it has stopped — run{' '}
              <span className="mono">bash /opt/crypto-trader/deploy/azure/diagnose.sh</span> on the
              VM. A visible hole means an outage during that period; the engine will not compute
              across it and says so rather than guessing.
            </p>
          </div>
        </>
      )}
    </main>
  );
}
