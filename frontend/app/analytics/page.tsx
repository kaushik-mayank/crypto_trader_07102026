'use client';

/**
 * The analytics page: what the engine has suggested, and how those suggestions ended.
 *
 * **Every number here comes from the engine already aggregated.** The page receives a few dozen
 * counts, not a few thousand rows, which matters for three separate reasons: the VM is small, the
 * archive only grows, and a date boundary computed in the browser moves with the viewer's timezone
 * so two readers would see different days.
 *
 * **Every rate is rendered with its numerator and denominator.** "The target-hit rate" is ambiguous
 * and the two readings differ by a lot — 3 of 5 resolved is 60%, 3 of 40 issued is 7.5% — so both
 * are shown, neither is called "the" rate, and the fraction is always visible beside the percentage
 * so any figure can be checked against a direct query.
 *
 * **Nothing here is evidence of performance.** No strategy has been validated (specs/09), outcomes
 * are hypothetical, and a rate over a handful of suggestions is noise with a percentage sign. The
 * engine generates the limitations list from the data, so it cannot go stale, and this page renders
 * it at the top rather than in a footnote.
 */
import { useCallback, useEffect, useState } from 'react';

import { Down } from '@/components/Panels';
import { ModelFilter, modelList, type ModelMeta } from '@/components/Suggestions';
import { api } from '@/lib/api';

const ASSETS = ['B-BTC_USDT', 'B-ETH_USDT', 'B-SOL_USDT'];

/** Preset windows. All within the engine's 366-day cap, which it enforces regardless. */
const RANGES = [
  { label: '7d', days: 7 },
  { label: '30d', days: 30 },
  { label: '90d', days: 90 },
  { label: '365d', days: 365 },
];

type Rate = { numerator: number; denominator: number; value: number | null; definition: string };

function utcDay(offsetDays: number): string {
  const d = new Date(Date.now() + offsetDays * 86_400_000);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}

/** A percentage, or an explicit "not defined" — never `0%` for an empty sample. */
function pct(rate?: Rate): string {
  if (!rate || rate.value === null || rate.value === undefined) return 'not defined';
  return `${(rate.value * 100).toFixed(1)}%`;
}

/**
 * One metric, with its working shown.
 *
 * The fraction is not decoration: it is what makes the number checkable, and it is what stops a
 * reader taking `100%` from one resolved suggestion as a result.
 */
function Metric({ label, rate, hint }: { label: string; rate?: Rate; hint?: string }) {
  const undefined_ = !rate || rate.value === null;
  return (
    <div className="metric" title={rate?.definition ?? hint}>
      <span className="label">{label}</span>
      <span className={undefined_ ? 'metric-value muted' : 'metric-value'}>{pct(rate)}</span>
      {rate && (
        <span className="metric-frac mono">
          {rate.numerator} / {rate.denominator}
        </span>
      )}
      {undefined_ && rate?.denominator === 0 && (
        <span className="metric-note">no suggestions in this denominator yet</span>
      )}
    </div>
  );
}

function Count({ label, value, tone }: { label: string; value: number; tone?: string }) {
  return (
    <div className="metric">
      <span className="label">{label}</span>
      <span className={tone ? `metric-value ${tone}` : 'metric-value'}>
        {value.toLocaleString()}
      </span>
    </div>
  );
}

