# Telegram notifications — setup and hosting guide

This guide sets up one thing: **Telegram messages when a BTCUSDT C3-C suggestion appears or changes
state.** Nothing else sends a message. No other coin, no other strategy.

**About 20 minutes.** Copy each command exactly. If a step's output doesn't match what's described,
stop there and say what you saw.

---

## What this does, and what it cannot do

A third service is added alongside the two you already run. It **reads** the database the engine
writes, notices when a BTCUSDT C3-C suggestion changes, and sends you a message.

| | |
|---|---|
| It reads the database | **read-only** — it is physically unable to change a suggestion |
| It runs as its own service | if it breaks, stops, or you never install it, the engine and the dashboard are unaffected |
| It changes no suggestion | it forwards what already exists; it generates nothing |
| It sends only BTCUSDT C3-C | the filter is in the database query itself, not a checkbox |

You can skip this entire guide and the rest of the system works exactly as it does today.

---

## Step 1 — Create the bot

On your phone or desktop Telegram:

1. Search for **@BotFather** and open the chat. It has a blue tick.
2. Send `/newbot`.
3. It asks for a **name**. This is the display name; anything is fine, e.g. `My Crypto Trader`.
4. It asks for a **username**. This must end in `bot` and be unused, e.g. `mycryptotrader_7421_bot`.
5. BotFather replies with a line like:

   ```
   Use this token to access the HTTP API:
   8012345678:AAFhQ9x_exampleexampleexampleexample
   ```

**Copy that whole token**, including the digits before the colon. That is one value, not two.

> **Treat it like a password.** Anyone with it can send messages as your bot. Don't paste it into a
> chat, a screenshot, or a file you push to GitHub. If it leaks, send `/revoke` to BotFather and
> start this step again.

---

## Step 2 — Start a chat with your own bot

A Telegram bot **cannot message you first**. You have to speak to it once.

1. In Telegram, search for the username you just chose (e.g. `@mycryptotrader_7421_bot`).
2. Open it and press **Start**, or send `hello`.

It won't reply. That's expected — nothing is running yet. The point is that the conversation now
exists.

---

## Step 3 — Find your chat ID

The chat ID says *where* to send. Open this address in a browser, replacing `<TOKEN>` with the token
from Step 1:

```
https://api.telegram.org/bot<TOKEN>/getUpdates
```

Note there is **no space and no slash** between `bot` and your token.

You'll get a page of text. Look for `"chat":{"id":` followed by a number:

```json
"chat":{"id":123456789,"first_name":"...","type":"private"}
```

**That number is your chat ID.** It may be negative (e.g. `-1001234567890`) if you used a group —
keep the minus sign.

### If the page shows `{"ok":true,"result":[]}`

The empty `result` means Telegram has no messages to show you. Either:

- you skipped Step 2 — go back and send the bot a message; or
- too much time passed. Send the bot another message and reload the page.

---

## Step 4 — Put the two values on the VM

Connect to your VM, then open the environment file:

```bash
sudo nano /etc/crypto-trader/env
```

Scroll to the bottom and add these two lines, pasting your own values after the `=`:

```
CT_TELEGRAM__BOT_TOKEN=8012345678:AAFhQ9x_exampleexampleexampleexample
CT_TELEGRAM__CHAT_ID=123456789
```

No quotes, no spaces around the `=`.

Save and exit: **Ctrl+O**, **Enter**, **Ctrl+X**.

### Why this file and not somewhere else

It's the same file your API token already lives in, and it is already protected: owned by `root`,
readable only by the `crypto` service user, mode `0640`. Confirm that is still true:

```bash
ls -l /etc/crypto-trader/env
```

Expected: `-rw-r----- 1 root crypto`. If it differs, fix it:

```bash
sudo chown root:crypto /etc/crypto-trader/env
sudo chmod 640 /etc/crypto-trader/env
```

**This file is never committed to Git.** `.gitignore` excludes `.env`, and this file lives outside
the project folder entirely.

---

## Step 5 — Get the code onto the VM

From your **laptop**, in the project folder:

```bash
git add -A
git commit -m "telegram notifications for BTCUSDT C3-C"
git push
git status
```

The last command must say **"working tree clean"** and **"up to date with 'origin/main'"**. If it
says "ahead of origin/main", the push didn't go through — run `git push` again and read the error.

Then on the **VM**:

```bash
sudo bash /opt/crypto-trader/deploy/azure/update.sh
```

Near the end you'll see `crypto-notify not installed; skipped`. That's correct — you install it in
the next step.

---

## Step 6 — Test it before installing the service

This is the step that saves time. Run the checks by hand first.

```bash
cd /opt/crypto-trader
sudo -u crypto ./.venv/bin/python -m notify --check
```

Expected output:

```
Telegram notifier preflight
  watching:      B-BTC_USDT  C3-C  (and nothing else)
  bot token:     (len 46, ends ...mple)
  chat id:       123456789
  state file:    data/notify/telegram_state.json
  poll interval: 30s
  values from:   /etc/crypto-trader/env  (9 setting(s) in it)
  OK   database readable: 3 C3-C B-BTC_USDT suggestion(s) on record
  OK   Telegram accepted the bot token
  NOTE not primed yet. ...

Everything checks out.
```

