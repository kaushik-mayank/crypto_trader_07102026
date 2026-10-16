#!/usr/bin/env bash
# Pull the latest code from GitHub and restart. Run it on the VM with sudo:
#
#   sudo bash /opt/crypto-trader/deploy/azure/update.sh
#
# `sudo bash` and not `./update.sh`, for two reasons:
#
#  - the executable bit does not survive the trip through Git on Windows, so the file may well not
#    be executable when it lands here;
#  - the repository belongs to the `crypto` user, so every Git and pip step has to run **as crypto**
#    (Git refuses to operate on a repository owned by someone else — "dubious ownership"), while
#    restarting a service needs root. This script needs both, so it takes root and steps down.
#
# Safe to run repeatedly. It stops at the first failure rather than leaving a service running half
# of one version and half of another, which produces results nobody can reproduce.
set -euo pipefail

APP_DIR=/opt/crypto-trader
SERVICE_USER=crypto
AS_SERVICE="sudo -u $SERVICE_USER"

if [ "$(id -u)" -ne 0 ]; then
  echo "!!! run this with sudo: sudo bash $APP_DIR/deploy/azure/update.sh" >&2
  exit 1
fi

cd "$APP_DIR"

echo "==> current version"
$AS_SERVICE git log --oneline -1

echo "==> fetching"
$AS_SERVICE git fetch --quiet origin
LOCAL=$($AS_SERVICE git rev-parse HEAD)
REMOTE=$($AS_SERVICE git rev-parse origin/main)
if [ "$LOCAL" = "$REMOTE" ]; then
  # "Already up to date" is the answer people read as success, and twice now it has meant the
  # opposite: the code was never pushed, so there was nothing to pull and the VM stayed old while
  # the dashboard moved on (F-059). Say what it actually proves and what it does not.
  echo "    already up to date with origin/main"
  echo
  echo "    NOTE: this means nothing new has been PUSHED, not that the VM has the newest code."
  echo "    If a dashboard page is still broken, check on your laptop that the code is committed:"
  echo "        git status          # must show nothing left to commit"
  echo "        git push"
  echo "    then run this script again."
else
  # Refuse to discard local edits silently. If someone hot-fixed a file on the VM, overwriting it
  # without a word is how a fix disappears and the bug comes back a week later looking new.
  if ! $AS_SERVICE git diff --quiet || ! $AS_SERVICE git diff --cached --quiet; then
    echo "!!! there are uncommitted changes on the VM:" >&2
    $AS_SERVICE git status --short >&2
    echo "!!! commit them, or discard with: sudo -u $SERVICE_USER git checkout -- ." >&2
    exit 1
  fi
  echo "==> updating to $REMOTE"
  $AS_SERVICE git merge --ff-only origin/main
  echo "==> dependencies"
  $AS_SERVICE ./.venv/bin/pip install --quiet -e .
fi

# Ownership is re-asserted on every update, because getting it wrong is the failure that took the
# first deployment down (F-042) and because it costs nothing to be sure.
echo "==> ownership of data/ and logs/"
mkdir -p "$APP_DIR/data" "$APP_DIR/logs"
chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/data" "$APP_DIR/logs"
echo "    both belong to $SERVICE_USER"

echo "==> checks before restarting"
# Preflight proves the new code can start: parameters load, every feature's inputs are present, the
# regime artifacts are readable. Finding that out here beats finding it out from a crash loop.
$AS_SERVICE ./.venv/bin/python -m ingest --check
$AS_SERVICE ./.venv/bin/python -m app --check

echo "==> restarting"
systemctl restart crypto-ingest
systemctl restart crypto-app
# The notifier is optional, so this is guarded. `is-enabled` returns non-zero when the unit was
# never installed, which must not fail the update of the two services that are. `set -e` is on, so
# the guard has to be the `if` rather than the command's own exit code.
if systemctl is-enabled crypto-notify >/dev/null 2>&1; then
  systemctl restart crypto-notify
  echo "    crypto-notify restarted"
else
  echo "    crypto-notify not installed; skipped (see TELEGRAM_HOSTING.md)"
fi
sleep 5
systemctl is-active crypto-ingest crypto-app

echo "==> routes the dashboard needs"
# Checked here as well as in diagnose.sh, because this is the moment someone is watching. An engine
# that restarts cleanly but does not serve a route the dashboard calls looks entirely healthy from
# every other angle, and the symptom appears on one page as a 404 that invites a settings hunt.
TOKEN=$(grep -E '^CT_SERVER__AUTH_TOKEN=' /etc/crypto-trader/env 2>/dev/null | cut -d= -f2- | tr -d '"' || true)
if [ -n "${TOKEN:-}" ]; then
  MISSING=""
  for route in /api/signals/live /api/health /api/candles/B-BTC_USDT /api/sources; do
    RC=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 \
         -H "x-auth-token: $TOKEN" "http://127.0.0.1:8000${route}" 2>/dev/null || echo 000)
    printf '    %-28s %s\n' "$route" "$RC"
    [ "$RC" = "404" ] && MISSING="$MISSING $route"
  done
  if [ -n "$MISSING" ]; then
    echo
    echo "!!! this engine does not serve:$MISSING" >&2
    echo "!!! the code for those routes is not in the commit above, which means it was never" >&2
    echo "!!! pushed. On your laptop: git status, then git add -A, git commit, git push." >&2
  fi
else
  echo "    skipped: CT_SERVER__AUTH_TOKEN not readable from /etc/crypto-trader/env"
fi

echo "==> done"
$AS_SERVICE git log --oneline -1
echo
echo "Now confirm the whole chain:  bash $APP_DIR/deploy/azure/diagnose.sh"
