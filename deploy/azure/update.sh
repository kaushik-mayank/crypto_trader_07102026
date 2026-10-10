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
  echo "    already up to date"
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
systemctl restart crypto-tunnel 2>/dev/null || true
sleep 5
systemctl is-active crypto-ingest crypto-app

echo "==> done"
$AS_SERVICE git log --oneline -1
echo
echo "Now confirm the whole chain:  bash $APP_DIR/deploy/azure/diagnose.sh"
