# Fixing the dashboard — step by step

**Round 2.** Your last run fixed the engine: it is healthy, evaluating, and answering. The only thing
left was the Cloudflare tunnel, and **we are removing it.** Vercel will now talk to your VM directly.

**About 15 minutes.** Copy each command exactly. If a step's output doesn't match what I describe,
stop there and tell me what you saw.

---

## First — what your last run proved

Your diagnose output was almost all `OK`, and that matters. These are now settled:

| | |
|---|---|
| File ownership | `data/` and `logs/` both belong to `crypto` ✅ |
| All services | running, **0 restarts** — no crash loop ✅ |
| The engine | 401 without the token, 200 with it ✅ |
| Price history | complete ✅ |
| Disk | 8% used ✅ |
| Errors in the last hour | **zero** ✅ |

The engine was never the problem. Only the route from Vercel to it.

---

## Two more bugs I found while reading your logs

Both were mine, both are fixed, and **one of them would have wasted your entire 72-hour run.**

### 1. The dashboard would have frozen after one evaluation

`python -m app --serve` evaluated **once**, then started the web server — and the web server never
returns. So the engine would have produced one set of suggestions at startup and served that same
frozen moment for three days. The only thing that would have moved on your dashboard is the "data
age" counter climbing.

It would have looked almost plausible, which is the worst kind of bug. **Fixed:** the web server now
runs alongside a loop that keeps evaluating. I measured it — 6 evaluations in 75 seconds at a
12-second interval, where before it was exactly 1.

### 2. The health panel reported your database as broken when it wasn't

A detail of how databases and threads interact: the health check was asking the *writing* connection
a question from the *reading* side, which that kind of database refuses. So the dashboard would have
shown your store as `DOWN` with a confusing message while the engine was writing perfectly well.

**Fixed:** the health panel now checks the connection the dashboard actually reads through. After the
fix it reports honestly — your store healthy, and the two known-degraded regime models flagged as
degraded, which is true and expected.

---

## Step 1 — Get the fixed code

```bash
ssh -i "C:\Users\dksma\Downloads\crypto-vm_key.pem" azureuser@20.193.148.146
```

```bash
cd /opt/crypto-trader
sudo -u crypto git pull
sudo -u crypto ./.venv/bin/pip install --quiet -e .
```

---

## Step 2 — Switch the engine to listen on the network

Right now the engine only listens to itself, which is why it needed a tunnel. One line changes that.

```bash
sudo nano /etc/crypto-trader/env
```

Find this line:

```
CT_SERVER__HOST=127.0.0.1
```

Change it to:

```
CT_SERVER__HOST=0.0.0.0
```

`0.0.0.0` means "listen on the network card" instead of "only to myself". While you are in the file,
also change this line to `false` (it was for the tunnel, which is going away):

```
CT_SERVER__PUBLIC_VIA_TUNNEL=false
```

Save and exit: `Ctrl+O`, `Enter`, `Ctrl+X`.

> **The engine refuses to start on a network address without a token.** That is deliberate. You
> already have a token set, so this will be fine — but it means there is no way to accidentally end
> up with an open, password-free port.

---

## Step 3 — Stop and remove the tunnel

```bash
sudo systemctl disable --now crypto-tunnel
sudo rm -f /etc/systemd/system/crypto-tunnel.service /etc/crypto-trader/tunnel-env
sudo systemctl daemon-reload
```

Then restart the engine so it picks up the new setting:

```bash
sudo systemctl restart crypto-ingest
sudo systemctl restart crypto-app
sleep 15
systemctl is-active crypto-ingest crypto-app
```

You want **two lines saying `active`**.

Now confirm it is listening on the network rather than just to itself:

```bash
ss -ltn | grep 8000
```

You want to see **`0.0.0.0:8000`**. If it says `127.0.0.1:8000`, the setting didn't take — check
Step 2 and restart `crypto-app` again.

