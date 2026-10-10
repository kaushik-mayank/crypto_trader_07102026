#!/usr/bin/env bash
# Walk the whole chain and say which link is broken.
#
#   bash /opt/crypto-trader/deploy/azure/diagnose.sh
#
# Run it with `bash` rather than directly, so it works whether or not the executable bit survived
# the trip through Git on Windows.
#
# The chain is:
#
#   Vercel  ->  crypto-app (0.0.0.0:8000)  ->  SQLite  <-  crypto-ingest
#
# "The engine did not answer" is true of a break anywhere along it, which is why that message alone
# is not enough to act on. This script tests each link separately and in order, so the first FAIL is
# the thing to fix.
set -uo pipefail

APP_DIR=/opt/crypto-trader
PY="$APP_DIR/.venv/bin/python"
ENV_FILE=/etc/crypto-trader/env
SERVICE_USER=crypto
FAIL=0
WARN=0

ok()   { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$1"; FAIL=1; }
warn() { printf '  \033[33mWARN\033[0m  %s\n' "$1"; WARN=1; }
note() { printf '        %s\n' "$1"; }
head2(){ printf '\n\033[1m%s\033[0m\n' "$1"; }

# ---- 1. ownership, the thing that broke the first deployment ------------------------------
head2 "1. File ownership (data/ and logs/ must belong to '$SERVICE_USER')"
for dir in data logs; do
  path="$APP_DIR/$dir"
  if [ ! -d "$path" ]; then
    bad "$path does not exist"
    note "fix: sudo -u $SERVICE_USER mkdir -p $path"
    continue
  fi
  owner=$(stat -c '%U' "$path")
  if [ "$owner" = "$SERVICE_USER" ]; then
    ok "$dir/ is owned by $owner"
  else
    bad "$dir/ is owned by '$owner', not '$SERVICE_USER'"
    note "This stops the services writing. It is the single most common cause of a dead engine."
    note "fix: sudo chown -R $SERVICE_USER:$SERVICE_USER $path"
  fi
done

# A file inside that the service user cannot write is the same problem one level down — it happens
# when a command was run as the wrong user and created a file as root or azureuser.
if [ -d "$APP_DIR/data" ]; then
  foreign=$(find "$APP_DIR/data" "$APP_DIR/logs" ! -user "$SERVICE_USER" 2>/dev/null | head -5)
  if [ -n "$foreign" ]; then
    bad "some files inside data/ or logs/ are not owned by $SERVICE_USER:"
    echo "$foreign" | sed 's/^/        /'
    note "fix: sudo chown -R $SERVICE_USER:$SERVICE_USER $APP_DIR/data $APP_DIR/logs"
  else
    ok "every file inside data/ and logs/ belongs to $SERVICE_USER"
  fi
fi

# ---- 2. configuration ----------------------------------------------------------------------
head2 "2. Configuration"
if [ ! -f "$ENV_FILE" ]; then
  bad "$ENV_FILE is missing"
  note "fix: sudo cp $APP_DIR/deploy/azure/env.example $ENV_FILE && sudo nano $ENV_FILE"
else
  ok "$ENV_FILE exists"
  TOKEN=$(sudo grep -E '^CT_SERVER__AUTH_TOKEN=' "$ENV_FILE" 2>/dev/null | cut -d= -f2-)
  if [ -z "${TOKEN:-}" ]; then
    bad "CT_SERVER__AUTH_TOKEN is empty"
    note "The app refuses to start on a public address without it. Generate one:"
    note "  $PY -c \"import secrets; print(secrets.token_urlsafe(48))\""
  else
    ok "CT_SERVER__AUTH_TOKEN is set (${#TOKEN} characters)"
    note "Vercel's CT_API_TOKEN must match this exactly. Last 6 characters: ...${TOKEN: -6}"
  fi
  # On a direct bind the non-loopback address already makes the token mandatory, so this flag
  # should be false. It exists for a tunnel setup, which this deployment does not use.
  if sudo grep -qE '^CT_SERVER__PUBLIC_VIA_TUNNEL=true' "$ENV_FILE"; then
    warn "CT_SERVER__PUBLIC_VIA_TUNNEL=true, but this deployment binds directly"
    note "Harmless — it only adds a second reason the token is required. Set it to false to match."
  else
    ok "CT_SERVER__PUBLIC_VIA_TUNNEL is false (correct for a direct bind)"
  fi
fi

# ---- 3. the three services ------------------------------------------------------------------
head2 "3. Services"
for svc in crypto-ingest crypto-app; do
  if systemctl is-active --quiet "$svc"; then
    since=$(systemctl show -p ActiveEnterTimestamp --value "$svc")
    restarts=$(systemctl show -p NRestarts --value "$svc")
    if [ "${restarts:-0}" -gt 3 ]; then
      # A running service that has restarted many times is a crash loop caught mid-breath, which
      # looks identical to healthy in `is-active` alone.
      bad "$svc is running but has restarted ${restarts} times — this is a crash loop"
      note "see why: journalctl -u $svc -n 40 --no-pager"
    else
      ok "$svc running since $since (${restarts:-0} restarts)"
    fi
  else
    bad "$svc is NOT running"
    note "see why: journalctl -u $svc -n 40 --no-pager"
    note "then:    sudo systemctl restart $svc"
  fi
done

# ---- 4. the app answers on loopback ---------------------------------------------------------
head2 "4. The app answers locally"
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:8000/api/health 2>/dev/null || echo 000)
case "$CODE" in
  401) ok "unauthenticated request refused (401) — the app is up and the token is enforced" ;;
  200) bad "unauthenticated request returned 200 — the API is OPEN"
       note "CT_SERVER__AUTH_TOKEN is not set or not loaded. Fix it and restart crypto-app." ;;
  000) bad "nothing is listening on 127.0.0.1:8000"
       note "crypto-app is down or crash-looping: journalctl -u crypto-app -n 40 --no-pager" ;;
  *)   bad "unexpected status $CODE from the local API"
       note "journalctl -u crypto-app -n 40 --no-pager" ;;
