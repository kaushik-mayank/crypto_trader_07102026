# Fixing the dashboard — step by step

**Your VM is on, but the dashboard says "the engine did not answer".** I read your full setup log and
found what happened. Nothing is seriously wrong and nothing has been lost.

**About 15 minutes.** Copy each command exactly. If a step's output doesn't match what I describe,
stop there and tell me what you saw.

---

## What actually happened

One command in my guide was wrong, and that caused everything else.

Near the end you ran this, to fetch the full price history:

```
/opt/crypto-trader/.venv/bin/python -m bootstrap --run --profile full
```

It failed with a permission error. **That was my mistake** — my guide printed that line without the
`sudo -u crypto` prefix that every other command has. The engine runs as a user called `crypto`, and
only `crypto` is allowed to write into the `data` and `logs` folders. You ran it as yourself, so it
was refused.

Then, very reasonably, you fixed the permission error:

```
sudo chown -R azureuser:azureuser /opt/crypto-trader/logs
```

That gave the `logs` folder to **you** — and took it away from `crypto`. So from that moment the
engine could no longer write its log file. And here is my second mistake: **the engine treated "I
can't write a log file" as a fatal error and shut down.** Linux then restarted it, it failed again,
and it has been going round that loop ever since. That loop is why your dashboard says the engine
didn't answer.

So there were three separate problems:

| # | Problem | Whose fault |
|---|---|---|
| 1 | A command in the guide was missing `sudo -u crypto` | Mine — guide now fixed |
| 2 | The engine died because it couldn't write a *log file* | Mine — code now fixed |
| 3 | `chown` handed the `logs` folder to the wrong user | A reasonable fix for problem 1 — you'll undo it below |

**I have already fixed problems 1 and 2 in the code.** You need to do Step 1 to collect those fixes,
and Step 2 to undo problem 3.

There may also be something unfinished in Cloudflare and Vercel — Steps 4 and 5 check that.

---

## Step 1 — Get the fixed code onto the VM

Connect to the VM:

```bash
ssh -i "C:\Users\dksma\Downloads\crypto-vm_key.pem" azureuser@20.193.148.146
```

Then:

```bash
cd /opt/crypto-trader
sudo -u crypto git pull
```

You should see a list of changed files. If it says `Already up to date`, I haven't pushed yet — tell
me and wait.

Then install anything new:

```bash
sudo -u crypto ./.venv/bin/pip install --quiet -e .
```

---

## Step 2 — Give the folders back to `crypto`

This is the fix for the actual breakage. One command:

```bash
sudo chown -R crypto:crypto /opt/crypto-trader/data /opt/crypto-trader/logs
```

Check it worked:

```bash
ls -ld /opt/crypto-trader/data /opt/crypto-trader/logs
```

Both lines must show **`crypto crypto`**. Like this:

```
drwxr-xr-x 2 crypto crypto 4096 Oct  7 18:19 /opt/crypto-trader/data
drwxr-xr-x 2 crypto crypto 4096 Oct  7 18:22 /opt/crypto-trader/logs
```

If either says `azureuser`, run the `chown` command again and look for an error message.

---

## Step 3 — Restart the engine and check it

```bash
sudo systemctl restart crypto-ingest
sudo systemctl restart crypto-app
sudo systemctl restart crypto-tunnel
sleep 15
systemctl is-active crypto-ingest crypto-app crypto-tunnel
```

You want **three lines each saying `active`**.

Now check the engine answers. Two commands:

```bash
# Without a password — this SHOULD be refused. 401 is the correct, healthy answer.
curl -s -o /dev/null -w "no token: %{http_code}\n" http://127.0.0.1:8000/api/health
```

```bash
# With the password — paste your own token between the quotes.
curl -s -o /dev/null -w "with token: %{http_code}\n" \
  -H "x-auth-token: PASTE_YOUR_TOKEN_HERE" http://127.0.0.1:8000/api/health
```

| What you want | What it means |
|---|---|
| `no token: 401` | The engine is up and refusing strangers. Correct |
| `with token: 200` | The engine is working |

Forgotten your token? Show it with:

```bash
sudo grep AUTH_TOKEN /etc/crypto-trader/env
```

**If you got `no token: 000`**, the engine still isn't running. Find out why:

```bash
journalctl -u crypto-app -n 40 --no-pager
```

Send me what that prints.

---

## Step 4 — Check Cloudflare has a route (likely still missing)

Your tunnel is running, but I can't see in your log that you finished the second half of the
Cloudflare setup. A running tunnel with no route accepts the connection and then has nowhere to send
it — which looks exactly like the engine not answering.

