'use client';

/**
 * A candlestick chart, drawn as plain SVG.
 *
 * **No charting library, and that is a decision rather than an omission.** A library would be fewer
 * lines, but it would also be a dependency whose build I cannot verify here — and a chart that fails
 * to compile on Vercel is worse than one written out longhand. SVG also means the candles are real
 * DOM nodes, so the whole thing is inspectable and prints.
 *
 * What it must get right, in order of how badly a mistake would mislead:
 *
 * 1. **Non-final bars look different.** The most recent bar has usually been fetched once and not
 *    yet confirmed by a second fetch, so the feature layer will not compute on it. Drawing it
 *    identically to a settled bar would show the reader a price the engine is deliberately ignoring.
 * 2. **Gaps are visible as gaps.** Candles are positioned by *time*, not by array index, so a
 *    missing hour leaves a hole. Index positioning would silently close the gap and make an outage
 *    look like a quiet market — which is the whole failure this page exists to catch.
 * 3. **The price axis never lies by omission.** It is labelled with real prices at real positions,
 *    and the range comes from the data rather than from a round number that happens to look tidy.
 */
import { useEffect, useMemo, useRef, useState } from 'react';

export type Candle = {
  t: number;
  o: string;
  h: string;
  l: string;
  c: string;
  v: string;
  final: boolean;
};

type Props = {
  candles: Candle[];
  timeframeMs: number;
  height?: number;
};

const PAD = { top: 12, right: 64, bottom: 28, left: 8 };
const VOLUME_FRACTION = 0.18;

function niceTicks(min: number, max: number, count = 5): number[] {
  if (!(max > min)) return [min];
  const raw = (max - min) / count;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) ?? magnitude * 10;
  const first = Math.ceil(min / step) * step;
  const out: number[] = [];
  for (let v = first; v <= max; v += step) out.push(v);
  return out;
}

function formatPrice(v: number): string {
  if (v >= 1000) return v.toLocaleString(undefined, { maximumFractionDigits: 0 });
  if (v >= 1) return v.toFixed(2);
  return v.toPrecision(4);
}

function formatTime(ms: number, timeframeMs: number): string {
  const d = new Date(ms);
  // Below a day, the clock matters more than the date; at or above it, the reverse.
  if (timeframeMs < 86_400_000) {
    return d.toLocaleString(undefined, {
      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false,
    });
  }
  return d.toLocaleDateString(undefined, { year: '2-digit', month: 'short', day: 'numeric' });
}

