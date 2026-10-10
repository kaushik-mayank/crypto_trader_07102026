# Dashboard (Vercel)

The read-only dashboard for the suggestion engine. Deployed on Vercel; the engine runs on an Azure VM
and answers on port 8000 behind a shared token.

Full setup: [`../HOSTING.md`](../HOSTING.md).

## Pages

| Page | File | What it is for |
|---|---|---|
| Suggestions | [`app/page.tsx`](app/page.tsx) | What the strategies currently say |
| Chart | [`app/chart/page.tsx`](app/chart/page.tsx) | **The prices behind it** |

The pairing is the point. Every panel on the suggestions page reports a *conclusion* — no
suggestions, a regime cell, a health status — and every one of them looks identical whether the
prices behind it are current, stale or absent. The chart is the one view where wrong data is obvious
at a glance, which is why it shows the newest bar's age in intervals, how many bars are stored versus
drawn, and how many have not yet been confirmed by a second fetch.

## How it reaches the engine

```
browser → this app's /api/proxy/... route (on Vercel's server) → Azure VM, port 8000
```

The browser never talks to the engine. Every call goes through
[`app/api/proxy/[...path]/route.ts`](app/api/proxy/%5B...path%5D/route.ts), which runs on Vercel's
server and adds the shared token there. Three consequences, all deliberate:

- the token **never reaches the browser**, which is why neither environment variable is named
  `NEXT_PUBLIC_*` — that prefix compiles a value into the JavaScript the browser downloads;
- the engine needs no CORS configuration, because no browser ever calls it;
- only this one route knows the engine's address, so moving the engine is a one-variable change.

`CT_API_BASE` is the **host and port only** — `http://<ip>:8000`, never with `/api` on the end. A
path there makes every request a doubled one, which the engine answers `404 {"detail": "Not Found"}`;
that used to render as a calm, empty market, and both this route and `lib/honesty.ts` now refuse it
explicitly (F-050).

The proxy forwards **GET only**, and only paths on an explicit allow-list. The engine is read-only by
construction — there is no order-placing code anywhere in the project — but the proxy does not rely
on that, so a future endpoint cannot become publicly reachable by accident.

The tunnel that used to sit in the middle is gone (F-044): an unmaintainable security control is not
a security control, and the engine's own bind guard already refuses to start on a public address
without a token. The cost is TLS — see `../HOSTING.md`'s HTTPS appendix for closing that.

## Running it locally

```bash
cp .env.example .env.local      # then fill in CT_API_BASE and CT_API_TOKEN
npm install
npm run dev                     # http://localhost:3000
```

To point at an engine on your own machine rather than the VM, set `CT_API_BASE=http://127.0.0.1:8000`
and run the engine with `python -m app --serve`.

## Tests

```bash
npm test        # node --test, no framework to install
```

These check the display rules in `lib/honesty.ts`. **The same rules are also checked from Python**,
in `../tests/test_frontend_contract.py`, which reads this directory as text and asserts its
thresholds and colour tokens match `src/api/render.py`. That cross-check is the important one,
because it runs in the suite that runs on every change — Node may not be installed everywhere, and a
duplication nobody checks is a divergence with a delay on it.

## Why the rules exist twice at all

There are two dashboards: this one and the server-rendered one the engine serves on the VM. Both
apply the specs/15 honesty rules — never show stale data as fresh, never render an unreachable engine
as an empty market, never leave a missing confidence score blank.

Duplicating a rule is normally refused in this project (there is one ACT, one trailing-stop, one cost
model). It is accepted here because the alternative is passing HTML strings across a network boundary
into `dangerouslySetInnerHTML`, which cannot be type-checked at all. The duplication is confined to
`lib/honesty.ts`, kept small enough to read side by side with `render.py`, and cross-checked by the
Python test above.
