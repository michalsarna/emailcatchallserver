#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

HTTP_PORT="${HTTP_PORT:-8001}"
SMTP_PORT="${SMTP_PORT:-1025}"

if lsof -nP -iTCP:"$HTTP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $HTTP_PORT is already in use — is mailcatch already running?" >&2
  exit 1
fi
if lsof -nP -iTCP:"$SMTP_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $SMTP_PORT is already in use — is mailcatch already running?" >&2
  exit 1
fi

exec python3 mailcatch.py --http-port "$HTTP_PORT" --smtp-port "$SMTP_PORT" "$@"
