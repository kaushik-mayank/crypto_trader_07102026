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
#   Vercel  ->  Cloudflare tunnel  ->  crypto-app (127.0.0.1:8000)  ->  SQLite  <-  crypto-ingest
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
    note "The app refuses to start without it when a tunnel is declared. Generate one:"
    note "  $PY -c \"import secrets; print(secrets.token_urlsafe(48))\""
  else
    ok "CT_SERVER__AUTH_TOKEN is set (${#TOKEN} characters)"
    note "Vercel's CT_API_TOKEN must match this exactly. Last 6 characters: ...${TOKEN: -6}"
  fi
  if sudo grep -qE '^CT_SERVER__PUBLIC_VIA_TUNNEL=true' "$ENV_FILE"; then
    ok "CT_SERVER__PUBLIC_VIA_TUNNEL=true (the token is therefore mandatory)"
  else
    warn "CT_SERVER__PUBLIC_VIA_TUNNEL is not 'true'"
    note "Set it, so the app refuses to run unauthenticated behind the tunnel."
  fi
fi

# ---- 3. the three services ------------------------------------------------------------------
head2 "3. Services"
for svc in crypto-ingest crypto-app crypto-tunnel; do
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

# ---- 5. the tunnel --------------------------------------------------------------------------
head2 "5. The tunnel (Cloudflare -> this machine)"
if systemctl is-active --quiet crypto-tunnel; then
  # cloudflared logs its registered connections. No connection means the token is wrong or outbound
  # traffic is blocked; a connection but no route means the Public Hostname was never added.
  if journalctl -u crypto-tunnel --since '10 minutes ago' 2>/dev/null \
       | grep -qiE 'Registered tunnel connection|Connection .* registered'; then
    ok "cloudflared has registered a connection to Cloudflare"
  else
    warn "no 'Registered tunnel connection' in the last 10 minutes of the tunnel's log"
    note "check: journalctl -u crypto-tunnel -n 30 --no-pager"
    note "If the token is wrong, the log says so plainly."
  fi
  note "This script cannot see Cloudflare's routing table. Confirm in the dashboard that your"
  note "tunnel has a Public Hostname whose Service is HTTP -> localhost:8000."
  note "Then, FROM YOUR LAPTOP (not here), run:"
  note "  curl -s -o /dev/null -w \"%{http_code}\\n\" https://YOUR-TUNNEL-HOSTNAME/api/health"
  note "Expect 401. A 502 or a hang means the Public Hostname is missing or points elsewhere."
else
  bad "crypto-tunnel is not running, so nothing outside this machine can reach the engine"
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
  printf 'If the dashboard still cannot reach it, the break is between Cloudflare and Vercel:\n'
  printf '  - is CT_API_BASE in Vercel exactly your tunnel hostname, with https:// and no trailing slash?\n'
  printf '  - did you REDEPLOY Vercel after setting the variables? It does not apply them otherwise.\n'
  exit 0
fi