export default function AnalyticsPage() {
  const [days, setDays] = useState(30);
  const [model, setModel] = useState('');
  const [asset, setAsset] = useState('');
  const [data, setData] = useState<any | null>(null);
  const [problem, setProblem] = useState<Record<string, any> | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.analytics({
        start: utcDay(-(days - 1)),
        end: utcDay(0),
        strategyClass: model || undefined,
        asset: asset || undefined,
      });
      const body = res.payload as Record<string, any> | null;
      if (res.transportError) {
        setProblem({
          message: res.transportError,
          likely_cause: 'the dashboard could not reach its own server',
          remediation: 'on the VM, run: bash /opt/crypto-trader/deploy/azure/diagnose.sh',
        });
        return;
      }
      if (!body || body.error) {
        setProblem(body?.error ?? { message: 'the dashboard received no readable response' });
        return;
      }
      if (!body.data?.totals) {
        setProblem({
          problem_class: 'application_bug',
          message: 'the engine answered, but not with an analytics payload',
          likely_cause: 'the VM is running a version without /api/analytics',
          remediation: 'update the VM: sudo bash /opt/crypto-trader/deploy/azure/update.sh',
        });
        return;
      }
      setData(body.data);
      setProblem(null);
    } finally {
      setLoading(false);
    }
  }, [days, model, asset]);

  // No polling. Analytics over a window of whole days changes when a suggestion resolves, not
  // every fifteen seconds, and re-running four GROUP BY queries on a timer would spend the VM's
  // cycles to redraw identical numbers.
  useEffect(() => {
    void load();
  }, [load]);

  const totals = data?.totals ?? {};
  const rates = data?.rates ?? {};
  const models: ModelMeta[] = modelList(data);

  return (
    <main className="wrap">
      <h1>Analytics</h1>
      <p style={{ color: 'var(--muted)', marginTop: 0 }}>
        What the engine suggested over a window, and how those suggestions ended. Every rate shows
        its own numerator and denominator, because the obvious phrase &ldquo;hit rate&rdquo; has two
        legitimate meanings that differ by a lot.
      </p>

      <div className="card">
        <div className="controls">
          <div className="control">
            <span className="label">window (UTC days)</span>
            <div className="seg" role="group" aria-label="Window">
              {RANGES.map((r) => (
                <button key={r.label} type="button" aria-pressed={days === r.days}
                        onClick={() => setDays(r.days)}>{r.label}</button>
              ))}
            </div>
          </div>
          <ModelFilter models={models} value={model} onChange={setModel} />
          <div className="control">
            <span className="label">asset</span>
            <div className="seg" role="group" aria-label="Asset">
              <button type="button" aria-pressed={asset === ''} onClick={() => setAsset('')}>
                All
              </button>
              {ASSETS.map((a) => (
                <button key={a} type="button" aria-pressed={asset === a}
                        onClick={() => setAsset(a)}>
                  {a.replace(/^B-/, '').replace(/_USDT$/, '')}
                </button>
              ))}
            </div>
          </div>
        </div>
        {data?.range && (
          <p style={{ margin: '10px 0 0', color: 'var(--muted)' }}>
            <span className="label">covering</span>
            <span className="mono">
              {data.range.from} to {data.range.to}
            </span>{' '}
            inclusive, {data.range.days} UTC days. Day boundaries are UTC everywhere in this system,
            so these groups mean the same thing to every reader.
          </p>
        )}
      </div>

      {problem && (
        <Down
          state={{
            kind: 'down',
            message: String(problem.message ?? 'the engine did not answer'),
            likelyCause: problem.likely_cause,
            remediation: problem.remediation,
          }}
        />
      )}

      {loading && !data && <div className="card empty">Aggregating…</div>}

      {data && (
        <>
          {/* The limitations first, not last. A number on a dashboard acquires authority it has not
              earned, and the reader needs the caveat before the figure, not after it. */}
          {(data.limitations ?? []).length > 0 && (
            <div className="card unvalidated" style={{ borderStyle: 'solid' }}>
              <strong>What these numbers are not</strong>
              <ul style={{ margin: '8px 0 0', paddingLeft: 20 }}>
                {(data.limitations as string[]).map((l) => (
                  <li key={l} style={{ marginBottom: 4 }}>{l}</li>
                ))}
              </ul>
            </div>
          )}

          <h2>Suggestions in this window</h2>
          <div className="card">
            <div className="metric-row">
              <Count label="all issued" value={totals.all ?? 0} />
              <Count label="still open" value={totals.open ?? 0} tone="age-amber" />
              <Count label="target hit" value={totals.target_hit ?? 0} tone="long" />
              <Count label="stopped" value={totals.stopped ?? 0} tone="short" />
              <Count label="ended with no trade result" value={totals.closed_without_result ?? 0} />
            </div>
            {/* The buckets are shown to add up, so nothing can be quietly dropped from a
                denominator without the arithmetic visibly failing. */}
            <p style={{ margin: '10px 0 0', color: 'var(--muted)' }}>
              {totals.open ?? 0} open + {totals.resolved ?? 0} resolved +{' '}
              {totals.closed_without_result ?? 0} ended without a result
              {totals.unclassified ? ` + ${totals.unclassified} unclassified` : ''} ={' '}
              <span className="mono">{totals.all ?? 0}</span>. &ldquo;Resolved&rdquo; means a target
              or a stop was reached — the only two states that are a trade result. Expired,
              invalidated, suppressed, missed and handed-off are real outcomes but not trade results,
              so they are counted separately rather than buried in a rate.
            </p>
          </div>

          <h2>Rates</h2>
          <div className="card">
            <div className="metric-row">
              <Metric label="win rate (of resolved)" rate={rates.target_hit_of_resolved as Rate} />
              <Metric label="stop rate (of resolved)" rate={rates.stopped_of_resolved as Rate} />
              <Metric label="target hits / all issued" rate={rates.target_hit_of_all as Rate} />
              <Metric label="resolved / all issued" rate={rates.resolved_of_all as Rate} />
              <Metric label="triggered / all issued" rate={rates.triggered_of_all as Rate} />
            </div>
            <p style={{ margin: '12px 0 0', color: 'var(--muted)' }}>
              <strong style={{ color: 'var(--text)' }}>The first two share a denominator and the
              next two do not.</strong> The win rate asks &ldquo;of the suggestions that reached a
              trade result, how many worked&rdquo;. &ldquo;Target hits / all issued&rdquo; asks
              &ldquo;of everything suggested, how much worked&rdquo; — a smaller number, because most
              suggestions never reach a result at all. Hover any figure for its exact definition.
            </p>
          </div>

          <h2>By model</h2>
          <BreakdownTable rows={data.by_model ?? []} keyField="model" labelField="short" />

          <h2>By asset</h2>
          <BreakdownTable rows={data.by_asset ?? []} keyField="asset" />

          <h2>By day</h2>
          {(data.by_day ?? []).length === 0 ? (
            <div className="card empty">
              No suggestions were issued on any day in this window. For a system whose strategies
              fire rarely by design, that is a normal result rather than a fault.
            </div>
          ) : (
            <div className="card" style={{ overflowX: 'auto' }}>
              <table className="data-table">
                <thead>
                  <tr>
                    <th>day (UTC)</th>
                    <th style={{ textAlign: 'right' }}>issued</th>
                    <th style={{ textAlign: 'right' }}>open</th>
                    <th style={{ textAlign: 'right' }}>target hit</th>
                    <th style={{ textAlign: 'right' }}>stopped</th>
                    <th style={{ textAlign: 'right' }}>no result</th>
                  </tr>
                </thead>
                <tbody>
                  {(data.by_day as any[]).map((d) => (
                    <tr key={d.day}>
                      <td className="mono">{d.day}</td>
                      <td className="mono" style={{ textAlign: 'right' }}>{d.totals.all}</td>
                      <td className="mono" style={{ textAlign: 'right' }}>{d.totals.open}</td>
                      <td className="mono long" style={{ textAlign: 'right' }}>
                        {d.totals.target_hit}
                      </td>
                      <td className="mono short" style={{ textAlign: 'right' }}>
                        {d.totals.stopped}
                      </td>
                      <td className="mono" style={{ textAlign: 'right' }}>
                        {d.totals.closed_without_result}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <footer>
            Aggregated by the engine in SQL over {data.range?.days ?? '?'} UTC days. This page does
            not poll — the numbers change when a suggestion resolves, not every few seconds. Use the
            Suggestions page&apos;s History view to see the individual records behind any figure
            here.
          </footer>
        </>
      )}
    </main>
  );
}

/**
 * Counts and the win rate per dimension value.
 *
 * The per-group rates come from the engine, computed with the same bucket definitions as the
 * totals — so a model's win rate and the overall win rate cannot disagree about what "resolved"
 * means, which they would if this table divided two numbers itself.
 */
function BreakdownTable({
  rows,
  keyField,
  labelField,
}: {
  rows: any[];
  keyField: string;
  labelField?: string;
}) {
  if (rows.length === 0) {
    return <div className="card empty">Nothing in this window.</div>;
  }
  return (
    <div className="card" style={{ overflowX: 'auto' }}>
      <table className="data-table">
        <thead>
          <tr>
            <th>{keyField}</th>
            <th style={{ textAlign: 'right' }}>issued</th>
            <th style={{ textAlign: 'right' }}>open</th>
            <th style={{ textAlign: 'right' }}>resolved</th>
            <th style={{ textAlign: 'right' }}>target hit</th>
            <th style={{ textAlign: 'right' }}>stopped</th>
            <th>win rate (of resolved)</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const rate = r.rates?.target_hit_of_resolved as Rate | undefined;
            const label = labelField ? (r[labelField] ?? r[keyField]) : r[keyField];
            return (
              <tr key={String(r[keyField])}>
                <td className="mono" title={r.name ?? undefined}>
                  {String(label).replace(/^B-/, '').replace(/_USDT$/, '')}
                </td>
                <td className="mono" style={{ textAlign: 'right' }}>{r.totals.all}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{r.totals.open}</td>
                <td className="mono" style={{ textAlign: 'right' }}>{r.totals.resolved}</td>
                <td className="mono long" style={{ textAlign: 'right' }}>{r.totals.target_hit}</td>
                <td className="mono short" style={{ textAlign: 'right' }}>{r.totals.stopped}</td>
                <td>
                  <span className={rate?.value === null ? 'muted' : 'mono'}>{pct(rate)}</span>
                  {rate && (
                    <span className="mono" style={{ color: 'var(--muted)', marginLeft: 6 }}>
                      ({rate.numerator}/{rate.denominator})
                    </span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