You can also delete the tunnel in the Cloudflare dashboard if you like. Nothing depends on it now.

---

## Step 4 — Open the port in the Azure firewall

1. [portal.azure.com](https://portal.azure.com) → your VM (`crypto-vm`) → **Networking** →
   **Network settings**
2. **Add inbound port rule**, and fill in exactly this:

   | Field | Value |
   |---|---|
   | Source | **Any** |
   | Source port ranges | `*` |
   | Destination | **Any** |
   | Service | **Custom** |
   | Destination port ranges | `8000` |
   | Protocol | **TCP** |
   | Action | **Allow** |
   | Priority | `110` |
   | Name | `allow-api` |

3. **Add**

### Why "Any" is the source, and what that actually risks

Vercel doesn't publish fixed addresses for its servers on the free plan, so the rule can't be
narrowed to them. **The token is what protects the port.** Being precise about what that means:

**What someone with a stolen token could do:** read public market prices, and read your suggestions.

**What they could not do:**

- **trade anything.** No code in this project can place, cancel or transfer — there is none, and a
  test fails the build if an order or wallet address ever appears in it;
- **steal a credential.** Your CoinDCX fields are empty, because everything this system reads is
  public;
- **burn your exchange rate limit.** The web API only reads your local database. It makes no calls
  out to CoinDCX at all — that's the separate ingest service, on its own schedule.

**The honest downside:** the traffic is plain HTTP, so the token crosses the internet unencrypted.
Someone sitting on the network path could read it. For what's behind it, that is a fair trade — and
the engine now logs a summary whenever requests arrive without a valid token, so you'd see it being
probed. If you want it encrypted, there's an HTTPS upgrade at the end of `HOSTING.md`; it needs a
free hostname and takes about 10 minutes.

---

## Step 5 — Test it from your own PC

**PowerShell on your laptop** — not the VM:

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" http://20.193.148.146:8000/api/health
```

| Result | Meaning |
|---|---|
| **401** | Correct. Reachable, and refusing anyone without the token |
| Hangs, then nothing | The firewall rule is missing or wrong — redo Step 4 |
| **200** | **Stop and tell me.** That would mean the port is open with no password |

**Do not go to Step 6 until you see 401.** Everything after this depends on it.

---

## Step 6 — Point Vercel at the VM and redeploy

1. **vercel.com** → your project → **Settings** → **Environment Variables**

2. Change **`CT_API_BASE`** to exactly this:

   ```
   http://20.193.148.146:8000
   ```

   | | |
   |---|---|
   | ✅ Right | `http://20.193.148.146:8000` |
   | ❌ `https` | `https://20.193.148.146:8000` — there is no certificate, this will fail |
   | ❌ Trailing slash | `http://20.193.148.146:8000/` |
   | ❌ A path on the end | `http://20.193.148.146:8000/api` |

   **`http`, not `https`** — and that is not a security mistake on your part. Your browser still
   loads the dashboard over HTTPS; only Vercel's *server* speaks plain HTTP to your VM, and browsers
   don't care what a server does behind the scenes.

3. Leave **`CT_API_TOKEN`** alone — it hasn't changed.

4. **Redeploy. This is the step people miss.** Vercel does not apply a changed variable to a
   deployment that already exists; the old value stays live until you build again.

   **Deployments** → the top one → the **⋯** menu → **Redeploy** → **Redeploy**.

Wait two minutes, then open your dashboard.

---

## Step 7 — Confirm, then leave it alone

On the VM:

```bash
bash /opt/crypto-trader/deploy/azure/diagnose.sh
```

Section 5 is now about direct access rather than the tunnel, and tells you whether the port is
genuinely listening on the network.

Then **stop touching it for 72 hours.** That is the whole point of this exercise: finding out what
breaks when nobody is watching, which is the only way to find that out.

### Once a day, three commands

```bash
# Still running, and not restarting?
systemctl is-active crypto-ingest crypto-app
systemctl show -p NRestarts --value crypto-app
```

```bash
# Anything gone wrong?
journalctl -u crypto-app -u crypto-ingest --since '1 day ago' | grep '"level": *"error"' | tail -20
```

```bash
# Price data still current? Should say 0 bars missing.
cd /opt/crypto-trader && sudo -u crypto ./.venv/bin/python -m bootstrap --check | tail -3
```

### What counts as a problem, and what doesn't

| What you see | Is it a problem? |
|---|---|
| `No suggestions right now` | **No.** A quiet market is the normal state. Most hours have nothing to say |
| The data age sitting at a few seconds and resetting | **No** — that is the fix from this round working |
| A few `DQ_STALE` errors | **No.** A source was briefly late and the engine declined to guess |
| A few `api_requests_refused` summaries | **No.** Background scanning of a public port. Normal |
| A *rising* stream of `api_requests_refused` from one address | **Yes.** Someone is guessing tokens — rotate it (below) |
| `NRestarts` climbing | **Yes.** `journalctl -u crypto-app -n 40 --no-pager` |
| Data age climbing past 10 minutes and never resetting | **Yes.** The evaluation loop has stopped |
| Disk above 85% | **Yes.** The database grows ~2 GB/month |

### Rotating the token, if you ever need to

```bash
/opt/crypto-trader/.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"
sudo nano /etc/crypto-trader/env     # paste it over CT_SERVER__AUTH_TOKEN
sudo systemctl restart crypto-app
```

Then update `CT_API_TOKEN` in Vercel to match and **redeploy**.

---

## Your CoinDCX API key — still don't add it

Nothing has changed here. Leave it out:

1. **Every piece of data this system reads is public.** We tested the one endpoint the vendor docs
   claim needs a password — it returns everything to a plain, unauthenticated request.

2. **It wouldn't give us the missing data anyway.** What we lack historically — funding rates,
   positioning, the order book — isn't withheld behind a login. The exchange publishes no "give me
   last year's funding rates" endpoint to *anyone*. A key cannot create history that was never
   published. That data only accumulates from now on, while your engine runs.

3. **An unused key can't leak.** Right now a complete compromise of your VM costs you nothing,
   because there is nothing there to take. That is worth keeping.

If you ever do need one, make a **fresh** key with trading and withdrawal **switched off**.

---

## Two small housekeeping notes from your log

**Your SSH stopped working for a while** because your home internet address changed
(`106.192.48.69` → `106.222.206.143`) and the firewall rule still allowed the old one. You fixed it
correctly. It will happen again whenever your router gets a new address — same fix: VM →
**Networking** → the `SSH` rule → **Source** → **My IP address** → Save.

**The VM says `*** System restart required ***`** from security updates. Worth doing before the
72-hour run starts, so it doesn't interrupt it:

```bash
sudo apt-get update && sudo apt-get upgrade -y
sudo reboot
```

Wait two minutes, reconnect, and confirm both services came back on their own:

```bash
systemctl is-active crypto-ingest crypto-app
```

Both should say `active` without you starting anything — that is what `enable` was for, and it's
worth proving once.

---

## The rules that keep this working

**Every command touching `data` or `logs` starts with `sudo -u crypto`.** A permission error almost
always means that prefix was missing.

**Never run `chown` to fix a permission error here.** It looks like the fix and it's what broke the
engine last time.

**After changing anything in Vercel, redeploy.** Settings alone do nothing.

**When something looks wrong, run `diagnose.sh` first.** It names the broken link instead of making
you guess.

---

## If you're still stuck

Run these and send me everything:

```bash
bash /opt/crypto-trader/deploy/azure/diagnose.sh
```

```bash
journalctl -u crypto-app -n 60 --no-pager
```

```bash
ss -ltn | grep 8000
```

And from your laptop, the result of the `curl.exe` command in Step 5. With those four I can tell you
exactly where the chain is broken.
