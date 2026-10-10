'use client';

/**
 * The data page: what the system is actually fetching, and when each source last delivered.
 *
 * **The question this answers that nothing else does: is each source still arriving?** Health says
 * whether components are *working*, which is a different thing — a poller that has been failing for
 * an hour is DEGRADED and visible, but a poller that succeeds while the venue returns the same
 * frozen row is HEALTHY, and the only evidence is the data's own timestamps. The chart shows that for
 * candles. This page shows it for everything, on one screen, so a single source that has gone quiet
 * stands out against the ones that have not.
 *
 * Two distinctions it refuses to blur:
 *
 * 1. **Tier A can be rebuilt; Tier B cannot.** A gap in candles is an inconvenience — the bootstrap
 *    refetches them. A gap in funding, positioning or the order book is permanent, for everyone,
 *    with any API key, because no endpoint serves yesterday's order book. Each source says which it
 *    is, next to its own numbers rather than in a footnote.
 * 2. **Fresh is not the same as correct.** A source can be perfectly current and still be throwing
 *    rows away, so the data-quality counts sit on the same page as the timestamps.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import { Down } from '@/components/Panels';
import { api } from '@/lib/api';
import { ageText } from '@/lib/honesty';

const REFRESH_MS = 15_000;

type SeriesSource = {
  id: string;
  label: string;
  kind: string;
  tier: string;
  asset: string;
  product: string;
  timeframe: string;
  rows: number;
  newest_ms: number | null;
  oldest_ms: number | null;
  pending_confirmation: number;
  age_s: number | null;
  interval_s: number | null;
  intervals_behind: number | null;
  last_value: Record<string, any> | null;
};

type PitSource = {
  id: string;
  label: string;
  tier: string;
  readable: boolean;
  /** False when nothing in this system polls the source — an empty table is then correct. */
  collected?: boolean;
  note?: string;
  error?: Record<string, any>;
  per_asset?: {
    asset: string | null;
    rows: number;
    newest_ms: number | null;
    age_s: number | null;
    rows_last_hour: number;
    last_value: Record<string, any> | null;
  }[];
};

type Payload = {
  as_of_ms: number;
  series: SeriesSource[];
  point_in_time: PitSource[];
  data_quality?: { readable: boolean; window_h?: number; events?: any[]; message?: string };
  errors?: any[];
};

/**
 * A series is late when it is more than one interval behind its own bar length; a point-in-time
 * source when nothing has arrived for an hour.
 *
 * Expressed in **intervals** for series rather than in seconds, because seconds cannot compare
 * across timeframes: 20 minutes is a dead 1-minute feed and a perfectly healthy daily one. The same
 * absolute threshold applied to both is what makes a freshness warning permanent and therefore
 * ignored.
 */
function seriesState(s: SeriesSource): 'ok' | 'amber' | 'red' | 'unknown' {
  if (s.intervals_behind === null) return 'unknown';
  if (s.intervals_behind >= 3) return 'red';
  if (s.intervals_behind >= 1) return 'amber';
  return 'ok';
}

function pitState(age: number | null): 'ok' | 'amber' | 'red' | 'unknown' {
  if (age === null || age === undefined) return 'unknown';
  if (age > 3600) return 'red';
  if (age > 900) return 'amber';
  return 'ok';
}

function shortAsset(asset: string | null): string {
  if (!asset) return 'all';
  return asset.replace(/^B-/, '').replace(/_USDT$/, '');
}

