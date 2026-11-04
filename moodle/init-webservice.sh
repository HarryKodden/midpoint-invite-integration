#!/bin/sh
set -eu
PHP_BIN="${PHP_BIN:-/opt/bitnami/php/bin/php}"
SCRIPT="${INIT_SCRIPT:-/tmp/init_midpoint_webservice.php}"
TOKEN_FILE="${MOODLE_WS_TOKEN_FILE:-/shared/moodle_ws_token}"
MOODLE_URL="${MOODLE_INTERNAL_URL:-http://moodle:8080}"

echo "Waiting for Moodle HTTP at ${MOODLE_URL}..."
i=0
while true; do
  if command -v curl >/dev/null 2>&1; then
    curl -fsS -o /dev/null "${MOODLE_URL}/" && break
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O /dev/null "${MOODLE_URL}/" && break
  else
    # Fall back: config.php present means install finished.
    [ -f /bitnami/moodle/config.php ] && break
  fi
  i=$((i + 1))
  if [ "$i" -gt 90 ]; then
    echo "Timed out waiting for Moodle" >&2
    exit 1
  fi
  sleep 2
done

# Extra settle time after HTTP is up (DB upgrade / Apache).
sleep 3

echo "Running ${SCRIPT}..."
OUT="$("$PHP_BIN" "$SCRIPT")"
echo "$OUT"

# CLI runs as root; Apache is daemon — fix dataroot ownership for WS calls.
if [ "$(id -u)" = "0" ] && [ -d /bitnami/moodledata ]; then
  chown -R daemon:daemon /bitnami/moodledata 2>/dev/null || true
fi

TOKEN="$(printf '%s\n' "$OUT" | sed -n 's/^TOKEN=//p' | tail -1)"
if [ -z "$TOKEN" ]; then
  echo "No TOKEN= line in init output" >&2
  exit 1
fi

mkdir -p "$(dirname "$TOKEN_FILE")"
printf '%s' "$TOKEN" > "$TOKEN_FILE"
chmod 644 "$TOKEN_FILE"
echo "Wrote token to ${TOKEN_FILE}"
