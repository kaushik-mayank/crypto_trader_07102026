/**
 * Server-side proxy to the Azure VM's API.
 *
 * This file is the whole reason the dashboard can be public while the engine is not. The browser
 * talks only to this route; this route adds the shared token and forwards to the VM. So:
 *
 *  - the token lives in a Vercel environment variable and **never reaches the browser**. It is read
 *    here, on Vercel's server. A `NEXT_PUBLIC_` variable would be compiled into the JavaScript the
 *    browser downloads, which is why neither variable has that prefix;
 *  - the VM needs no CORS configuration, because no browser ever calls it directly;
 *  - only this one route needs to know the engine's address, so moving the engine is a
 *    one-variable change.
 *
 * **Only GET, and only an allow-listed set of paths.** The engine's API is read-only by
 * construction — there is no endpoint that places an order, because no such code exists anywhere in
 * the project — but this route does not rely on that. It forwards nothing it has not been told to
 * forward, so a future endpoint cannot become publicly reachable by accident.
 */
import { NextRequest, NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

/**
 * How long to wait for the engine.
 *
 * Raised from 8 s after the first deployment reported the dashboard flipping between connected and
 * not connected while the engine was demonstrably healthy: a cold serverless function plus a round
 * trip to a small VM in another region can exceed 8 s without anything being wrong.
 */
const PROXY_TIMEOUT_MS = 20_000;

/**
 * Exactly the paths the dashboard reads. An allow-list rather than a pattern: a pattern like
 * `api/**` would quietly expose whatever gets added to the engine later, and the person adding it
 * would have no reason to think about this file.
 */
const ALLOWED = new Set([
  'api/signals/live',
  'api/signals/history',
  'api/health',
  'api/regime',
  'api/warmup',
  'api/events',
]);

/** Paths whose last segment is a variable id, matched by shape. */
const ALLOWED_PREFIXES: RegExp[] = [
  /^api\/signals\/[A-Za-z0-9._-]+$/,
  /^api\/signals\/[A-Za-z0-9._-]+\/audit$/,
  /^api\/assets\/[A-Za-z0-9._-]+$/,
  /^api\/assets\/[A-Za-z0-9._-]+\/rejections$/,
  /^api\/candles\/[A-Za-z0-9._-]+$/,
];

function permitted(path: string): boolean {
  return ALLOWED.has(path) || ALLOWED_PREFIXES.some((re) => re.test(path));
}

/** A failure shaped like the engine's own errors, so the UI has one error shape to render. */
function problem(code: string, message: string, likely: string, fix: string, status: number) {
  return NextResponse.json(
    { error: { code, problem_class: 'configuration', message, likely_cause: likely, remediation: fix } },
    { status, headers: { 'Cache-Control': 'no-store' } },
  );
}

export async function GET(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const target = (path ?? []).join('/');

  if (!permitted(target)) {
    return problem(
      'PROXY_PATH_NOT_ALLOWED',
      `this proxy does not forward ${target}`,
      'the dashboard asked for a path that is not on the allow-list in app/api/proxy/[...path]/route.ts',
      'add it to ALLOWED if the dashboard genuinely needs it; the list is explicit so a new engine endpoint cannot become public by accident',
      404,
    );
  }

  const base = process.env.CT_API_BASE;
  const token = process.env.CT_API_TOKEN;

  // Checked separately so the message names which one is missing. "Something is misconfigured" sends
  // someone to read code; "CT_API_TOKEN is not set" sends them to the settings page.
  if (!base) {
    return problem(
      'PROXY_NOT_CONFIGURED',
      'CT_API_BASE is not set, so the dashboard does not know where the engine is',
      'the environment variable is missing on this deployment',
      'set CT_API_BASE in Vercel -> Project -> Settings -> Environment Variables to http://YOUR_VM_IP:8000, then redeploy',
      503,
    );
  }
  if (!token) {
    return problem(
      'PROXY_NOT_CONFIGURED',
      'CT_API_TOKEN is not set, so requests to the engine would be rejected',
      'the environment variable is missing on this deployment',
      'set CT_API_TOKEN in Vercel to the same value as CT_SERVER__AUTH_TOKEN on the VM, then redeploy',
      503,
    );
  }

  const url = new URL(`${base.replace(/\/+$/, '')}/${target}`);
  // Query parameters are forwarded, but the engine's routes take no parameter that reaches the
  // strategy or risk engines (specs/14 §11), so there is nothing here to influence a suggestion.
  req.nextUrl.searchParams.forEach((v, k) => url.searchParams.set(k, v));

  try {
    const upstream = await fetch(url, {
      headers: { 'x-auth-token': token, accept: 'application/json' },
      // Generous, and deliberately so after the first deployment showed the dashboard flipping
      // between connected and not. 8 seconds was not enough to cover a cold serverless function
      // plus a round trip to a small VM in another region, so a healthy engine looked intermittent.
      //
      // Still bounded: a dashboard that hangs forever is worse than one that says the engine is
      // unreachable, because the reader cannot tell a slow page from a dead engine and will assume
      // the numbers on screen are current.
      signal: AbortSignal.timeout(PROXY_TIMEOUT_MS),
      cache: 'no-store',
    });

    const body = await upstream.text();

    // A non-2xx whose body is not one of the engine's own structured errors gets wrapped into one.
    //
    // Passing it through verbatim is what made the first deployment's worst screen possible: with
    // `CT_API_BASE` set to `http://IP:8000/api`, the request became `/api/api/signals/live` and
    // FastAPI answered `404 {"detail": "Not Found"}`. The dashboard found no `error` key and no
    // envelope in that body, concluded nothing was wrong, and reported a calm, empty market for an
    // engine it had never reached (F-050). The fix belongs here as well as in the dashboard, so the
    // mistake is named once, where the URL that caused it is in scope.
    if (!upstream.ok && !body.includes('"error"')) {
      return NextResponse.json(
        {
          error: {
            code: 'PROXY_UPSTREAM_NOT_OURS',
            problem_class: 'configuration',
            message: `the engine answered HTTP ${upstream.status} with a body that is not one of its own responses`,
            likely_cause:
              upstream.status === 404
                ? `nothing is served at /${target} — CT_API_BASE probably includes a path, so the request became a doubled one`
                : 'the reply came from something other than this engine, or from a version of it that does not serve this path',
            remediation:
              'CT_API_BASE must be the host and port only, with no trailing path: http://YOUR_VM_IP:8000. Check it in Vercel -> Project -> Settings -> Environment Variables, redeploy, then run bash /opt/crypto-trader/deploy/azure/diagnose.sh on the VM if it persists.',
            upstream_status: upstream.status,
            // The body, truncated. It is the only evidence of what actually answered, and a
            // dashboard that hides it sends the reader to the logs for one line.
            upstream_body: body.slice(0, 200),
          },
        },
        { status: 502, headers: { 'Cache-Control': 'no-store' } },
      );
    }

    return new NextResponse(body, {
      status: upstream.status,
      headers: {
        'content-type': upstream.headers.get('content-type') ?? 'application/json',
        'Cache-Control': 'no-store',
      },
    });
  } catch (e) {
    const timedOut = e instanceof Error && e.name === 'TimeoutError';
    return NextResponse.json(
      {
        error: {
          code: timedOut ? 'PROXY_UPSTREAM_TIMEOUT' : 'PROXY_UPSTREAM_UNREACHABLE',
          problem_class: 'infrastructure',
          message: timedOut
            ? `the engine did not answer within ${PROXY_TIMEOUT_MS / 1000} seconds`
            : 'the engine could not be reached',
          likely_cause: timedOut
            ? 'the engine is reachable but slow, or the VM is under load'
            : 'the VM is off, the Azure firewall is not allowing port 8000, or CT_API_BASE points somewhere else',
          remediation:
            'on the VM, run: bash /opt/crypto-trader/deploy/azure/diagnose.sh — it names the first broken link. This is an infrastructure problem, not a market one, so nothing on this page should be read as current.',
        },
      },
      { status: 503, headers: { 'Cache-Control': 'no-store' } },
    );
  }
}
