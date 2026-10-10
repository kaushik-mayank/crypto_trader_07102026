'use client';

/**
 * The panels, and the states a panel can be in.
 *
 * Every component here exists to keep one of the specs/15 failure modes from happening. They are
 * small on purpose: all the judgement lives in `lib/honesty.ts` and in the engine's own payload, so
 * these are renderers, not deciders.
 */
import { ageState, ageText, concentrationNotice, confidenceCopy, directionPending, displayable, priceBasis, signalDetail, validationNotice, type PanelState } from '@/lib/honesty';

/** Age, always as text and colour together — never colour alone. */
export function Freshness({ ageSeconds, label = 'data' }: { ageSeconds?: number | null; label?: string }) {
  const state = ageState(ageSeconds);
  return (
    <span className={`age age-${state}`}>
      {label} {ageText(ageSeconds)}
      {state === 'red' && ' — too old to act on'}
      {state === 'amber' && ' — getting stale'}
      {state === 'unknown' && ' — the engine did not say how old this is'}
    </span>
  );
}

/**
 * The engine could not answer. specs/14 §10: rendering this as an empty market is the most
 * dangerous thing this dashboard could do, so it looks nothing like one.
 */
export function Down({ state }: { state: Extract<PanelState, { kind: 'down' }> }) {
  return (
    <div className="card down">
      <h3>Cannot tell — the engine did not answer</h3>
      <p style={{ margin: '0 0 8px' }}>{state.message}</p>
      {state.likelyCause && (
        <p style={{ margin: '0 0 4px' }}>
          <span className="label">likely cause</span>
          {state.likelyCause}
        </p>
      )}
      {state.remediation && (
        <p style={{ margin: '0 0 4px' }}>
          <span className="label">what to do</span>
          {state.remediation}
        </p>
      )}
      {state.correlationId && <p className="mono" style={{ margin: 0 }}>id {state.correlationId}</p>}
      <p style={{ margin: '8px 0 0', fontWeight: 600 }}>
        This is not &ldquo;no opportunities&rdquo;. Nothing on this page is known to be current.
      </p>
    </div>
  );
}

/** specs/15 failure mode 4: warm-up is not failure, and it says how much longer. */
export function Warming({ detail }: { detail: string }) {
  return (
    <div className="card warming">
      <strong>Warming up</strong>
      <p style={{ margin: '6px 0 0' }}>{detail}</p>
      <p style={{ margin: '6px 0 0', color: 'var(--muted)' }}>
        Nothing is wrong. The engine is still accumulating the history its longest measurements need.
      </p>
    </div>
  );
}

/** specs/15 failure mode 1: a quiet market states the binding condition, never a blank box. */
export function Empty({ reason }: { reason: string }) {
  return (
    <div className="card empty">
      <strong style={{ color: 'var(--text)' }}>No suggestions right now</strong>
      <p style={{ margin: '6px 0 0' }}>{reason}</p>
      <p style={{ margin: '6px 0 0' }}>
        A quiet market is the normal state. The engine is working and has nothing to say.
      </p>
    </div>
  );
}

type Signal = Record<string, any>;

/**
 * True when the engine says this signal has no direction yet (C3-A, C3-C arm pending).
 *
 * Via `lib/honesty.ts` so the older-engine fallbacks apply here too: while the VM lags behind this
 * deployment, `pending_direction` is absent and a placeholder direction would be rendered as a
 * recommendation exactly as before the fix (F-058).
 */
function isPending(s: Signal): boolean {
  return directionPending(s);
}

/** True when the signal's levels are a spread rather than a price (C1-C). */
function isSpread(s: Signal): boolean {
  return priceBasis(s) !== 'price';
}

/** specs/15: ENTER_NOW is relocated to the top, with three independent cues before colour. */
export function ActionBar({ signals }: { signals: Signal[] }) {
  // A pending-direction setup is excluded: "ENTER NOW — BTC LONG at —" is an instruction nobody
  // can follow, built from a placeholder direction and a level that does not exist yet.
  const now = signals.filter(
    (s) => String(s.lifecycle).toUpperCase() === 'ENTER_NOW' && !isPending(s),
  );
  if (now.length === 0) return null;
  return (
    <>
      {now.map((s) => (
        <div className="banner" key={s.signal_id}>
          ENTER NOW — {s.asset} {String(s.direction).toUpperCase()}{' '}
          {isSpread(s) ? '(two-leg spread — see the card below)' : `at ${s.entry ?? '—'}`}
          {s.sizing?.size ? ` · size ${s.sizing.size}` : ''}
        </div>
      ))}
    </>
  );
}

