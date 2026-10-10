# Azure network security group — what to allow

Two inbound rules, and nothing else.

| Priority | Name | Port | Source | Action |
|---|---|---|---|---|
| 100 | `allow-ssh-from-me` | 22 | **your public IP /32** | Allow |
| 110 | `allow-api` | 8000 | `Any` | Allow |
| 65500 | `DenyAllInBound` (default) | any | any | Deny |

## Why port 8000 is open, and what that does and does not risk

Vercel's servers have to reach the engine, and Vercel does not publish fixed egress addresses on the
free tier, so the rule cannot be narrowed to them. The port is therefore open to the internet and
**the token is what protects it**. The app refuses to start on a non-loopback address without one,
so there is no configuration in which the port is open and unauthenticated.

What a stolen token would actually get someone, stated precisely:

- **read access to public market data** — prices CoinDCX publishes to anyone;
- **read access to the suggestions** this system produces;
- a little CPU on your VM.

What it would **not** get them:

- **any ability to trade.** No code in this project places, cancels or transfers anything, and a test
  fails the build if an order or wallet endpoint ever appears in it;
- **any credential.** The CoinDCX fields in the environment file are empty, because everything this
  system reads is public (F-019);
- **the ability to spend your exchange rate budget.** The API reads only the local database and makes
  no outbound calls at all. Polling CoinDCX is the ingest process's job, on its own schedule,
  independent of any request.

The traffic is **plain HTTP, so the token crosses the internet unencrypted.** That is the real cost of
dropping the tunnel, and it is the reason to rotate the token if the log shows it being probed. If
that trade is not acceptable, see the HTTPS upgrade at the end of `HOSTING.md` — it needs a hostname.

## What must stay closed

- **No 80, no 443** unless you set up the HTTPS upgrade. Nothing else on this VM serves anything.
- **SSH source must be your own IP, never `Any`.** Azure's portal offers `Any` as a default, and it
  means every SSH scanner on the internet reaches your login prompt. If your home address changes,
  SSH stops working and you edit this rule — a 20-second job, and the right trade.

## Checking it afterwards

From your laptop, with the VM running:

```bash
# Should print 401 — reachable, and refusing anyone without the token. That is correct.
curl -s -o /dev/null -w "%{http_code}\n" http://YOUR_VM_IP:8000/api/health

# Should connect.
ssh crypto@YOUR_VM_IP
```

A **200** on the first command is the thing to catch on this page: it would mean the port is open
*and* unauthenticated. The app should make that impossible, but check anyway.
