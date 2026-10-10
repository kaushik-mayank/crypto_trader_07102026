# Fixing the dashboard — step by step

**Round 5. This one is different: nothing is wrong with your VM, and nothing is wrong with your
settings.** Your `diagnose.sh` output was clean on all eight sections and it was telling the truth.

**The problem is that the new code never reached GitHub.** I wrote it, you deployed, and most of it
was still sitting on my side. Twice I told you to run `update.sh` without checking whether the code
it pulls contains the fix. That was my mistake, and this guide now has the step that was missing.

**About 10 minutes.**

---

## What was actually wrong — three things, all mine

### 1. The engine code was never committed

Your two commits (`chart 1`, `data1`) contained **only frontend files**. Every backend change from
the last two sessions — the `/api/candles` route the Chart page calls, `/api/sources` for the Data
page, the price rounding, the spread/price fix — was never in a commit.

So `update.sh` was doing exactly what you asked: fetching a branch whose newest commit did not have
those routes, saying "already up to date", and restarting the same code. The chart page would have
kept 404ing however many times you ran it.

### 2. The Data page could not be committed at all

The project's `.gitignore` had a line `data/` to protect the validation data. That pattern matches a
folder called `data` **anywhere** — including `frontend/app/data/`, the new page.

So `git add` skipped it, the commit succeeded, the push succeeded, Vercel built successfully, and
**nothing reported a problem** — an ignored file is not an error. The only symptom was the 404 you
saw. Fixed: the rule is now `/data/`, which still protects the validation data and no longer eats
the page. There is a test that will fail if this ever happens again.

### 3. BTC longs still negative

Same root cause as #1. The fix needs the engine to say "these numbers are a spread, not a price",
and the engine was never updated. I have also added a fallback so those cards render correctly even
against an older engine — because the two halves are deployed separately and will always drift
apart for a while.

---

# Part A — Push the code properly (on your laptop, not the VM)

This is the part that was missing. Do it from the project folder on your PC.

### A1. See what is not yet committed

```bash
git status
```

You will see a list of modified files under `src/`, `tests/`, `frontend/` and some `.md` files, plus
an untracked file `src/snapshot/request.py`.

**Read that list.** Everything in it is code that exists only on your PC.

### A2. Stage everything, then check what you staged

```bash
git add -A
git status
```

Now everything should be listed under **"Changes to be committed"** and the
**"Changes not staged"** and **"Untracked files"** sections should be gone or empty.

> **The one check that matters.** Confirm these four files are in the staged list:
>
> ```
> src/api/app.py
> src/api/services.py
> src/snapshot/request.py
> frontend/app/data/page.tsx
> ```
>
> If `frontend/app/data/page.tsx` is missing, the `.gitignore` fix did not come through — tell me
> before going further.

### A3. Commit and push

```bash
git commit -m "chart and data pages, price rounding, spread display, deploy checks"
git push
```

### A4. Prove it arrived

```bash
git status
```

Expected: **"nothing to commit, working tree clean"** and **"Your branch is up to date with
'origin/main'"**.

If it says "ahead of origin/main", the push did not go through. Run `git push` again and read any
error it prints.

---

# Part B — Update the VM

Only after Part A says "working tree clean".

### B1. Update

```bash
sudo bash /opt/crypto-trader/deploy/azure/update.sh
```

Watch the end of the output. It now checks the routes itself:

```
==> routes the dashboard needs
    /api/signals/live            200
    /api/health                  200
    /api/candles/B-BTC_USDT      200
    /api/sources                 200
```

**All four must be 200.** A `404` there means that route is still not in the code — go back to Part
A. The script will say so explicitly.

If it prints **"already up to date"** *and* a route still 404s, the code did not reach GitHub. That
is Part A again, not a VM problem. The script now says this too.

### B2. Confirm the whole chain

```bash
bash /opt/crypto-trader/deploy/azure/diagnose.sh
```

There are two new sections:

| Section | What it tells you |
|---|---|
| **4b. Does this engine serve every route the dashboard needs?** | lists each route and its status. Any `404` means the VM is older than the dashboard |
| **4c. Deployed version** | the exact commit the VM is running, and whether it is behind the remote |

**Section 4b is the one that was missing**, and it is why this took two rounds. All eight of your
original sections asked "is the engine working". None asked "is the engine the version the dashboard
expects".

---

# Part C — Redeploy Vercel

**Deployments** → the top one → the **⋯** menu → **Redeploy** → **Redeploy**.

Change nothing in Settings. Your `CT_API_BASE` and `CT_API_TOKEN` are correct and always were.

Wait two minutes, then open the dashboard. You should have three pages in the left menu:
**Suggestions**, **Chart**, **Data**.