Three lines to read carefully:

- **`watching: B-BTC_USDT C3-C (and nothing else)`** — the safety filter. If it says anything else,
  stop and check Step 4.
- **`values from: /etc/crypto-trader/env`** — it found your settings, and where.
- **`OK Telegram accepted the bot token`** — your token works. If this says FAIL, go to
  Troubleshooting.

`--check` sends **no message to anyone**, so run it as often as you like.

> ### If you are reading an older copy of this guide
>
> An earlier version of this step did not work, and the failure looked exactly like a mistake on
> your side:
>
> ```
> TELEGRAM NOTIFIER NOT CONFIGURED: CT_TELEGRAM__BOT_TOKEN is not set
> ```
>
> **even with the variables correctly entered in `/etc/crypto-trader/env`.**
>
> The reason: that file is a *systemd* environment file. systemd loads it when it **starts a
> service**. Running a command yourself in the shell does not load it — your shell has never heard
> of that file. So the check genuinely could not see your settings, no matter how right they were.
>
> That is fixed. The notifier now reads `/etc/crypto-trader/env` itself when a value is not already
> in the environment, so this command and the running service see exactly the same settings. If you
> are still getting that message, you are on the old code — run Step 5 again (`git push` on your
> laptop, then `update.sh` on the VM) and check that the `values from:` line appears.

### Send yourself one test message

```bash
sudo -u crypto ./.venv/bin/python -m notify --test-message
```

Your phone should buzz with a short message saying notifications are connected. It says plainly that
it is a test and not a suggestion.

If it doesn't arrive, go to Troubleshooting before continuing.

### See what a real notification will look like

```bash
sudo -u crypto ./.venv/bin/python -m notify --dry-run
```

This prints the messages it *would* send and **sends nothing**. On a first run it will say
`primed`, which means it recorded the suggestions that already exist so that starting the service
doesn't replay your history at you.

---

## Step 7 — Install it as a service

```bash
sudo cp /opt/crypto-trader/deploy/azure/crypto-notify.service /etc/systemd/system/
sudo -u crypto mkdir -p /opt/crypto-trader/data/notify
sudo systemctl daemon-reload
sudo systemctl enable --now crypto-notify
```

Check it came up:

```bash
systemctl is-active crypto-notify
```

Expected: `active`.

Watch it for a few seconds:

```bash
sudo journalctl -u crypto-notify -n 20 --no-pager
```

You're looking for `notify_running`, and on the first start `notify_primed`.

---

## Step 8 — Confirm only BTCUSDT C3-C notifies

Three ways, cheapest first.

**1. Ask the notifier what it is watching.**

```bash
cd /opt/crypto-trader && sudo -u crypto ./.venv/bin/python -m notify --check
```

The `watching:` line is the whole answer. The filter is applied inside the database query — the
notifier never even receives rows for other coins or strategies.

**2. Count what it can see against what exists.**

```bash
cd /opt/crypto-trader
sudo -u crypto ./.venv/bin/sqlite3 data/state.db \
  "SELECT strategy_id, asset, COUNT(*) FROM signals GROUP BY strategy_id, asset;"
```

> If `sqlite3` isn't installed, skip this one — it's informational. The `--check` output above
> already reports how many qualifying suggestions exist.

Compare the `C3-C` + `B-BTC_USDT` row against the count `--check` printed. They will match, and
every other row in that list is invisible to the notifier.

**3. Watch the log when something changes.**

```bash
sudo journalctl -u crypto-notify -f
```

Leave it running. When a BTCUSDT C3-C suggestion changes state you'll see `notify_sent` with the
signal id, the new state and the old one. You will **not** see a line for any other coin or
strategy, because those rows never reach the code that logs.

Press **Ctrl+C** to stop watching.

---

## What the messages look like

```
BTCUSDT · C3-C · SHORT
state: TRIGGERED  (was SETUP_DETECTED)

entry: 112730
stop:  113910
target: 111500

suggested: 2026-10-12 11:51 IST
state as of: 2026-10-12 12:30 IST

rejection of an upside break: closed beyond then back inside within 2 bars

Suggestion only — this system never places, cancels or transfers anything,
and nothing in it has been validated against history.
```

Times are **IST**, the same as the dashboard.

Some messages will say **"direction not yet decided"** with no entry or stop. That is correct, not a
bug: C3-C watches a price level and trades whichever way it breaks, so it has no direction until the
market gives it one. A second message arrives when it resolves.

Some will say **"trailing stop: ... (no fixed target by design)"**. Also correct — some strategies
deliberately have no fixed target and trail the position instead.

### How often to expect one

Rarely. C3-C is one strategy on one coin, and these strategies are built to fire seldom. **Days of
silence are the normal state**, not a fault. Use `--check` if you want reassurance that it's alive.

---

## Day-to-day operation