esac

if [ -n "${TOKEN:-}" ] && [ "$CODE" = "401" ]; then
  AUTHED=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
           -H "x-auth-token: $TOKEN" http://127.0.0.1:8000/api/health 2>/dev/null || echo 000)
  if [ "$AUTHED" = "200" ]; then
    ok "authenticated request returned 200 — the engine is working"
  else
    bad "authenticated request returned $AUTHED"
    note "The token in $ENV_FILE may differ from the one the running process loaded."
    note "fix: sudo systemctl restart crypto-app"
  fi
fi

# ---- 4b. is this engine new enough for the dashboard? ---------------------------------------
# The check that was missing, and it cost two rounds of debugging.
#
# Every section above passed on an installation where the chart page was broken, because every
# section above asks "is the engine working" and none asked "is the engine the version the dashboard
# expects". The VM and Vercel deploy separately, so they drift apart by default, and the symptom is a
# single page 404ing while the rest of the dashboard looks perfect (F-055, F-059).
#
# So: ask the running process which routes it actually serves. A 404 here names the missing route
# and the fix, instead of leaving a reader to suspect their settings.
head2 "4b. Does this engine serve every route the dashboard needs?"
if [ -n "${TOKEN:-}" ] && [ "$CODE" = "401" ]; then
  MISSING=""
  for route in       "/api/signals/live"       "/api/health"       "/api/regime"       "/api/candles/B-BTC_USDT"       "/api/sources"; do
    RC=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8          -H "x-auth-token: $TOKEN" "http://127.0.0.1:8000${route}" 2>/dev/null || echo 000)
    case "$RC" in
      200|503) ok "$route -> $RC" ;;
      404)     bad "$route -> 404: this engine does not have that route"
               MISSING="$MISSING $route" ;;
      *)       warn "$route -> $RC (unexpected, but the route exists)" ;;
    esac
  done
  if [ -n "$MISSING" ]; then
    note ""
    note "This engine is OLDER than the dashboard. It is not a settings problem and not a firewall"
    note "problem: the code on this machine does not contain those routes."
    note ""
    note "fix, in this order:"
    note "  1. ON YOUR LAPTOP, make sure the code is actually pushed:"
    note "       git status            # must show nothing left to commit"
    note "       git push"
    note "  2. HERE:"
    note "       sudo bash $APP_DIR/deploy/azure/update.sh"
    note "  3. HERE, confirm the routes appear:"
    note "       bash $APP_DIR/deploy/azure/diagnose.sh"
    note ""
    note "If step 2 says 'Already up to date' while this still fails, the code never reached"
    note "GitHub — check step 1 again. That is the common case."
  fi