export function CandleChart({ candles, timeframeMs, height = 420 }: Props) {
  const [hover, setHover] = useState<number | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(900);

  // Measured rather than assumed, so the chart fits a phone as well as a monitor.
  //
  // `useEffect`, not `useMemo`: a memo body runs during render, where calling `setWidth` is an
  // update-while-rendering error, and where the returned cleanup is discarded so the resize
  // listener would accumulate one per render.
  useEffect(() => {
    const measure = () => setWidth(wrapRef.current?.clientWidth ?? 900);
    measure();
    window.addEventListener('resize', measure);
    return () => window.removeEventListener('resize', measure);
  }, []);

  const model = useMemo(() => {
    if (candles.length === 0) return null;

    const nums = candles.map((c) => ({
      t: c.t,
      o: Number(c.o), h: Number(c.h), l: Number(c.l), c: Number(c.c), v: Number(c.v),
      final: c.final,
    }));
    // A non-finite price would silently collapse the whole scale, so they are dropped and counted
    // rather than drawn — and the caller is told, because a dropped bar is a data problem.
    const clean = nums.filter((d) => [d.o, d.h, d.l, d.c].every(Number.isFinite));
    if (clean.length === 0) return null;

    const lo = Math.min(...clean.map((d) => d.l));
    const hi = Math.max(...clean.map((d) => d.h));
    const span = hi - lo || hi * 0.01 || 1;
    const pad = span * 0.06;
    const yMin = lo - pad;
    const yMax = hi + pad;

    const tMin = clean[0].t;
    const tMax = clean[clean.length - 1].t + timeframeMs;

    const plotW = Math.max(40, width - PAD.left - PAD.right);
    const volH = height * VOLUME_FRACTION;
    const priceH = height - PAD.top - PAD.bottom - volH;

    const x = (t: number) => PAD.left + ((t - tMin) / (tMax - tMin)) * plotW;
    const y = (p: number) => PAD.top + (1 - (p - yMin) / (yMax - yMin)) * priceH;

    const maxV = Math.max(...clean.map((d) => (Number.isFinite(d.v) ? d.v : 0)), 1);
    const vy = (v: number) => height - PAD.bottom - (Number.isFinite(v) ? v / maxV : 0) * volH;

    // Width from the time step, so a gap stays a gap. One pixel minimum: at 5,000 candles a body is
    // sub-pixel, and a candle you cannot see is indistinguishable from one that is not there.
    const bodyW = Math.max(1, (plotW / Math.max(1, (tMax - tMin) / timeframeMs)) * 0.7);

    return {
      clean, dropped: nums.length - clean.length,
      x, y, vy, yMin, yMax, tMin, tMax, plotW, priceH, volH, bodyW,
      ticks: niceTicks(yMin, yMax),
    };
  }, [candles, timeframeMs, width, height]);

  if (!model) {
    return (
      <div ref={wrapRef} className="card empty" style={{ height }}>
        <strong style={{ color: 'var(--text)' }}>Nothing to draw</strong>
        <p style={{ margin: '6px 0 0' }}>
          No usable candles came back for this asset and interval. That is not the same as a flat
          market — it means the engine has no bars stored here yet.
        </p>
      </div>
    );
  }

  const { clean, x, y, vy, bodyW, ticks, dropped } = model;
  const hovered = hover === null ? null : clean[hover];

  // Time labels: about six, evenly spaced by index so they never collide.
  const labelEvery = Math.max(1, Math.floor(clean.length / 6));

  return (
    <div ref={wrapRef} style={{ position: 'relative' }}>
      {dropped > 0 && (
        <p className="age age-amber" style={{ margin: '0 0 6px' }}>
          {dropped} bar{dropped === 1 ? '' : 's'} could not be read as a number and {dropped === 1 ? 'was' : 'were'} left
          out — that is a data problem, not a quiet market.
        </p>
      )}

      <svg
        width="100%"
        height={height}
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        role="img"
        aria-label={`Candlestick chart, ${clean.length} intervals`}
        onMouseLeave={() => setHover(null)}
        onMouseMove={(e) => {
          const box = e.currentTarget.getBoundingClientRect();
          const px = ((e.clientX - box.left) / box.width) * width;
          // Nearest by pixel distance, so hovering in a gap picks the closer neighbour rather than
          // snapping to an index that is nowhere near the cursor.
          let best = 0;
          let bestD = Infinity;
          clean.forEach((d, i) => {
            const dist = Math.abs(x(d.t) + bodyW / 2 - px);
            if (dist < bestD) { bestD = dist; best = i; }
          });
          setHover(best);
        }}
      >
        {/* Price gridlines and labels on the right, where the latest price is. */}
        {ticks.map((p) => (
          <g key={p}>
            <line x1={PAD.left} x2={width - PAD.right} y1={y(p)} y2={y(p)}
                  stroke="var(--border)" strokeWidth={1} />
            <text x={width - PAD.right + 6} y={y(p) + 4} fill="var(--muted)" fontSize={11}
                  fontFamily="ui-monospace, monospace">{formatPrice(p)}</text>
          </g>
        ))}

        {/* Volume, underneath and deliberately muted: context, not the subject. */}
        {clean.map((d) => (
          <rect key={`v${d.t}`} x={x(d.t)} y={vy(d.v)} width={bodyW}
                height={Math.max(0, height - PAD.bottom - vy(d.v))}
                fill={d.c >= d.o ? 'var(--long)' : 'var(--short)'} opacity={0.22} />
        ))}

        {/* The candles. */}
        {clean.map((d, i) => {
          const up = d.c >= d.o;
          const colour = up ? 'var(--long)' : 'var(--short)';
          const bodyTop = y(Math.max(d.o, d.c));
          const bodyBottom = y(Math.min(d.o, d.c));
          const cx = x(d.t) + bodyW / 2;
          return (
            <g key={d.t} opacity={hover === null || hover === i ? 1 : 0.75}>
              <line x1={cx} x2={cx} y1={y(d.h)} y2={y(d.l)} stroke={colour} strokeWidth={1} />
              <rect
                x={x(d.t)} y={bodyTop} width={bodyW}
                height={Math.max(1, bodyBottom - bodyTop)}
                fill={d.final ? colour : 'none'}
                stroke={colour}
                strokeWidth={d.final ? 0 : 1.5}
                /* Hollow with a dashed outline when the bar has not been confirmed by a second
                   fetch. The engine will not compute on it, so the chart must not present it as
                   settled. */
                strokeDasharray={d.final ? undefined : '2 2'}
              />
            </g>
          );
        })}

        {/* Time axis. */}
        {clean.map((d, i) =>
          i % labelEvery === 0 ? (
            <text key={`t${d.t}`} x={x(d.t)} y={height - 8} fill="var(--muted)" fontSize={11}
                  textAnchor="middle">{formatTime(d.t, timeframeMs)}</text>
          ) : null,
        )}

        {/* Crosshair on the hovered candle. */}
        {hovered && (
          <line x1={x(hovered.t) + bodyW / 2} x2={x(hovered.t) + bodyW / 2}
                y1={PAD.top} y2={height - PAD.bottom}
                stroke="var(--muted)" strokeWidth={1} strokeDasharray="3 3" opacity={0.6} />
        )}
      </svg>

      {hovered && (
        <div
          className="card"
          style={{
            position: 'absolute', top: 8, left: 12, margin: 0, padding: '8px 12px',
            pointerEvents: 'none', fontSize: 12, lineHeight: 1.7,
          }}
        >
          <div className="mono" style={{ color: 'var(--muted)' }}>
            {formatTime(hovered.t, timeframeMs)}
          </div>
          <div className="mono">
            O {formatPrice(hovered.o)} &nbsp; H {formatPrice(hovered.h)} &nbsp;
            L {formatPrice(hovered.l)} &nbsp;
            <span className={hovered.c >= hovered.o ? 'long' : 'short'}>
              C {formatPrice(hovered.c)}
            </span>
          </div>
          <div className="mono" style={{ color: 'var(--muted)' }}>
            vol {Number.isFinite(hovered.v) ? hovered.v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : '—'}
          </div>
          {!hovered.final && (
            <div className="age age-amber">
              not yet confirmed — the engine will not compute on this bar
            </div>
          )}
        </div>
      )}
    </div>
  );
}