1. Go to **[one.dash.cloudflare.com](https://one.dash.cloudflare.com)**
2. **Networks** → **Tunnels** → click your tunnel (`crypto-trader`)
3. Click the **Public Hostname** tab

**If the list is empty, that is the problem.** Click **Add a public hostname** and fill in:

| Field | What to put |
|---|---|
| Subdomain | `crypto-api` |
| Domain | whichever domain Cloudflare offers you |
| Path | leave empty |
| Service → Type | **HTTP** (not HTTPS — the engine is plain HTTP on the machine itself) |
| Service → URL | `localhost:8000` |

Click **Save**. Copy the full address it shows you — something like
`crypto-api.yourdomain.com`. You need it in Step 5.

### Now test it from your own PC

Open **PowerShell on your laptop** — not the VM:

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" https://crypto-api.yourdomain.com/api/health
```

| Result | Meaning |
|---|---|
| **401** | Correct. The tunnel works and the engine is refusing strangers, as it should |
| `502` or `530` | The Public Hostname is missing or its Service URL is wrong — redo this step |
| Hangs, then nothing | Same as above, or the engine is down — go back to Step 3 |

**Do not continue until you see 401.** Everything in Step 5 depends on this working.

---

## Step 5 — Check Vercel, then redeploy

Two things to verify, and one that's easy to miss.

1. Go to **vercel.com** → your project → **Settings** → **Environment Variables**

2. Check `CT_API_BASE`. It must be **exactly** your tunnel address from Step 4:

   | | |
   |---|---|
   | ✅ Right | `https://crypto-api.yourdomain.com` |
   | ❌ Trailing slash | `https://crypto-api.yourdomain.com/` |
   | ❌ Missing `https://` | `crypto-api.yourdomain.com` |
   | ❌ A path on the end | `https://crypto-api.yourdomain.com/api` |

3. Check `CT_API_TOKEN` matches the VM's token **character for character**. Compare against what
   `sudo grep AUTH_TOKEN /etc/crypto-trader/env` printed in Step 3. A single missing character gives
   you a dashboard where every panel fails.

4. **Now redeploy — this is the step people miss.** Vercel does *not* apply a changed environment
   variable to a deployment that already exists. The old value stays live until you build again.

   **Deployments** → the top one → the **⋯** menu → **Redeploy** → **Redeploy**.

Wait two minutes, then open your dashboard.

---

## Step 6 — One command that checks everything

I've added a script that walks the whole chain and tells you which link is broken, instead of the
vague "the engine did not answer". On the VM:

```bash
bash /opt/crypto-trader/deploy/azure/diagnose.sh
```

Note the `bash` at the front — type it exactly like that. (The old `health-check.sh` gave you
`Permission denied` because files lose their "runnable" flag when they travel through Git from
Windows. Putting `bash` in front sidesteps that entirely, and I've fixed the flag as well.)

It prints eight sections with `OK`, `WARN` or `FAIL`. **Fix the first `FAIL`, then run it again.**
When the machine is healthy it tells you so, and reminds you to check the two Vercel things.

Run this any time something looks wrong. It is the first thing to try, always.

---

## Two things you saw that are **not** problems

### "remaining incomplete: 3 series, 3 bars"

Your bootstrap run ended with that message. **Nothing was wrong.** The run took about a minute, and
during that minute one new one-minute candle closed on the exchange. The check at the end compared
against the clock *after* the run, so that brand-new candle counted as missing.

It was my bug — a misleading message and a false failure. Fixed: the check now compares against the
same moment the run was planned for. Newer candles are the live ingest service's job, and it picks
them up within a minute.

### "no token: 401"

A 401 looks like an error and is the opposite. It means a stranger with no password was correctly
turned away. If that ever shows **200**, something is wrong — the engine would be open to anyone.

---

## About your CoinDCX API key — don't add it

You mentioned you have a key and secret and haven't entered them anywhere. **That's the right call.
Leave them out.** Three reasons:

1. **Every piece of data this system reads is public.** We tested this specifically. The endpoint the
   vendor's documentation says needs a password actually returns everything to a plain, unauthenticated
   request. The key would add nothing.

2. **It wouldn't fix the missing data anyway.** The data we can't get historically — funding rates,
   positioning, the order book — isn't withheld behind a login. The exchange simply doesn't offer a
   "give me last year's funding rates" endpoint to anyone. A key can't create history that was never
   published. That data only accumulates from now on, while the engine runs.

3. **An unused key is safer than a used one.** A key stored on a server is a key that can leak. This
   system has no code that could place, cancel or transfer anything — so right now a leak of your
   server costs you nothing at all. Keep it that way.

If you ever do need one for something, make a **fresh** key with **trading and withdrawal permissions
switched off**. Never use one that can trade.

---

## Rules that stop this happening again

**Every command touching `data` or `logs` starts with `sudo -u crypto`.** That's the whole lesson. If
you see a permission error, it almost always means that prefix was missing — add it and run again.

**Never run `chown` to fix a permission error on this project.** It looks like the fix and it's the
thing that breaks the engine. The folders must stay owned by `crypto`.

**After changing anything in Vercel, redeploy.** Settings alone do nothing.

**When something looks wrong, run the diagnose script first.** It's faster than guessing and it tells
you which of the four possible causes you're looking at.

---

## If you're still stuck

Run these three and send me all the output:

```bash
bash /opt/crypto-trader/deploy/azure/diagnose.sh
```

```bash
journalctl -u crypto-app -n 60 --no-pager
```

```bash
ls -ld /opt/crypto-trader/data /opt/crypto-trader/logs
```

And from your laptop, the result of the `curl.exe` command in Step 4. With those four things I can
tell you exactly where the chain is broken.