export function SignalCard({ signal }: { signal: Signal }) {
  // The third firewall layer. The engine filters controls out and the controls refuse to run live;
  // this is written as if neither did.
  if (!displayable(signal)) return null;

  const detail = signalDetail(signal);
  const dir = String(signal.direction ?? '').toUpperCase();
  const notice = validationNotice(signal);
  const targets: string[] = Array.isArray(signal.targets) ? signal.targets.map(String) : [];

  // Two things the engine publishes that this card used to throw away, each of which made it show
  // something untrue.
  //
  // `pending_direction` — C3-A and C3-C arm *without* a direction and let the market assign it. The
  // engine sets `direction` to LONG as a structural placeholder and says so in the payload. The card
  // printed that placeholder in green next to "LONG", so a setup whose entire premise is "we do not
  // predict which way this breaks" was displayed as a long suggestion (F-053).
  //
  // `price_basis` — C1-C's levels are a beta-adjusted spread, not a price, and are negative about
  // half the time. Rendered under a heading marked *entry* they are a price that exists nowhere
  // (F-051).
  const pendingDirection = directionPending(signal);
  const basis = priceBasis(signal);
  const isPrice = basis === 'price';
  const legs: any[] = Array.isArray(detail.legs) ? detail.legs : [];

  return (
    <div className="card">
      <div className="row">
        <strong className="mono">{signal.asset}</strong>
        {pendingDirection ? (
          <strong className="age age-amber" style={{ fontSize: 13 }}>
            DIRECTION NOT YET DECIDED
          </strong>
        ) : (
          <strong className={dir === 'LONG' ? 'long' : 'short'}>{dir}</strong>
        )}
        <span className="mono">{signal.strategy_id}</span>
        <span className="age age-ok">{String(signal.lifecycle ?? '')}</span>
        {!isPrice && <span className="age age-amber">{basis} units — not a price</span>}
      </div>

      {pendingDirection && (
        <p style={{ margin: '8px 0 0' }}>
          This setup is watching a level and will trade <strong>whichever way it resolves</strong>.
          There is no entry or stop yet because the direction decides them.
          {detail.boundary && (
            <>
              {' '}
              The level being watched is <span className="mono">{String(detail.boundary)}</span>.
            </>
          )}
          {detail.resolution_rule && (
            <>
              {' '}
              <span style={{ color: 'var(--muted)' }}>{String(detail.resolution_rule)}</span>
            </>
          )}
        </p>
      )}

      {/* The levels row is suppressed entirely for a non-price basis. Showing "entry -0.04" with a
          caveat underneath would still be a number under the word entry, and a reader skimming for
          a price finds one. The legs below are the executable instruction. */}
      {isPrice && !pendingDirection && (
        <div className="row" style={{ marginTop: 10 }}>
          <span>
            <span className="label">entry</span>
            <span className="mono">{signal.entry ?? '—'}</span>
          </span>
          <span>
            <span className="label">stop</span>
            <span className="mono">{signal.stop ?? '—'}</span>
          </span>
          <span>
            <span className="label">target</span>
            <span className="mono">{targets.length ? targets.join(', ') : 'none — trailed'}</span>
          </span>
          {signal.sizing?.size && (
            <span>
              <span className="label">size</span>
              <span className="mono">{signal.sizing.size}</span>
            </span>
          )}
        </div>
      )}

      {!isPrice && (
        <div style={{ marginTop: 10 }}>
          <div className="unvalidated" style={{ borderStyle: 'solid' }}>
            <strong>This is a two-leg spread trade, not a single position.</strong> The numbers this
            strategy works in are {basis} units — a beta-adjusted spread that is often negative and
            is <strong>not a price</strong>. What you would actually place is the pair below; the
            notionals differ by design, and doing one leg without the other is a different trade
            with a different risk.
          </div>
          {legs.length > 0 ? (
            <div style={{ marginTop: 10 }}>
              {legs.map((leg, i) => (
                <div className="row" key={`${leg.asset}-${i}`}>
                  <strong className="mono">{String(leg.asset)}</strong>
                  <strong className={String(leg.direction).toUpperCase() === 'LONG' ? 'long' : 'short'}>
                    {String(leg.direction).toUpperCase()}
                  </strong>
                  <span>
                    <span className="label">price</span>
                    <span className="mono">{String(leg.price ?? '—')}</span>
                  </span>
                  <span>
                    <span className="label">notional weight</span>
                    <span className="mono">{String(leg.notional_weight ?? '—')}</span>
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="age age-red" style={{ marginTop: 8 }}>
              The engine did not publish the leg table for this spread trade, so there is nothing
              here that could be executed. Treat it as information only and report it — a two-leg
              signal without its legs is an engine defect, not a market condition.
            </p>
          )}
          <p style={{ margin: '8px 0 0', color: 'var(--muted)' }}>
            <span className="label">spread levels (for reference, not for placing)</span>
            entry <span className="mono">{signal.entry ?? '—'}</span>, stop{' '}
            <span className="mono">{signal.stop ?? '—'}</span>, target{' '}
            <span className="mono">{targets.length ? targets.join(', ') : '—'}</span>
          </p>
        </div>
      )}

      {/* specs/15 failure mode 2: the blank confidence gets replaced by what the trade needs. */}
      <p style={{ margin: '10px 0 0', color: 'var(--muted)' }}>{confidenceCopy(signal)}</p>

      {signal.rationale && <p style={{ margin: '10px 0 0' }}>{signal.rationale}</p>}

      {notice && <div className="unvalidated">{notice}</div>}
    </div>
  );
}

/**
 * specs/15 failure mode 5: say that correlated longs are one position.
 *
 * **Which signals count as directional is the whole correctness of this panel**, and the first
 * version counted all of them. It read "long 5, short 4" and warned about five correlated longs on a
 * set that included two pending-direction setups — which have no direction, only a placeholder — and
 * two beta-neutral spreads, which are *market-neutral by construction* and are the opposite of
 * concentrated directional risk. The warning was therefore wrong in both directions at once:
 * inflated, and inflated by exactly the signals that do not add directional exposure (F-053).
 *
 * The three kinds are now counted separately, because they are three different things to know.
 */
export function ExposureStrip({ signals }: { signals: Signal[] }) {
  const shown = signals.filter(displayable);
  const pending = shown.filter(isPending);
  const spreads = shown.filter((s) => !isPending(s) && isSpread(s));
  const directional = shown.filter((s) => !isPending(s) && !isSpread(s));

  const longs = directional.filter((s) => String(s.direction).toUpperCase() === 'LONG').length;
  const shorts = directional.length - longs;
  const notice = concentrationNotice(longs, shorts);

  return (
    <div className="card">
      <div className="row">
        <span>
          <span className="label">long</span>
          <span className="mono long">{longs}</span>
        </span>
        <span>
          <span className="label">short</span>
          <span className="mono short">{shorts}</span>
        </span>
        {spreads.length > 0 && (
          <span>
            <span className="label">two-leg spreads</span>
            <span className="mono">{spreads.length}</span>
          </span>
        )}
        {pending.length > 0 && (
          <span>
            <span className="label">awaiting direction</span>
            <span className="mono">{pending.length}</span>
          </span>
        )}
      </div>
      {notice && <p style={{ margin: '8px 0 0', color: 'var(--act)' }}>{notice}</p>}
      {(spreads.length > 0 || pending.length > 0) && (
        <p style={{ margin: '8px 0 0', color: 'var(--muted)' }}>
          {spreads.length > 0 && (
            <>
              Spreads are counted apart from long and short because they are beta-neutral by
              construction — they are a bet on a relationship, not on direction.{' '}
            </>
          )}
          {pending.length > 0 && (
            <>
              Setups awaiting direction have none yet: they are watching a level and will trade
              whichever way it resolves.
            </>
          )}
        </p>
      )}
    </div>
  );
}

/**
 * Health, grouped by whose problem it is.
 *
 * The engine classifies every failure as an application bug, a configuration mistake, local
 * infrastructure, an external dependency, data quality, or a modelling limit. Showing that
 * classification is the whole point: it is the difference between "read the code" and "restart the
 * tunnel", and a dashboard that renders every failure the same way throws it away.
 */
export function HealthPanel({ payload }: { payload: any }) {
  // Three different nothings, and they had all been collapsed into one sentence — "the engine did
  // not report any component health" — which the first deployment showed for an engine that could
  // not be reached at all. Each now says which nothing it is.
  if (payload === null || payload === undefined) {
    return (
      <div className="card down">
        <h3>Cannot tell — no health reading arrived</h3>
        <p style={{ margin: 0 }}>
          The dashboard has no health payload at all, which is not the same as the engine reporting
          itself unhealthy. Nothing on this page is known to be current.
        </p>
      </div>
    );
  }
  if (!Array.isArray(payload.components)) {
    return (
      <div className="card down">
        <h3>Cannot tell — the health reply was not the expected shape</h3>
        <p style={{ margin: '0 0 8px' }}>
          Something answered, but without a component list. That is an application or version
          problem, not a market one.
        </p>
        <p style={{ margin: 0 }}>
          <span className="label">what to do</span>
          the VM and this deployment are probably on different versions — redeploy both from the
          same commit
        </p>
      </div>
    );
  }
  const components: any[] = payload.components;
  if (!components.length) {
    return (
      <div className="card empty">
        <strong style={{ color: 'var(--text)' }}>The engine reported an empty health list</strong>
        <p style={{ margin: '6px 0 0' }}>
          It answered, and said it is monitoring nothing. The engine always registers at least a
          store check and a runner check, so an empty list means it was built without its health
          source attached — an application bug rather than anything wrong with the market.
        </p>
      </div>
    );
  }
  const bad = components.filter((c) => String(c.status).toUpperCase() !== 'HEALTHY');
  return (
    <div className="card">
      <div className="row">
        <strong>{components.length - bad.length} healthy</strong>
        {bad.length > 0 && <strong className="short">{bad.length} not healthy</strong>}
      </div>
      {bad.map((c) => (
        <div key={c.name} style={{ marginTop: 10 }}>
          <div className="row">
            <span className="mono">{c.name}</span>
            <span className="short">{c.status}</span>
            {c.problem_class && <span className="age age-amber">{c.problem_class}</span>}
          </div>
          {c.detail && <p style={{ margin: '4px 0 0', color: 'var(--muted)' }}>{c.detail}</p>}
          {c.remediation && <p style={{ margin: '4px 0 0' }}>{c.remediation}</p>}
        </div>
      ))}
    </div>
  );
}
