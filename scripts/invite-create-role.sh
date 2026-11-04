#!/usr/bin/env bash
# Create a SURF Invite role via the external API (manual MidPoint-leading step).
#
# Usage:
#   ./scripts/invite-create-role.sh [role-name]
#   ROLE_NAME=my-role ./scripts/invite-create-role.sh
#
# Requires INVITE_* in .env (or the environment).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "${ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source <(grep -E '^[A-Z0-9_]+=' "${ROOT}/.env" | grep -E '^INVITE_')
  set +a
fi

: "${INVITE_API_BASE_URL:?Set INVITE_API_BASE_URL}"
: "${INVITE_API_TOKEN:?Set INVITE_API_TOKEN}"
: "${INVITE_ORGANIZATION_GUID:?Set INVITE_ORGANIZATION_GUID}"
: "${INVITE_APPLICATION_MANAGE_ID:?Set INVITE_APPLICATION_MANAGE_ID}"

INVITE_APPLICATION_MANAGE_TYPE="${INVITE_APPLICATION_MANAGE_TYPE:-OIDC10_RP}"
INVITE_APPLICATION_LANDING_PAGE="${INVITE_APPLICATION_LANDING_PAGE:-https://app.pilot1.sram.surf.nl/}"
INVITE_DEFAULT_EXPIRY_DAYS="${INVITE_DEFAULT_EXPIRY_DAYS:-365}"

ROLE_NAME="${1:-${ROLE_NAME:-mp-manual-$(date +%Y%m%d%H%M%S)}}"
DESCRIPTION="${ROLE_DESCRIPTION:-Created via scripts/invite-create-role.sh}"

BODY=$(ROLE_NAME="$ROLE_NAME" DESCRIPTION="$DESCRIPTION" \
  ORG="$INVITE_ORGANIZATION_GUID" \
  APP="$INVITE_APPLICATION_MANAGE_ID" \
  APPTYPE="$INVITE_APPLICATION_MANAGE_TYPE" \
  LANDING="$INVITE_APPLICATION_LANDING_PAGE" \
  DAYS="$INVITE_DEFAULT_EXPIRY_DAYS" \
  python3 - <<'PY'
import json, os
print(json.dumps({
  "name": os.environ["ROLE_NAME"],
  "description": os.environ["DESCRIPTION"],
  "defaultExpiryDays": int(os.environ["DAYS"]),
  "enforceEmailEquality": False,
  "eduIDOnly": False,
  "overrideSettingsAllowed": False,
  "organizationGUID": os.environ["ORG"],
  "applicationUsages": [{
    "landingPage": os.environ["LANDING"],
    "application": {
      "manageId": os.environ["APP"],
      "manageType": os.environ["APPTYPE"],
    },
  }],
  "notifyInviter": False,
}))
PY
)

echo "POST ${INVITE_API_BASE_URL}/roles"
echo "name: ${ROLE_NAME}"
echo

curl -sS -X POST "${INVITE_API_BASE_URL}/roles" \
  -H "accept: application/json" \
  -H "Content-Type: application/json" \
  -H "X-API-TOKEN: ${INVITE_API_TOKEN}" \
  -d "${BODY}" | python3 -m json.tool

echo
echo "Optional follow-ups (replace ROLE_ID):"
echo "  curl -sS -H \"X-API-TOKEN: \$INVITE_API_TOKEN\" \"${INVITE_API_BASE_URL}/roles/ROLE_ID\" | jq"
echo "  curl -sS -X DELETE -H \"X-API-TOKEN: \$INVITE_API_TOKEN\" \"${INVITE_API_BASE_URL}/roles/ROLE_ID\" -w '\\nHTTP %{http_code}\\n'"
