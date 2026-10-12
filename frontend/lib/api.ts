/**
 * Fetching, from the browser, through our own proxy route.
 *
 * Every call goes to `/api/proxy/...` on this same Vercel deployment — never to the engine directly.
 * That is what keeps the shared token server-side and means the Azure firewall never has to open a
 * port.
 */

export type Envelope = {
  data_as_of_ms?: number | null;
  served_at_ms?: number | null;
  age_seconds?: number | null;
  system_status?: string;
  detail?: string;
};

export type ApiResult<T = any> = {
  ok: boolean;
  status: number;
  payload: T | null;
  /** Set when the request itself failed, as distinct from the engine answering with a problem. */
  transportError?: string;
};

async function get<T>(path: string, search?: Record<string, string>): Promise<ApiResult<T>> {
  const qs = search ? `?${new URLSearchParams(search).toString()}` : '';
  try {
    const res = await fetch(`/api/proxy/${path}${qs}`, { cache: 'no-store' });
    const payload = (await res.json().catch(() => null)) as T | null;
    if (payload === null) {
      // The response arrived but was not JSON — a gateway error page, a truncated body, a proxy
      // that answered before reaching the engine. Reported as a transport failure rather than as
      // `payload: null`, because a null payload reads downstream as "the engine said nothing",
      // which is indistinguishable from a calm market and is the one conflation specs/14 §10
      // forbids outright.
      return {
        ok: false,
        status: res.status,
        payload: null,
        transportError:
          `the engine's answer could not be read as JSON (HTTP ${res.status})` +
          ' — something between this page and the engine replied instead of the engine',
      };
    }
    return { ok: res.ok, status: res.status, payload };
  } catch (e) {
    // The page is open and the network is gone. Reported as its own state rather than as an empty
    // result, for the same reason the engine distinguishes DOWN from empty.
    return {
      ok: false,
      status: 0,
      payload: null,
      transportError: e instanceof Error ? e.message : 'the request could not be sent',
    };
  }
}

export const api = {
  liveSignals: () => get('api/signals/live'),
  health: () => get('api/health'),
  regime: () => get('api/regime'),
  warmup: () => get('api/warmup'),
  rejections: (asset: string) => get(`api/assets/${encodeURIComponent(asset)}/rejections`),
  /** Where each strategy series is specified and implemented. */
  specs: () => get('api/specs'),
  /** Every source the engine fetches, with its newest row and last value. */
  sources: () => get('api/sources'),
  /**
   * Live suggestions, optionally narrowed to one model.
   *
   * The model is the persisted `strategy_class` integer, passed straight through — the frontend
   * never derives a model from a strategy id or from display text, because the engine already
   * stores the classification and a second derivation is a second thing to keep in step.
   */
  liveSignalsFiltered: (strategyClass?: string) =>
    get('api/signals/live', strategyClass ? { strategy_class: strategyClass } : undefined),
  /**
   * Suggestion counts and rates for a window, aggregated by the engine.
   *
   * The aggregation is deliberately not done here: the browser receives a few dozen numbers
   * instead of the whole archive, and the UTC day boundary stays on the server where it means the
   * same thing to every reader.
   */
  analytics: (params: { start?: string; end?: string; strategyClass?: string; asset?: string;
                        state?: string }) => {
    const search: Record<string, string> = {};
    if (params.start) search.start = params.start;
    if (params.end) search.end = params.end;
    if (params.strategyClass) search.strategy_class = params.strategyClass;
    if (params.asset) search.asset = params.asset;
    if (params.state) search.state = params.state;
    return get('api/analytics', search);
  },
  /** Which UTC days have suggestions, with per-day counts. The date picker's index. */
  signalDays: (strategyClass?: string) =>
    get('api/signals/days', strategyClass ? { strategy_class: strategyClass } : undefined),
  /**
   * One page of past suggestions. Every filter is applied **in SQL** by the engine, so a page is a
   * page of matches rather than a page of rows that were then filtered down to nothing.
   */
  signalHistory: (params: {
    day?: string;
    strategyClass?: string;
    state?: string;
    asset?: string;
    limit?: number;
    cursorMs?: number;
    cursorId?: string;
  }) => {
    const search: Record<string, string> = {};
    if (params.day) search.day = params.day;
    if (params.strategyClass) search.strategy_class = params.strategyClass;
    if (params.state) search.state = params.state;
    if (params.asset) search.asset = params.asset;
    if (params.limit) search.limit = String(params.limit);
    if (params.cursorMs && params.cursorId) {
      search.cursor_ms = String(params.cursorMs);
      search.cursor_id = params.cursorId;
    }
    return get('api/signals/history', search);
  },
  /**
   * Stored OHLCV for the chart.
   *
   * `timeframe` and `limit` are validated and clamped by the engine, not here — the frontend
   * offering a fixed set of buttons is a convenience, not a guarantee, and the engine must not
   * depend on it.
   */
  candles: (asset: string, timeframe: string, limit: number) =>
    get(`api/candles/${encodeURIComponent(asset)}`, { timeframe, limit: String(limit) }),
};

/**
 * How often the page re-reads the engine.
 *
 * 15 seconds, matching the engine's own fastest poll. Faster would show the same numbers again and
 * spend the venue's rate budget through the proxy for nothing; much slower and the age counter on
 * screen starts being the only thing that moves, which is how a reader stops noticing it.
 */
export const REFRESH_MS = 15_000;