| What you want | Command |
|---|---|
| Is it running? | `systemctl is-active crypto-notify` |
| What has it done? | `sudo journalctl -u crypto-notify -n 50 --no-pager` |
| Is everything wired up? | `cd /opt/crypto-trader && sudo -u crypto ./.venv/bin/python -m notify --check` |
| Restart it | `sudo systemctl restart crypto-notify` |
| Stop notifications for a while | `sudo systemctl stop crypto-notify` |
| Turn them back on | `sudo systemctl start crypto-notify` |
| Turn them off permanently | `sudo systemctl disable --now crypto-notify` |

Stopping it never affects suggestions, the dashboard, or data collection. While it is stopped,
changes are not notified — and when you start it again it **primes**, so it won't send a backlog of
what it missed.

After any future `update.sh`, the notifier is restarted automatically along with the other two.

---

## Troubleshooting

### "TELEGRAM NOTIFIER NOT CONFIGURED: CT_TELEGRAM__BOT_TOKEN is not set"

The message now tells you which of four situations you are in — read the `likely cause` line:

| What the cause says | What it means | Fix |
|---|---|---|
| `...has no line for it (the file was read and holds N other setting(s))` | The file is fine and that one line is genuinely absent | Add it — Step 4 |
| `...has a line for it but nothing after the '='` | The line is there and empty | Put the value right after the `=`, no spaces, no quotes |
| `...does not exist` | Wrong path, or the file was never created | Check `ls -l /etc/crypto-trader/env` |
| `...cannot read it... run with 'sudo -u crypto'` | A permissions problem, not a missing value | Use `sudo -u crypto ...` as every command in this guide does |

Start by confirming the two lines are present:

```bash
sudo grep CT_TELEGRAM /etc/crypto-trader/env
```

You should see both, each with a value after the `=`. If you just edited the file and the **service**
still says this, restart it — a service only reads that file when it starts:

```bash
sudo systemctl restart crypto-notify
```

> **If you are on older code**, this message appears even when the file is perfectly correct,
> because a command run by hand did not read the systemd environment file at all. See the note in
> Step 6. Running Step 5 again fixes it.

### "Telegram rejected the bot token"

The token is wrong or was revoked. It is the **whole** string including the digits before the colon.
Re-copy it from BotFather (send it `/mybots`, pick your bot, **API Token**), update Step 4, restart.

### "Telegram says that chat does not exist"

Usually Step 2 was skipped. Open the bot in Telegram and send it a message, then:

```bash
cd /opt/crypto-trader && sudo -u crypto ./.venv/bin/python -m notify --test-message
```

If it still fails, your chat ID is probably wrong — redo Step 3. For a **group**, the ID starts with
a minus sign and the minus is part of it.

### "the bot is blocked or was never started by this chat"

Open the chat and send `/start`. If you blocked the bot, unblock it.

### `--check` says everything is fine but no messages ever arrive

Most likely nothing has qualified yet. Confirm with:

```bash
cd /opt/crypto-trader && sudo -u crypto ./.venv/bin/python -m notify --check
```

If it reports `0 C3-C B-BTC_USDT suggestion(s) on record`, there is nothing to notify about — C3-C
has not produced a BTCUSDT suggestion yet. That is a market outcome, not a fault. Check the
dashboard's **History** view with the model filter set to **Transition** to see the same thing.

### The service keeps restarting

```bash
systemctl status crypto-notify --no-pager | head -20
sudo journalctl -u crypto-notify -n 60 --no-pager
```

Look at `NRestarts`. A climbing number is a crash loop — the log's last lines say why. The most
common cause is a missing or malformed value in `/etc/crypto-trader/env`.

### "could not reach Telegram"

The VM has no network path to `api.telegram.org`. Test it:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://api.telegram.org
```

Expected: `200` or `302`. If it hangs, outbound HTTPS is blocked — check the Azure network security
group's **outbound** rules. (Your inbound rule for port 8000 is unrelated.)

### I want to stop the notifications immediately

```bash
sudo systemctl stop crypto-notify
```

That's it. Nothing else is affected.

### I want to remove it completely

```bash
sudo systemctl disable --now crypto-notify
sudo rm /etc/systemd/system/crypto-notify.service
sudo systemctl daemon-reload
```

You can also delete the two `CT_TELEGRAM__` lines from `/etc/crypto-trader/env`. The rest of the
system is unchanged either way.

---

## The rules that keep this safe

| | |
|---|---|
| **The bot token is a credential** | It lives only in `/etc/crypto-trader/env`, root-owned `0640`. Never in Git, never in a screenshot, never in a chat |
| **Never paste it into a conversation** | Including to me. If it leaks, `/revoke` it in BotFather |
| **This service never places a trade** | It has no order code — nothing in this project does — and it cannot even write to the database |
| **Messages are suggestions, not advice** | Every message says so. Nothing in this system has been validated against history |
| **Don't change the two filter variables** | `CT_TELEGRAM__ASSET` and `CT_TELEGRAM__STRATEGY` decide which suggestions get forwarded. Changing them is how you'd start receiving things you didn't ask for |
