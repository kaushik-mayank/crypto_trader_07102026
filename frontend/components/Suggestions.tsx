'use client';

/**
 * The pieces the suggestions page needs for model filtering and date browsing.
 *
 * Kept out of `Panels.tsx` because that file is the specs/15 honesty renderers and these are
 * navigation controls — but they reuse the same CSS classes (`.seg`, `.card`, `.row`, `.label`,
 * `.mono`) so the page keeps one visual language rather than acquiring a second.
 *
 * **Model labels come from the engine.** `/api/signals/live` and `/api/signals/history` publish
 * `models`, built from the `STRATEGY_CLASSES` table that mirrors `specs/03`-`specs/06`, so the names
 * on these chips are the canonical spec titles and not a second list to keep in step. The fallback
 * below exists only for an engine too old to publish them.
 */
import { ageText } from '@/lib/honesty';

export type ModelMeta = {
  id: string;
  key: string;
  name: string;
  short: string;
  mechanism: string;
  summary: string;
  count?: number;
};

/**
 * Used only when the engine does not publish `models` — an older VM, which is the ordinary state
 * for a while after every deploy (F-058). The ids are the persisted `strategy_class` integers, so
 * filtering still works correctly against an old engine; only the long names are missing.
 */
export const FALLBACK_MODELS: ModelMeta[] = [
  { id: '1', key: 'correction', short: 'Correction', name: 'Correction & Rebalancing',
    mechanism: 'restoring force', summary: 'fade dislocations' },
  { id: '2', key: 'persistence', short: 'Persistence',
    name: 'Trend Persistence & Risk-Normalized Participation',
    mechanism: 'risk-adjusted drift', summary: 'participate in trends' },
  { id: '3', key: 'transition', short: 'Transition', name: 'Anticipatory Transition',
    mechanism: 'state change', summary: 'anticipate, risk quantified' },
  { id: '4', key: 'established', short: 'Established',
    name: 'Established Methodologies & the Benchmark Suite',
    mechanism: 'external evidence', summary: 'published, documented edges' },
];

/**
 * The engine's `models` map, ordered by class, with counts where the payload carried them.
 *
 * **Any bucket in `by_model` that is not one of the four is appended, not dropped.** That omission
 * is what hid F-065 for as long as it lasted: every C1/C2/C3 signal was persisted with
 * `strategy_class = 0`, the engine counted them under a `"0"` key, and this function only ever
 * looked at keys 1-4 — so the chips read `Correction 0, Persistence 0, Transition 0, Established 13`
 * while the list below them showed far more than 13. The discrepancy was visible on screen and
 * unexplainable from it.
 *
 * A count that does not appear anywhere cannot be reconciled, so now it appears.
 */
export function modelList(payload: any): ModelMeta[] {
  const published = payload?.models;
  const counts = payload?.by_model ?? {};
  const base: ModelMeta[] = published
    ? Object.keys(published)
        .sort()
        .map((k) => ({ ...(published[k] as ModelMeta), id: String(k) }))
    : FALLBACK_MODELS;
  const known = new Set(base.map((m) => m.id));
  const extra: ModelMeta[] = Object.keys(counts)
    .filter((k) => !known.has(k) && (counts[k]?.count ?? 0) > 0)
    .sort()
    .map((k) => ({
      id: k,
      key: `unclassified-${k}`,
      short: 'Unclassified',
      name: `unclassified (strategy_class = ${k})`,
      mechanism: 'unknown',
      summary:
        'these suggestions were stored without a valid strategy class, so they belong to no ' +
        'model filter. That is an engine defect, not a market condition — report it.',
    }));
  return [...base, ...extra].map((m) => ({ ...m, count: counts[m.id]?.count }));
}

/**
 * The number of suggestions the model counts do not account for.
 *
 * Rendered next to the filter so the chips can always be reconciled against the list. If this is
 * ever non-zero, the chips and the list below them genuinely disagree and the reader deserves to
 * know which number to doubt.
 */
export function unclassifiedCount(payload: any): number {
  const counts = payload?.by_model ?? {};
  const known = new Set(Object.keys(payload?.models ?? {}));
  return Object.keys(counts)
    .filter((k) => !known.has(k))
    .reduce((n, k) => n + (counts[k]?.count ?? 0), 0);
}

/**
 * A timestamp, rendered in **UTC** with the label saying so.
 *
 * UTC and not the viewer's locale, because the day grouping is UTC: a suggestion issued at 23:30
 * UTC would otherwise appear under one date in its heading and a different one in its own
 * timestamp, for every reader east of Greenwich. One of the two has to give, and moving the
 * boundary with the viewer makes the date groups unshareable — "look at the 10th" would mean
 * different sets of suggestions to two people.
 */
export function utcStamp(ms?: number | null): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return 'time unknown';
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, '0');
  return (
    `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`
  );
}

/** The model filter. An explicit "All models" option, never an absent chip meaning all. */
export function ModelFilter({
  models,
  value,
  onChange,
}: {
  models: ModelMeta[];
  value: string;
  onChange: (next: string) => void;
}) {
  return (
    <div className="control">
      <span className="label">model</span>
      <div className="seg" role="group" aria-label="Model">
        <button type="button" aria-pressed={value === ''} onClick={() => onChange('')}
                title="every model">
          All
        </button>
        {models.map((m) => (
          <button
            key={m.id}
            type="button"
            aria-pressed={value === m.id}
            onClick={() => onChange(m.id)}
            /* The canonical name and mechanism in the tooltip; the chip has room for neither. */
            title={`${m.name} — ${m.mechanism}: ${m.summary}`}
          >
            {m.short}
            {typeof m.count === 'number' && <span className="chip-count"> {m.count}</span>}
          </button>
        ))}
      </div>
    </div>
  );
}

