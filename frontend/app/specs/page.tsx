'use client';

/**
 * The specs page: where each strategy series is defined, and where it is implemented.
 *
 * **Informational only, and served by the engine.** The mapping is built from
 * `src/api/spec_map.py`'s table merged with the live strategy registry, so the implementation half
 * is read from the code at runtime and cannot drift from it. Nothing here restates a trading rule:
 * a second copy of a threshold or a formula would be a second thing to keep correct, and the
 * specification files are deliberately the only one.
 *
 * It reuses the existing `.card`, `.row`, `.label`, `.mono` and `.data-table` classes, so the page
 * looks like the rest of the dashboard rather than introducing a second visual language.
 */
import { useCallback, useEffect, useState } from 'react';

import { Down } from '@/components/Panels';
import { api } from '@/lib/api';

type Series = {
  series: string;
  strategy_class: number;
  tradeable: boolean;
  version: string;
  tier: string;
  implementation: string | null;
  implementation_line: number | null;
  spec_file: string | null;
  spec_section: string | null;
  title: string | null;
  mechanism: string | null;
};

const CLASS_NAMES: Record<number, string> = {
  1: 'Correction & Rebalancing',
  2: 'Trend Persistence & Risk-Normalized Participation',
  3: 'Anticipatory Transition',
  4: 'Established Methodologies & the Benchmark Suite',
};

/** Render `**bold**` only. The mechanism strings use it for the one clause that matters. */
function emphasise(text: string) {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith('**') && part.endsWith('**') ? (
      <strong key={i}>{part.slice(2, -2)}</strong>
    ) : (
      <span key={i}>{part}</span>
    ),
  );
}

export default function SpecsPage() {
  const [data, setData] = useState<any | null>(null);
  const [problem, setProblem] = useState<Record<string, any> | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const res = await api.specs();
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
      if (!Array.isArray(body.data?.series)) {
        setProblem({
          problem_class: 'application_bug',
          message: 'the engine answered, but not with a specification index',
          likely_cause: 'the VM is running a version without /api/specs',
          remediation: 'update the VM: sudo bash /opt/crypto-trader/deploy/azure/update.sh',
        });
        return;
      }
      setData(body.data);
      setProblem(null);
    } finally {
      setLoading(false);
    }
  }, []);

  // Read once. The mapping changes when the code changes, not while the page is open, so a timer
  // here would spend requests to receive the same answer.
  useEffect(() => {
    void load();
  }, [load]);

  const series: Series[] = data?.series ?? [];
  const byClass = [1, 2, 3, 4].map((c) => ({
    klass: c,
    rows: series.filter((s) => s.strategy_class === c),
  }));

  return (
    <main className="wrap">
      <h1>Specs</h1>
      <p style={{ color: 'var(--muted)', marginTop: 0 }}>
        Where each strategy is defined, and where it is implemented. The specification files are the
        authority for every trading rule — this page says where to find them and never restates
        them, because a second copy would be a second thing to keep correct.
      </p>

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

      {loading && !data && <div className="card empty">Reading the specification index…</div>}

      {data && (
        <>
          {/* A series present in the code but not in the index, or the reverse, is shown rather
              than quietly omitted. A Specs page that silently drops a strategy is worse than none. */}
          {((data.unmapped ?? []).length > 0 || (data.orphaned ?? []).length > 0) && (
            <div className="card down">
              <h3>This index is incomplete</h3>
              {(data.unmapped ?? []).length > 0 && (
                <p style={{ margin: '0 0 4px' }}>
                  <span className="label">in the code, not in the index</span>
                  <span className="mono">{(data.unmapped as string[]).join(', ')}</span>
                </p>
              )}
              {(data.orphaned ?? []).length > 0 && (
                <p style={{ margin: 0 }}>
                  <span className="label">in the index, not in the code</span>
                  <span className="mono">{(data.orphaned as string[]).join(', ')}</span>
                </p>
              )}
            </div>
          )}

          {byClass.map(({ klass, rows }) => (
            <section key={klass}>
              <h2>
                Class {klass} — {CLASS_NAMES[klass]}
              </h2>
              {rows.length === 0 ? (
                <div className="card empty">No series registered in this class.</div>
              ) : (
                rows.map((s) => (
                  <div className="card" key={s.series}>
                    <div className="row">
                      <strong className="mono">{s.series}</strong>
                      <strong>{s.title ?? 'untitled'}</strong>
                      {!s.tradeable && (
                        <span className="age age-amber">control — never tradeable</span>
                      )}
                      {s.tier && <span className="age age-ok">Tier {s.tier}</span>}
                      <span className="age age-ok mono">v{s.version}</span>
                    </div>

                    {s.mechanism && (
                      <p style={{ margin: '8px 0 0' }}>{emphasise(s.mechanism)}</p>
                    )}

                    <div className="row" style={{ marginTop: 10 }}>
                      <span>
                        <span className="label">specified in</span>
                        <span className="mono">
                          {s.spec_file ?? 'not mapped'}
                          {s.spec_section ? ` ${s.spec_section}` : ''}
                        </span>
                      </span>
                      <span>
                        <span className="label">implemented in</span>
                        <span className="mono">
                          {s.implementation ?? 'unknown'}
                          {s.implementation_line ? `:${s.implementation_line}` : ''}
                        </span>
                      </span>
                    </div>
                  </div>
                ))
              )}
            </section>
          ))}

          <h2>Rules that apply to every strategy</h2>
          <div className="card" style={{ overflowX: 'auto' }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>topic</th>
                  <th>file</th>
                  <th>covers</th>
                </tr>
              </thead>
              <tbody>
                {(data.cross_cutting ?? []).map((d: any) => (
                  <tr key={d.file}>
                    <td>{d.topic}</td>
                    <td className="mono">{d.file}</td>
                    <td style={{ color: 'var(--muted)' }}>{d.detail}</td>
                  </tr>
                ))}
                {data.capability_map && (
                  <tr>
                    <td>Exchange capabilities</td>
                    <td className="mono">{data.capability_map.file}</td>
                    <td style={{ color: 'var(--muted)' }}>{data.capability_map.note}</td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <footer>
            {data.note} The implementation paths above are read from the running engine&apos;s own
            strategy registry, so they cannot drift from the code.
          </footer>
        </>
      )}
    </main>
  );
}