else
  warn "skipped: needs a working local API and a token (see section 4)"
fi

# ---- 4c. which commit is this machine running? ----------------------------------------------
# Printed unconditionally, because the answer to "did my push arrive" is one line and nobody should
# have to reconstruct it from timestamps.
head2 "4c. Deployed version"
if [ -d "$APP_DIR/.git" ]; then
  DESC=$(sudo -u "$SERVICE_USER" git -C "$APP_DIR" log -1 --format='%h %cd %s' --date=short 2>/dev/null)
  if [ -n "$DESC" ]; then
    note "running commit: $DESC"
    BEHIND=$(sudo -u "$SERVICE_USER" git -C "$APP_DIR" rev-list --count HEAD..@{u} 2>/dev/null || echo "?")
    if [ "$BEHIND" = "0" ]; then
      ok "up to date with the branch it tracks"
    elif [ "$BEHIND" = "?" ]; then
      note "could not compare with the remote (no network, or no upstream set)"
    else
      bad "$BEHIND commit(s) behind the remote"
      note "fix: sudo bash $APP_DIR/deploy/azure/update.sh"
    fi
  else
    warn "could not read the git log here"
  fi
else
  warn "$APP_DIR is not a git checkout, so the version cannot be reported"
fi

# ---- 5. reachable from outside -------------------------------------------------------------
head2 "5. Reachable from outside (Vercel -> this machine)"
BIND=$(sudo grep -E '^CT_SERVER__HOST=' "$ENV_FILE" 2>/dev/null | cut -d= -f2- | tr -d '[:space:]')
case "${BIND:-}" in
  0.0.0.0|"::")
    ok "CT_SERVER__HOST=$BIND — listening on the network interface, so Vercel can reach it" ;;
  127.0.0.1|localhost|"")
    bad "CT_SERVER__HOST is '${BIND:-unset}' — the engine only listens to itself"
    note "Nothing outside this machine can reach it. Set CT_SERVER__HOST=0.0.0.0 in $ENV_FILE,"
    note "then: sudo systemctl restart crypto-app" ;;
  *)
    warn "CT_SERVER__HOST is '$BIND', which is neither loopback nor 0.0.0.0" ;;
esac

# What the kernel thinks, which is the only authority on whether the port is actually open.
if command -v ss >/dev/null 2>&1; then
  LISTEN=$(ss -ltnH 2>/dev/null | awk '{print $4}' | grep -E ':8000$' | head -1)
  if [ -n "$LISTEN" ]; then
    case "$LISTEN" in
      0.0.0.0:8000|\*:8000|'[::]:8000')
        ok "the kernel shows port 8000 listening on all interfaces ($LISTEN)" ;;
      127.0.0.1:8000)
        bad "port 8000 is listening on loopback only ($LISTEN)"
        note "The setting and the running process disagree — the app has not been restarted since"
        note "the setting changed: sudo systemctl restart crypto-app" ;;
      *) warn "port 8000 is listening on $LISTEN" ;;
    esac
  else
    bad "nothing is listening on port 8000 at all"
  fi
fi