/**
 * The date picker, driven by the engine's day index rather than by a calendar widget.
 *
 * Only days that **have** suggestions are offered, with their counts, so the reader cannot pick an
 * empty day and wonder whether the filter is broken or the market was quiet. The index is one
 * aggregated query — the alternative, fetching every suggestion and grouping in the browser, grows
 * without bound and puts the date boundary in the viewer's timezone.
 */
export function DayPicker({
  days,
  value,
  onChange,
}: {
  days: { day: string; total: number; open: number; target_hit: number; stopped: number }[];
  value: string;
  onChange: (next: string) => void;
}) {
  if (days.length === 0) {
    return (
      <div className="card empty">
        <strong style={{ color: 'var(--text)' }}>No suggestions have been recorded yet</strong>
        <p style={{ margin: '6px 0 0' }}>
          The engine answered — this is not a connection problem. Nothing has been issued, which for
          a system whose strategies fire rarely is the normal early state.
        </p>
      </div>
    );
  }
  return (
    <div className="control" style={{ minWidth: 0 }}>
      <span className="label">date (UTC)</span>
      <div className="day-strip">
        {days.map((d) => (
          <button
            key={d.day}
            type="button"
            className={value === d.day ? 'day active' : 'day'}
            aria-pressed={value === d.day}
            onClick={() => onChange(d.day)}
          >
            <span className="day-date">{d.day}</span>
            <span className="day-counts">
              {d.total} total
              {d.open > 0 && <> · {d.open} open</>}
              {d.target_hit > 0 && <> · <span className="long">{d.target_hit} hit</span></>}
              {d.stopped > 0 && <> · <span className="short">{d.stopped} stopped</span></>}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

/** The status filter, over the lifecycle states a reader actually wants to narrow to. */
export const STATUS_OPTIONS = [
  { value: '', label: 'Any' },
  { value: 'TARGET_HIT', label: 'Target hit' },
  { value: 'STOPPED', label: 'Stopped' },
  { value: 'EXPIRED', label: 'Expired' },
  { value: 'INVALIDATED', label: 'Invalidated' },
  { value: 'TRIGGERED', label: 'Triggered' },
  { value: 'SETUP_DETECTED', label: 'Setup detected' },
];

export function StatusFilter({
  value,
  onChange,
}: {
  value: string;
  onChange: (next: string) => void;
}) {
  return (
    <div className="control">
      <span className="label">status</span>
      <select className="select" value={value} onChange={(e) => onChange(e.target.value)}
              aria-label="Status">
        {STATUS_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
  );
}

/**
 * One historical suggestion: what it was, when it was issued, and how it ended.
 *
 * **Two timestamps, labelled apart.** "issued" is `created_at_ms`, which never moves; "outcome"
 * is `state_since_ms`, which moves when the lifecycle changes. Showing one number would make a
 * suggestion from Tuesday whose stop was hit on Thursday look like a Thursday suggestion.
 */
export function HistoryRow({ signal, models }: { signal: any; models: ModelMeta[] }) {
  const state = String(signal.state ?? signal.lifecycle ?? '');
  const model = models.find((m) => m.id === String(signal.strategy_class));
  const targets: string[] = Array.isArray(signal.targets) ? signal.targets.map(String) : [];
  const outcome = outcomeClass(state);
  const issued = signal.created_at_ms;
  const changed = signal.state_since_ms;

  return (
    <div className="card history-row">
      <div className="row">
        <strong className="mono">{signal.asset}</strong>
        <strong className={String(signal.direction).toUpperCase() === 'LONG' ? 'long' : 'short'}>
          {String(signal.direction).toUpperCase()}
        </strong>
        <span className="mono">{signal.strategy_id}</span>
        {model && <span className="age age-ok" title={model.name}>{model.short}</span>}
        <span className={`outcome outcome-${outcome}`}>{state.replace(/_/g, ' ').toLowerCase()}</span>
      </div>

      <div className="row" style={{ marginTop: 8 }}>
        <span>
          <span className="label">issued</span>
          <span className="mono">{utcStamp(issued)}</span>
        </span>
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
      </div>

      {/* Only shown when the outcome is genuinely later than the issue. For a suggestion that has
          not changed state since it was written, a second identical timestamp is noise. */}
      {typeof changed === 'number' && typeof issued === 'number' && changed > issued + 60_000 && (
        <p style={{ margin: '8px 0 0', color: 'var(--muted)' }}>
          <span className="label">outcome recorded</span>
          <span className="mono">{utcStamp(changed)}</span>
          <span style={{ marginLeft: 8 }}>({ageText((Date.now() - changed) / 1000)})</span>
        </p>
      )}
    </div>
  );
}

/** Which visual family an outcome belongs to. Words carry it too — never colour alone. */
export function outcomeClass(state: string): 'hit' | 'stopped' | 'open' | 'closed' {
  const s = state.toUpperCase();
  if (s === 'TARGET_HIT') return 'hit';
  if (s === 'STOPPED') return 'stopped';
  if (s === 'SETUP_DETECTED' || s === 'ENTER_NOW' || s === 'TRIGGERED') return 'open';
  return 'closed';
}
