# SCIM → MidPoint — curl examples

SCIM is backed by MidPoint REST (`MIDPOINT_URL`). Groups map to MidPoint roles;
members are role assignments. The image is built from sibling `../scim`
(MidPoint plugin).

```bash
BASE=https://scim.pilot1.sram.surf.nl/api/v2
AUTH='Authorization: Bearer secret'   # must match SCIM_API_KEY
CT='Content-Type: application/scim+json'
```

## Create user

```bash
curl -sS -X POST "$BASE/Users" \
  -H "$AUTH" -H "$CT" \
  -d '{
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
    "userName": "scim-guest-001",
    "externalId": "scim-guest-001",
    "displayName": "SCIM Guest",
    "active": true
  }' | jq
```

Save the returned `id` as `USER_ID`.

With `SCIM_IDEMPOTENT_USER_CREATE=true` (default in this compose), a second
`POST /Users` with the same `userName` / `externalId` **reuses** the existing
User (HTTP 201) instead of returning 409 — important when Invite re-provisions.

## Create group with member

```bash
curl -sS -X POST "$BASE/Groups" \
  -H "$AUTH" -H "$CT" \
  -d "{
    \"schemas\": [\"urn:ietf:params:scim:schemas:core:2.0:Group\"],
    \"displayName\": \"scim-demo-role\",
    \"members\": [{\"value\": \"$USER_ID\"}]
  }" | jq
```

With `MIDPOINT_LEADING_GROUPS=true` (default here):

- Group create **reuses** an existing MidPoint Role whose `name` matches
  `displayName` (Invite echo after `invite_bridge` created the Invite Role).
- Group `DELETE` is a **no-op** (does not delete the MidPoint Role).
- Empty `members: []` does **not** wipe MidPoint assignments.

## List / get / patch membership

```bash
curl -sS "$BASE/Users" -H "$AUTH" | jq
curl -sS "$BASE/Groups" -H "$AUTH" | jq

curl -sS -X PATCH "$BASE/Groups/$GROUP_ID" \
  -H "$AUTH" -H "$CT" \
  -d "{
    \"schemas\": [\"urn:ietf:params:scim:api:messages:2.0:PatchOp\"],
    \"Operations\": [{
      \"op\": \"add\",
      \"path\": \"members\",
      \"value\": [{\"value\": \"$USER_ID\"}]
    }]
  }" | jq
```

## Notes

- Confirm in MidPoint UI at `https://midpoint.${DOMAIN}/midpoint`.
- Raw SCIM JSON is stored on MidPoint as `extension/scimResource` (marks
  Invite-provisioned objects for `invite_bridge` drift prevention).
- Prefer `Content-Type: application/scim+json` (Invite often sends
  `application/json`; the server logs a warning but still processes).