---

# Part D — How to restart the whole project safely

Keep this. It is the answer to "something is stuck, how do I start clean".

### Normal restart (keeps all data)

```bash
sudo systemctl restart crypto-ingest
sudo systemctl restart crypto-app
sleep 5
systemctl is-active crypto-ingest crypto-app
```

Expected: `active` twice. Nothing is lost — the database is on disk and both services pick up where
they left off. **Safe to do any time.**

### Stop everything

```bash
sudo systemctl stop crypto-app crypto-ingest
```

While stopped, **Tier B data is being lost permanently** (funding, positioning, order book have no
archive — see the Data page). Price candles will be refilled automatically on restart. So stop it
for minutes, not days.

### Restart after a code change

Always `update.sh`, never a bare `systemctl restart` — the restart alone reloads the *same* code:

```bash
sudo bash /opt/crypto-trader/deploy/azure/update.sh
```

### Start clean from scratch (deletes all collected data)

Only if the database is genuinely broken. **Read the warning first.**

```bash
sudo systemctl stop crypto-app crypto-ingest
sudo -u crypto rm -f /opt/crypto-trader/data/state.db*
sudo systemctl start crypto-ingest
# watch it rebuild — it fetches ~17,500 bars in about 20 seconds
journalctl -u crypto-ingest -f
# when you see "history_filled" or "history_complete", press Ctrl-C and start the app
sudo systemctl start crypto-app
```

> **What this costs.** Price candles come back automatically. **All Tier B history is gone for
> good** — nobody can refetch yesterday's order book, with any API key, at any price. Every day the
> system has been running is a day of Tier B history, and this throws it away.

### If a service will not stay up

```bash
systemctl status crypto-app --no-pager | head -20
journalctl -u crypto-app -n 60 --no-pager
```

Look at `NRestarts` in the status output. A climbing number is a crash loop — a crash loop still
reports `active`, which is why that number matters.

---

# Why "the frontend is updated but gets no data from the VM"

Worth understanding, because it will happen again otherwise. **They are two separate deployments
that cannot see each other's version.**

```
your PC  ──git push──▶  GitHub  ──┬──▶ Vercel pulls automatically  (the dashboard)
                                  └──▶ you run update.sh by hand   (the engine)
```

Vercel redeploys itself on every push. **The VM does not.** So after a push you are always in a
state where the dashboard is new and the engine is old, until you run `update.sh`. During that
window, new pages 404 and new fields are missing — which is exactly what you were looking at.

And if the push never happened, **both** sides are old, while `git status` on your PC is the only
place that says so. Hence Part A.

**The rule: `git push`, then `update.sh`, then redeploy Vercel. In that order, every time.**

---

# Reading the Data page

Once everything is up, `/data` lists every source with its row count, when it last delivered, and
the last value.

| What to check | Healthy |
|---|---|
| "series behind" at the top | **0** |
| each price series | `0 behind` and a sensible last close |
| each point-in-time source, **"last hour"** | **greater than zero** |
| data quality | "No data-quality events in 24 hours", or a few `ROWS_REJECTED` |

**"last hour" is the useful column.** It is measured, not configured — so a source that should poll
every 15 seconds but delivers nothing shows a red **0** there even though every service reports
`active`. Nothing else on the dashboard can show you that.

Two labels:

- **Tier A — rebuildable.** Price candles, instrument metadata. Refetched automatically if lost.
- **Tier B — no archive, gaps are permanent.** Funding, positioning, order book. These accumulate
  only while the ingest service runs. This is the real reason to leave the VM on.

One row says **"not collected by this system"** — tick-by-tick trades. That is correct, not a fault.

---

# The rules that keep this working

| | |
|---|---|
| **`git push` → `update.sh` → redeploy Vercel** | In that order. Skipping the middle step is what caused this round |
| **`git status` must say "working tree clean"** before you deploy | It is the only place that knows whether the code left your PC |
| **Never commit `.env`** | Credentials live in `.env` on the VM and in Vercel's settings. Nowhere else |
| **Never paste a key into a chat** | Including to me. Your CoinDCX key is still not needed — everything read here is public |
| **`data/` is never pushed** | It holds the validation holdout. Note this is now `/data/` — the repo root only |
| **The system never trades** | It has no order code, and a test greps the source to keep it that way |
| **Never edit files directly on the VM** | The next `update.sh` refuses to overwrite them, and the update stops |

---

# If something is still wrong

Send me:

1. the output of `git status` **on your PC** (this is the one I did not ask for last time, and it
   would have found the problem in a minute);
2. sections **4b** and **4c** of `diagnose.sh`;
3. a screenshot of the **Data** page.