export default function DataPage() {
  const [payload, setPayload] = useState<Payload | null>(null);
  const [problem, setProblem] = useState<Record<string, any> | null>(null);
  const [fetchedAt, setFetchedAt] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [stale, setStale] = useState(false);
  const lastGood = useRef<Payload | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await api.sources();
      const body = res.payload as Record<string, any> | null;

      if (res.transportError) {
        setProblem({
          message: res.transportError,
          likely_cause: 'the dashboard could not get a readable answer from its own server',
          remediation:
            'on the VM, run: bash /opt/crypto-trader/deploy/azure/diagnose.sh — it names the first broken link',
        });
        setStale(lastGood.current !== null);
        return;
      }
      if (!body || body.error) {
        setProblem(body?.error ?? { message: 'the dashboard received no readable response' });
        setStale(lastGood.current !== null);
        return;
      }
      const data = body.data as Payload | null;
      if (!data || !Array.isArray(data.series) || !Array.isArray(data.point_in_time)) {
        setProblem({
          problem_class: 'application_bug',
          message: 'the engine answered, but not with a source list',
          likely_cause: 'the engine and the dashboard disagree about the shape of /api/sources',
          remediation: 'the VM and this deployment are on different versions; redeploy both from the same commit',
        });
        setStale(lastGood.current !== null);
        return;
      }
      setPayload(data);
      lastGood.current = data;
      setProblem(null);
      setStale(false);
      setFetchedAt(Date.now());
    } catch (e) {
      setProblem({
        problem_class: 'application_bug',
        message: e instanceof Error ? e.message : 'the dashboard failed while reading sources',
        likely_cause: 'a defect in this page, not in the engine',
        remediation: 'open the browser console and report what it says',
      });
      setStale(lastGood.current !== null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), REFRESH_MS);
    return () => clearInterval(t);
  }, [load]);

  const shown = payload ?? lastGood.current;
  const series = shown?.series ?? [];
  const late = series.filter((s) => seriesState(s) === 'red').length;
  const totalRows = series.reduce((n, s) => n + (s.rows ?? 0), 0);

  return (
    <main className="wrap">
      <h1>Data</h1>
      <p style={{ color: 'var(--muted)', marginTop: 0 }}>
        Every source the system fetches, with its row count, when it last delivered, and the last
        value it delivered. Health says whether the pollers are working; this says whether the data
        is arriving.
      </p>

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
              <strong>Showing the last reading that arrived</strong>
              <p style={{ margin: '6px 0 0' }}>
                Everything below is from the most recent successful read, not from now.
              </p>
            </div>
          )}
        </>
      )}

      {loading && !shown && <div className="card empty">Reading sources…</div>}

      {shown && (
        <>
          <div className="card">
            <div className="row">
              <span>
                <span className="label">price series</span>
                <span className="mono">{series.length}</span>
              </span>
              <span>
                <span className="label">bars stored, all series</span>
                <span className="mono">{totalRows.toLocaleString()}</span>
              </span>
              <span>
                <span className="label">series behind</span>
                <span className={late > 0 ? 'mono short' : 'mono'}>{late}</span>
              </span>
              {fetchedAt && (
                <span>
                  <span className="label">page read</span>
                  <span className="age age-ok">{ageText((Date.now() - fetchedAt) / 1000)}</span>
                </span>
              )}
            </div>
            {series.length === 0 && (
              <p style={{ margin: '10px 0 0' }}>
                <strong>No price series are stored at all.</strong> The engine answered, so this is
                not a connection problem — the database is empty. Start or restart the ingest
                service on the VM:{' '}
                <span className="mono">sudo systemctl restart crypto-ingest</span>. It fills missing
                history at startup.
              </p>
            )}
          </div>

          {/* ---- price candles ---- */}
          <h2>Price candles</h2>
          <p style={{ color: 'var(--muted)', marginTop: 0 }}>
            <strong style={{ color: 'var(--text)' }}>Tier A — rebuildable.</strong> If these are lost
            the bootstrap refetches them from the venue. Lateness is shown in intervals, because
            twenty minutes is a dead 1-minute feed and a healthy daily one.
          </p>
          {series.length > 0 && (
            <div className="card" style={{ overflowX: 'auto' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>asset</th>
                    <th>interval</th>
                    <th style={{ textAlign: 'right' }}>bars</th>
                    <th style={{ textAlign: 'right' }}>unconfirmed</th>
                    <th>newest bar</th>
                    <th>last close</th>
                  </tr>
                </thead>
                <tbody>
                  {series.map((s) => {
                    const st = seriesState(s);
                    return (
                      <tr key={`${s.asset}-${s.product}-${s.timeframe}`}>
                        <td className="mono">
                          {shortAsset(s.asset)}
                          <span style={{ color: 'var(--muted)' }}> {s.product}</span>
                        </td>
                        <td className="mono">{s.timeframe}</td>
                        <td className="mono" style={{ textAlign: 'right' }}>
                          {s.rows.toLocaleString()}
                        </td>
                        <td
                          className={s.pending_confirmation > 0 ? 'mono age-amber' : 'mono'}
                          style={{ textAlign: 'right' }}
                        >
                          {s.pending_confirmation}
                        </td>
                        <td className={`age age-${st}`}>
                          {s.age_s === null ? 'none stored' : ageText(s.age_s)}
                          {s.intervals_behind !== null && (
                            <span style={{ color: 'var(--muted)' }}>
                              {' '}
                              · {s.intervals_behind} behind
                            </span>
                          )}
                        </td>
                        <td className="mono">{s.last_value?.c ?? '—'}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* ---- point-in-time ---- */}
          <h2>Point-in-time sources</h2>
          <p style={{ color: 'var(--muted)', marginTop: 0 }}>
            Funding, mark price, positioning and the order book. <strong>Most of these are Tier B:
            there is no archive endpoint</strong>, so a gap here cannot be filled later by anyone,
            with any API key. They accumulate only while the ingest service runs, which is why two
            strategies have no history to be tested against.
          </p>
          {(shown.point_in_time ?? []).map((src) => (
            <div className="card" key={src.id}>
              <div className="row">
                <strong>{src.label}</strong>
                {src.collected === false ? (
                  <span className="age age-ok">not collected by this system</span>
                ) : (
                  <span className={src.tier === 'A' ? 'age age-ok' : 'age age-amber'}>
                    Tier {src.tier} —{' '}
                    {src.tier === 'A' ? 'rebuildable' : 'no archive, gaps are permanent'}
                  </span>
                )}
              </div>

              {/* "We don't fetch this" and "we fetch this and nothing arrived" are different
                  findings, and a bare zero would have read as the second. */}
              {src.collected === false ? (
                <p style={{ margin: '8px 0 0', color: 'var(--muted)' }}>{String(src.note ?? '')}</p>
              ) : !src.readable || src.error ? (
                <p className="age age-red" style={{ margin: '8px 0 0' }}>
                  {String(src.error?.message ?? 'this source could not be read')}
                  {src.error?.remediation && (
                    <>
                      {' — '}
                      {String(src.error.remediation)}
                    </>
                  )}
                </p>
              ) : (src.per_asset ?? []).length === 0 ? (
                <p style={{ margin: '8px 0 0' }}>
                  <strong>Nothing has ever been recorded for this source.</strong> If the ingest
                  service is running, check its log for this source&apos;s poll task.
                </p>
              ) : (
                <div style={{ overflowX: 'auto' }}>
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>asset</th>
                        <th style={{ textAlign: 'right' }}>rows</th>
                        <th style={{ textAlign: 'right' }}>last hour</th>
                        <th>newest</th>
                        <th>last value</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(src.per_asset ?? []).map((a) => (
                        <tr key={`${src.id}-${a.asset ?? 'all'}`}>
                          <td className="mono">{shortAsset(a.asset)}</td>
                          <td className="mono" style={{ textAlign: 'right' }}>
                            {a.rows.toLocaleString()}
                          </td>
                          {/* Measured, not configured: a source set to poll every 15 seconds and
                              delivering four rows an hour is exactly the failure this page is for,
                              and only the count shows it. */}
                          <td
                            className={a.rows_last_hour === 0 ? 'mono short' : 'mono'}
                            style={{ textAlign: 'right' }}
                          >
                            {a.rows_last_hour}
                          </td>
                          <td className={`age age-${pitState(a.age_s)}`}>
                            {a.age_s === null ? 'never' : ageText(a.age_s)}
                          </td>
                          <td className="mono" style={{ fontSize: 12 }}>
                            {a.last_value
                              ? Object.entries(a.last_value)
                                  .map(([k, v]) => `${k} ${v}`)
                                  .join(' · ')
                              : '—'}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          ))}

          {/* ---- data quality ---- */}
          <h2>Data quality, last 24 hours</h2>
          <p style={{ color: 'var(--muted)', marginTop: 0 }}>
            A source can be perfectly current and still be wrong. If rows are being rejected, the
            timestamps above stay fresh and tell you nothing — these counts are where that shows.
          </p>
          {shown.data_quality?.readable === false ? (
            <div className="card down">
              <h3>The data-quality log could not be read</h3>
              <p style={{ margin: 0 }}>{String(shown.data_quality.message ?? '')}</p>
            </div>
          ) : (shown.data_quality?.events ?? []).length === 0 ? (
            <div className="card empty">
              <strong style={{ color: 'var(--text)' }}>No data-quality events in 24 hours</strong>
              <p style={{ margin: '6px 0 0' }}>
                Nothing was rejected, no bar changed after being settled, and no clock or staleness
                gate fired. This is the good case.
              </p>
            </div>
          ) : (
            <div className="card">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>event</th>
                    <th>severity</th>
                    <th style={{ textAlign: 'right' }}>count</th>
                    <th>newest</th>
                  </tr>
                </thead>
                <tbody>
                  {(shown.data_quality?.events ?? []).map((e: any) => (
                    <tr key={`${e.kind}-${e.severity}`}>
                      <td className="mono">{e.kind}</td>
                      <td
                        className={
                          String(e.severity).toLowerCase() === 'error' ? 'short' : 'age age-amber'
                        }
                      >
                        {e.severity}
                      </td>
                      <td className="mono" style={{ textAlign: 'right' }}>
                        {e.count}
                      </td>
                      <td className="age age-ok">
                        {ageText((Date.now() - e.newest_ms) / 1000)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p style={{ margin: '10px 0 0', color: 'var(--muted)' }}>
                A handful of <span className="mono">ROWS_REJECTED</span> is normal — the venue sends
                occasional bad rows and they are refused rather than computed on.{' '}
                <span className="mono">BAR_MUTATION</span> means a bar we had already settled came
                back different, which is rare and worth reporting.
              </p>
            </div>
          )}

          {(shown.errors ?? []).length > 0 && (
            <div className="card down">
              <h3>Some sources could not be read at all</h3>
              {(shown.errors ?? []).map((e: any, i: number) => (
                <p key={i} style={{ margin: '4px 0 0' }}>
                  <span className="mono">{String(e.source ?? 'unknown')}</span> —{' '}
                  {String(e.message ?? '')}
                </p>
              ))}
            </div>
          )}
        </>
      )}
    </main>
  );
}
