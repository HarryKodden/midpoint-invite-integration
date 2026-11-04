#!/usr/bin/env bash
# Smoke-test Moodle REST WS (cohorts) using the token from moodle_ws_init.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TOKEN_FILE_IN_VOLUME="moodle_ws_token"
DOMAIN="${DOMAIN:-pilot1.sram.surf.nl}"
BASE_URL="${MOODLE_WS_URL:-https://moodle.${DOMAIN}/webservice/rest/server.php}"

TOKEN="$(docker compose run --rm --no-deps --entrypoint cat moodle_ws_init /shared/${TOKEN_FILE_IN_VOLUME} 2>/dev/null || true)"
if [[ -z "${TOKEN}" ]]; then
  TOKEN="$(docker compose exec -T moodle_ws_init cat /shared/moodle_ws_token 2>/dev/null || true)"
fi
if [[ -z "${TOKEN}" ]]; then
  # Read from named volume via a helper container
  TOKEN="$(docker run --rm -v midpoint-invite-integration_moodle_ws_shared:/shared alpine:3.20 cat /shared/moodle_ws_token 2>/dev/null || true)"
fi
if [[ -z "${TOKEN}" ]]; then
  echo "No Moodle WS token found. Start moodle_ws_init first." >&2
  exit 1
fi

echo "Calling core_webservice_get_site_info via ${BASE_URL}..."
curl -fsS -X POST "$BASE_URL" \
  --data-urlencode "wstoken=${TOKEN}" \
  --data-urlencode "wsfunction=core_webservice_get_site_info" \
  --data-urlencode "moodlewsrestformat=json" | head -c 500
echo
echo "OK"