IP=$(curl -s --max-time 6 https://api.ipify.org 2>/dev/null || true)
note ""
note "This script cannot see Azure's firewall. Confirm in the portal that the VM has an inbound"
note "rule allowing TCP 8000, then FROM YOUR LAPTOP (not here) run:"
if [ -n "$IP" ]; then
  note "  curl -s -o /dev/null -w \"%{http_code}\n\" http://$IP:8000/api/health"
else
  note "  curl -s -o /dev/null -w \"%{http_code}\n\" http://YOUR_VM_IP:8000/api/health"
fi
note "Expect 401 — reachable and refusing strangers. A hang means the firewall rule is missing;"
note "a 200 would mean the port is open AND unauthenticated, which must be fixed immediately."

# Probing is expected on a public port; a rising count from one address is not.
REFUSED=$(journalctl -u crypto-app --since '1 day ago' 2>/dev/null           | grep -c 'api_requests_refused' || true)
if [ "${REFUSED:-0}" -gt 0 ]; then
  warn "$REFUSED refusal summaries in the last day — the port is being probed"
  note "A handful is normal background noise on any public port. A steady stream from one address"
  note "means someone is working through tokens: rotate CT_SERVER__AUTH_TOKEN here and in Vercel."
  note "see: journalctl -u crypto-app --since '1 day ago' | grep api_requests_refused | tail -3"
else
  ok "no refusal summaries in the last day"
fi

# ---- 6. data ---------------------------------------------------------------------------------
head2 "6. Price history"
if [ -f "$APP_DIR/data/state.db" ]; then
  size=$(du -h "$APP_DIR/data/state.db" | cut -f1)
  ok "database exists ($size)"
  if sudo -u "$SERVICE_USER" "$PY" -m bootstrap --check >/tmp/bootstrap-check.txt 2>&1; then
    ok "price history is complete for the live profile"
  else
    missing=$(grep -oE '[0-9]+ bars missing' /tmp/bootstrap-check.txt | tail -1)
    warn "price history incomplete: ${missing:-see /tmp/bootstrap-check.txt}"
    note "fix: cd $APP_DIR && sudo -u $SERVICE_USER $PY -m bootstrap --run"
  fi
else
  bad "no database at $APP_DIR/data/state.db"
  note "fix: cd $APP_DIR && sudo -u $SERVICE_USER $PY -m bootstrap --run"
fi

# ---- 7. disk ---------------------------------------------------------------------------------
head2 "7. Disk"
USED=$(df --output=pcent "$APP_DIR" 2>/dev/null | tail -1 | tr -dc '0-9')
if [ "${USED:-100}" -lt 85 ]; then
  ok "disk ${USED}% used"
else
  bad "disk ${USED}% used — a full disk stops writes while leaving the service apparently running"
fi

# ---- 8. recent errors ------------------------------------------------------------------------
head2 "8. Errors in the last hour"
for svc in crypto-ingest crypto-app; do
  n=$(journalctl -u "$svc" --since '1 hour ago' 2>/dev/null | grep -ciE '"level": *"(error|critical)"' || true)
  printf '        %-16s %s error-level lines\n' "$svc" "${n:-0}"
done
note "A handful of DQ_STALE lines is normal — a source was briefly late and the engine declined"
note "to guess. Repeated 'application_bug' lines are not normal; read those."

# ---- verdict ----------------------------------------------------------------------------------
printf '\n'
if [ "$FAIL" -ne 0 ]; then
  printf '\033[31mSomething is broken.\033[0m Fix the first FAIL above, then run this again.\n'
  exit 1
elif [ "$WARN" -ne 0 ]; then
  printf '\033[33mWorking, with warnings.\033[0m The engine is up; read the WARN lines.\n'
  exit 0
else
  printf '\033[32mEverything checks out on this machine.\033[0m\n'
  printf 'If the dashboard still cannot reach it, the break is the firewall or Vercel:\n'
  printf '  - does the Azure network security group allow inbound TCP 8000?\n'
  printf '  - is CT_API_BASE exactly http://YOUR_VM_IP:8000 — http not https, and no trailing slash?\n'
  printf '  - did you REDEPLOY Vercel after setting the variables? It does not apply them otherwise.\n'
  exit 0
fi
