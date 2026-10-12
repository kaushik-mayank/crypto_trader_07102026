'use client';

/**
 * The dashboard.
 *
 * It reads three endpoints through the proxy and renders exactly what they say. Every judgement
 * about whether something is fresh, degraded or unvalidated is made by the engine and travels in
 * the payload; this page's only job is to not discard that and to not present any of it as more
 * certain than it is.
 */
import { useCallback, useEffect, useState } from 'react';

import { ActionBar, Down, Empty, ExposureStrip, Freshness, HealthPanel, SignalCard, Warming } from '@/components/Panels';
import { DayPicker, HistoryRow, ModelFilter, StatusFilter, modelList, unclassifiedCount } from '@/components/Suggestions';
import { REFRESH_MS, api, type ApiResult } from '@/lib/api';
import { ageText, panelState } from '@/lib/honesty';

type State = {
  signals: ApiResult | null;
  health: ApiResult | null;
  /** When the browser last completed a read. Distinct from the data's own age. */
  fetchedAt: number | null;
  loading: boolean;
};

/** How many historical suggestions one page fetches. Paged, never the whole table. */
const HISTORY_PAGE = 50;

export default function Page() {
  const [state, setState] = useState<State>({ signals: null, health: null, fetchedAt: null, loading: true });
  /** `live` is the lifecycle's own definition of active; `history` browses by issue date. */
  const [view, setView] = useState<'live' | 'history'>('live');
  const [model, setModel] = useState('');
  const [status, setStatus] = useState('');
  const [day, setDay] = useState('');
  const [days, setDays] = useState<any[]>([]);
  const [history, setHistory] = useState<ApiResult | null>(null);
  const [historyLoading, setHistoryLoading] = useState(false);

  const refresh = useCallback(async () => {
    const [signals, health] = await Promise.all([
      api.liveSignalsFiltered(model || undefined),
      api.health(),
    ]);
    setState({ signals, health, fetchedAt: Date.now(), loading: false });
  }, [model]);

  useEffect(() => {
    void refresh();
    // The live poll runs only while the live view is open. Polling a view nobody is looking at
    // spends the proxy's budget and the VM's cycles to update a panel off screen.
    if (view !== 'live') return undefined;
    const t = setInterval(() => void refresh(), REFRESH_MS);
    return () => clearInterval(t);
  }, [refresh, view]);

  // The day index is fetched when the history view opens and when the model filter changes — not on
  // a timer. Past days do not gain suggestions, so re-polling them would be requests spent to
  // receive the same answer.
  useEffect(() => {
    if (view !== 'history') return;
    let cancelled = false;
    void (async () => {
      const res = await api.signalDays(model || undefined);
      const list = (res.payload as any)?.data?.days ?? [];
      if (cancelled) return;
      setDays(list);
      // Default to the newest day that has anything, so the view is never empty on open.
      setDay((current) => (current && list.some((d: any) => d.day === current)
        ? current
        : (list[0]?.day ?? '')));
    })();
    return () => {
      cancelled = true;
    };
  }, [view, model]);

  useEffect(() => {
    if (view !== 'history' || !day) return;
    let cancelled = false;
    setHistoryLoading(true);
    void (async () => {
      const res = await api.signalHistory({
        day,
        strategyClass: model || undefined,
        state: status || undefined,
        limit: HISTORY_PAGE,
      });
      if (!cancelled) {
        setHistory(res);
        setHistoryLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [view, day, model, status]);

  const sig = state.signals;
  const panel = sig?.transportError
    ? ({ kind: 'down', message: sig.transportError, likelyCause: 'the browser could not reach this page’s own server' } as const)
    : panelState(sig?.payload);

  const envelope = (sig?.payload as any)?.envelope ?? {};
  // `data` is an object, and the signals are the `signals` key inside it (`services.py:live`).
  // Reading `data` itself bound that object to an `any[]`, so `data.length` was `undefined`,
  // `data.length === 0` was false, and the page fell through to `data.map(...)` on something with
  // no `.map` — a TypeError during render. **No suggestion could ever have appeared on this page**,
  // whatever the engine found. The annotation is what hid it: `any[]` asserts a shape instead of
  // checking one, so there was nothing for the compiler to object to. Guarded now, and
  // `tests/test_frontend_contract.py` asserts the accessor by name (F-048).
  const payloadData = (sig?.payload as any)?.data;
  const data: any[] = Array.isArray(payloadData?.signals) ? payloadData.signals : [];
  // Undefined rather than null when the engine does not publish it, so `Freshness` renders its own
  // "the engine did not say how old this is" rather than a confident-looking zero.
  const marketAge: number | null | undefined = payloadData?.market_data?.age_s;
  // Labels come from the engine's own `models` map (the canonical specs/03-06 titles), with counts
  // where the payload carried them. The frontend never derives a model from a strategy id.
  const models = modelList(payloadData);
  // Non-zero means the chips and the list below them genuinely disagree, and the reader
  // deserves to be told which number to doubt rather than discovering it by adding up.
  const unclassified = unclassifiedCount(payloadData);
  const historyData = (history?.payload as any)?.data;
  const historySignals: any[] = Array.isArray(historyData?.signals) ? historyData.signals : [];

  return (
    <main className="wrap">
      <h1>Trade suggestions</h1>

      {/* Stated on every load, not buried in an about page. Someone reading this under time
          pressure should not have to remember what the system is allowed to do. */}
      <div className="banner never-trades">
        Read-only. This system never places, cancels or transfers anything — it suggests, and a
        person decides. Nothing here has been validated against history yet.
      </div>

      {state.loading && <div className="card empty">Reading the engine…</div>}

      {!state.loading && panel.kind === 'down' && <Down state={panel} />}
      {!state.loading && panel.kind === 'warming' && <Warming detail={panel.detail} />}

      {!state.loading && panel.kind === 'ready' && (
        <>
          <ActionBar signals={data} />

          <h2>Freshness</h2>
          <div className="card">
            <div className="row">
              {/* **Market data age comes from the newest 1-minute bar, not from the envelope.**
                  The envelope dates the payload, which for this endpoint is the newest *signal* —
                  and a setup at an hourly horizon legitimately stays armed for hours. Judging that
                  against a 300-second red threshold made a healthy system report "too old to act
                  on" permanently, which is a warning nobody reads twice (F-052). */}
              <Freshness ageSeconds={marketAge} label="market data" />
              <span className="age age-ok">
                newest suggestion {ageText(envelope.age_seconds)}
              </span>
              {state.fetchedAt && (
                <span className="age age-ok">
                  page read {Math.round((Date.now() - state.fetchedAt) / 1000)}s ago
                </span>
              )}
            </div>
            <p style={{ margin: '8px 0 0', color: 'var(--muted)' }}>
              <strong style={{ color: 'var(--text)' }}>Three different ages, and only the first
              says whether the system can see.</strong>{' '}
              <em>Market data</em> is how old the newest price bar is — this is the one that matters,
              and over a minute or two means data has stopped arriving. <em>Newest suggestion</em> is
              how long ago the most recent setup was written; setups here last hours by design, so a
              large number is normal and is not staleness. <em>Page read</em> is this browser, and a
              fresh page read never makes old data current.
            </p>
          </div>

          <h2>Exposure</h2>
          <ExposureStrip signals={data} />

          <h2>Suggestions</h2>

          <div className="card">
            <div className="controls">
              <div className="control">
                <span className="label">view</span>
                <div className="seg" role="group" aria-label="View">
                  <button type="button" aria-pressed={view === 'live'}
                          onClick={() => setView('live')}
                          title="setups the engine is currently tracking">
                    Live
                  </button>
                  <button type="button" aria-pressed={view === 'history'}
                          onClick={() => setView('history')}
                          title="past suggestions, by the date they were issued">
                    History
                  </button>
                </div>
              </div>
              <ModelFilter models={models} value={model} onChange={setModel} />
              {view === 'history' && <StatusFilter value={status} onChange={setStatus} />}
            </div>
            {unclassified > 0 && (
              <p className="age age-red" style={{ margin: '10px 0 0' }}>
                {unclassified} suggestion{unclassified === 1 ? '' : 's'} here{' '}
                {unclassified === 1 ? 'has' : 'have'} no valid model class, so{' '}
                {unclassified === 1 ? 'it belongs' : 'they belong'} to none of the filters above and
                the counts will not add up to the list below. That is an engine defect, not a market
                condition — the VM is probably running a version from before the classification was
                fixed, so update it.
              </p>
            )}
            <p style={{ margin: '10px 0 0', color: 'var(--muted)' }}>
              <strong style={{ color: 'var(--text)' }}>Live</strong> is what the engine is tracking
              now — a setup detected, at its trigger, or triggered and being followed.{' '}
              <strong style={{ color: 'var(--text)' }}>History</strong> is every suggestion by the
              date it was <em>issued</em>, which does not move when its outcome is recorded later.
            </p>
          </div>

          {view === 'live' ? (
            data.length === 0 ? (
              <Empty
                reason={
                  model
                    ? 'No setup from this model met all of its conditions on the latest closed bar. Other models may have.'
                    : envelope.detail ??
                      'No strategy met all of its conditions on the latest closed bar.'
                }
              />
            ) : (
              data.map((s) => <SignalCard key={s.signal_id} signal={s} />)
            )
          ) : (
            <>
              <div className="card">
                <DayPicker days={days} value={day} onChange={setDay} />
                <p style={{ margin: '10px 0 0', color: 'var(--muted)' }}>
                  Times are shown in <strong>IST</strong>. The dates above are{' '}
                  <strong>UTC days</strong> — the engine's own grouping, which every bar in the
                  system is aligned to. So a suggestion issued late in a UTC day correctly shows an
                  IST date one day later than the heading it sits under. Hover any timestamp to see
                  the same instant in UTC.
                </p>
              </div>

              {history?.transportError && (
                <Down state={{ kind: 'down', message: history.transportError }} />
              )}
              {(historyData === undefined && !historyLoading && days.length > 0) && (
                <div className="card empty">Select a date above.</div>
              )}
              {historyLoading && <div className="card empty">Reading that day…</div>}

              {!historyLoading && historyData && historySignals.length === 0 && (
                <div className="card empty">
                  <strong style={{ color: 'var(--text)' }}>
                    Nothing matches these filters on {day}
                  </strong>
                  <p style={{ margin: '6px 0 0' }}>
                    The engine answered — this is an empty result, not a failure. Widen the model or
                    status filter, or pick another date.
                  </p>
                </div>
              )}

              {!historyLoading && historySignals.map((s) => (
                <HistoryRow key={s.signal_id} signal={s} models={models} />
              ))}

              {historyData?.next_cursor && (
                <div className="card empty">
                  Showing the first {historySignals.length} of this day. More exist — narrow by
                  model or status to see them.
                </div>
              )}
            </>
          )}
        </>
      )}

      <h2>System health</h2>
      {/* "The engine did not report any component health" was being shown for an unreachable
          engine as well as for an engine that answered with nothing — the first deployment hit
          exactly that. The health reading is now classified before it is rendered, so an
          unreachable engine reads as unreachable (F-047). */}
      <HealthSection result={state.health} />

      <footer>
        Refreshes every {REFRESH_MS / 1000}s. Suggestions are produced by twelve strategies across
        three assets; none has passed validation, so each is sized at a reduced multiple and marked
        accordingly.
      </footer>
    </main>
  );
}

/**
 * The health reading, classified before it is rendered.
 *
 * Four outcomes, and they must not share a message:
 *
 * 1. the request never arrived — infrastructure, and nothing else on the page is trustworthy;
 * 2. the engine answered with an error — show the engine's own words, cause and fix;
 * 3. the engine answered with a non-OK status and no error body — something between the browser
 *    and the engine replied, so name the status code rather than inventing a cause;
 * 4. the engine answered — hand it to `HealthPanel`.
 */
function HealthSection({ result }: { result: ApiResult | null }) {
  if (result === null) {
    return <div className="card empty">Reading health…</div>;
  }
  const payload = result.payload as Record<string, any> | null;

  if (result.transportError) {
    return (
      <Down
        state={{
          kind: 'down',
          message: result.transportError,
          likelyCause: 'the VM is off, port 8000 is closed in the Azure firewall, or CT_API_BASE / CT_API_TOKEN on Vercel do not match the VM',
          remediation: 'on the VM, run: bash /opt/crypto-trader/deploy/azure/diagnose.sh',
        }}
      />
    );
  }
  if (payload?.error) {
    return (
      <Down
        state={{
          kind: 'down',
          message: String(payload.error.message ?? 'health could not be read'),
          likelyCause: payload.error.likely_cause ?? undefined,
          remediation: payload.error.remediation ?? undefined,
          correlationId: payload.error.correlation_id ?? undefined,
        }}
      />
    );
  }
  if (!result.ok) {
    return (
      <Down
        state={{
          kind: 'down',
          message: `the engine answered HTTP ${result.status} with no explanation`,
          likelyCause: 'the reply came from something in front of the engine rather than from the engine itself',
          remediation: 'on the VM, run: bash /opt/crypto-trader/deploy/azure/diagnose.sh',
        }}
      />
    );
  }
  return <HealthPanel payload={payload?.data ?? payload} />;
}
