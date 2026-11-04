#!/usr/bin/env python3
"""Live integration smoke: MidPoint unassign → Invite by_provisioned_scim.

Runs inside the ``invite_bridge`` container:

  docker compose -f compose.yml -f compose.traefik.yml cp \\
    scripts/integration_membership_revoke.py invite_bridge:/tmp/itest.py
  docker compose -f compose.yml -f compose.traefik.yml exec -T invite_bridge \\
    python /tmp/itest.py

If MidPoint has no Invite-SCIM membership but Invite still has one (drift),
the script recreates the MidPoint user/assignment from known SCIM ids, then
revokes — proving the notify path clears Invite.

Exit: 0 pass, 2 skip, 1 fail.
"""

from __future__ import annotations

import json
import os
import sys
import time

import requests

sys.path.insert(0, "/app")
import app as bridge  # noqa: E402

SCIM_NS = bridge.SCIM_DETAILS_NS

# Known from prior E2E cycle (Invite role 15 / MidPoint e2e-demo-083733).
KNOWN_USER_SCIM = "dcb1ccc0-e651-4614-8b42-e03e0366777c"
KNOWN_GROUP_SCIM = "8fa86039-2df3-4b80-9666-7e4b3c97ca73"
KNOWN_INVITE_ROLE_ID = 15
KNOWN_EMAIL = "harry@fried-air.com"
KNOWN_DISPLAY = "Harry FriedAir"


def _log(msg: str) -> None:
    print(msg, flush=True)


def _invite_headers() -> dict[str, str]:
    return {
        "X-API-TOKEN": os.environ["INVITE_API_TOKEN"].strip(),
        "accept": "application/json",
        "Content-Type": "application/json",
    }


def _invite_role_member_count(invite_role_id: int) -> int:
    base = os.environ["INVITE_API_BASE_URL"].rstrip("/")
    response = requests.get(
        f"{base}/user_roles/roles/{invite_role_id}",
        headers=_invite_headers(),
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return len(data) if isinstance(data, list) else 0


def _ensure_scim_user(mp: bridge.MidPointClient, oid: str) -> None:
    existing = mp.get_user(oid)
    scim_doc = {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
        "id": oid,
        "userName": KNOWN_EMAIL,
        "externalId": KNOWN_EMAIL,
        "displayName": KNOWN_DISPLAY,
        "name": {"givenName": "Harry", "familyName": "FriedAir"},
        "emails": [{"value": KNOWN_EMAIL, "type": "other"}],
    }
    payload = {
        "name": KNOWN_EMAIL,
        "fullName": KNOWN_DISPLAY,
        "givenName": "Harry",
        "familyName": "FriedAir",
        "emailAddress": KNOWN_EMAIL,
        "extension": {
            "@ns": SCIM_NS,
            bridge.SCIM_DETAILS_ATTR: json.dumps(scim_doc),
        },
    }
    if existing is None:
        payload["oid"] = oid
        mp._request("POST", "/users", json={"user": payload})
        _log(f"Created MidPoint user {oid}")
        return
    mp._request(
        "PATCH",
        f"/users/{oid}",
        json={
            "objectModification": {
                "itemDelta": [
                    {
                        "modificationType": "replace",
                        "path": "extension/scimResource",
                        "value": json.dumps(scim_doc),
                    }
                ]
            }
        },
    )
    _log(f"Refreshed scimResource on MidPoint user {oid}")


def _assign(mp: bridge.MidPointClient, user_oid: str, role_oid: str) -> None:
    members = {u.get("oid") for u in mp.list_role_members(role_oid)}
    if user_oid in members:
        _log("Assignment already present")
        return
    mp._request(
        "PATCH",
        f"/users/{user_oid}",
        json={
            "objectModification": {
                "itemDelta": [
                    {
                        "modificationType": "add",
                        "path": "assignment",
                        "value": {
                            "targetRef": {"oid": role_oid, "type": "RoleType"}
                        },
                    }
                ]
            }
        },
    )
    _log(f"Assigned {user_oid} → {role_oid}")


def _unassign(mp: bridge.MidPointClient, user_oid: str, role_oid: str) -> None:
    mp._request(
        "PATCH",
        f"/users/{user_oid}",
        json={
            "objectModification": {
                "itemDelta": [
                    {
                        "modificationType": "delete",
                        "path": "assignment",
                        "value": {
                            "targetRef": {"oid": role_oid, "type": "RoleType"}
                        },
                    }
                ]
            }
        },
    )


def main() -> int:
    mp = bridge.MidPointClient(
        os.environ.get("MIDPOINT_URL", "http://midpoint:8080/midpoint/ws/rest"),
        os.environ.get("MIDPOINT_USERNAME", "administrator"),
        os.environ["MIDPOINT_PASSWORD"],
    )
    invite = bridge.InviteClient(
        os.environ["INVITE_API_BASE_URL"].rstrip("/"),
        os.environ["INVITE_API_TOKEN"].strip(),
        os.environ["INVITE_ORGANIZATION_GUID"].strip(),
        os.environ["INVITE_APPLICATION_MANAGE_ID"].strip(),
        os.environ.get("INVITE_APPLICATION_MANAGE_TYPE", "OIDC10_RP").strip(),
        os.environ.get(
            "INVITE_APPLICATION_LANDING_PAGE", "https://app.example/"
        ).strip(),
        int(os.environ.get("INVITE_DEFAULT_EXPIRY_DAYS", "365")),
    )

    state_path = os.environ.get("INVITE_BRIDGE_STATE", "/data/invite_links.json")
    memberships_path = bridge._memberships_state_path(state_path)
    known = bridge._load_state(state_path)
    memberships = bridge._load_memberships_state(memberships_path)

    before = _invite_role_member_count(KNOWN_INVITE_ROLE_ID)
    _log(f"Invite role {KNOWN_INVITE_ROLE_ID} members before: {before}")

    role = next(
        (r for r in mp.list_roles() if r.get("oid") == KNOWN_GROUP_SCIM), None
    )
    if role is None or not bridge._role_has_scim_resource(role):
        _log(
            f"SKIP: MidPoint role {KNOWN_GROUP_SCIM} missing or not SCIM-managed"
        )
        return 2
    if known.get(KNOWN_GROUP_SCIM) is None and bridge._invite_id_from_cost_center(
        role.get("costCenter")
    ) is None:
        _log("SKIP: MidPoint role not Invite-linked")
        return 2

    _ensure_scim_user(mp, KNOWN_USER_SCIM)
    _assign(mp, KNOWN_USER_SCIM, KNOWN_GROUP_SCIM)

    memberships.setdefault(KNOWN_GROUP_SCIM, {})[KNOWN_USER_SCIM] = KNOWN_USER_SCIM
    bridge._save_memberships_state(memberships_path, memberships)

    # Give the daemon a moment; we drive sync ourselves for determinism.
    time.sleep(1)

    _log("Unassigning in MidPoint…")
    _unassign(mp, KNOWN_USER_SCIM, KNOWN_GROUP_SCIM)
    still = {u.get("oid") for u in mp.list_role_members(KNOWN_GROUP_SCIM)}
    if KNOWN_USER_SCIM in still:
        _log("FAIL: MidPoint still lists membership after unassign")
        return 1
    _log("MidPoint unassign OK")

    snap = bridge._load_memberships_state(memberships_path)
    bridge.sync_membership_revokes(mp, invite, known, snap, primed=True)
    bridge._save_memberships_state(memberships_path, snap)

    if KNOWN_USER_SCIM in snap.get(KNOWN_GROUP_SCIM, {}):
        _log("FAIL: bridge snapshot still has member after sync")
        return 1

    # Allow Invite a moment to commit.
    time.sleep(2)
    after = _invite_role_member_count(KNOWN_INVITE_ROLE_ID)
    _log(f"Invite role {KNOWN_INVITE_ROLE_ID} members after: {after}")

    if before > 0 and after != 0:
        _log(
            "FAIL: Invite still shows membership "
            f"(before={before} after={after}); by_provisioned_scim did not clear"
        )
        return 1
    if before == 0:
        # Invite already empty; at least confirm notify returned ok/gone.
        status = invite.delete_user_role_by_provisioned_scim(
            KNOWN_USER_SCIM, KNOWN_GROUP_SCIM
        )
        _log(f"Invite already empty; follow-up status={status}")
        if status == "error":
            return 1

    _log(
        json.dumps(
            {
                "result": "PASS",
                "invite_role_id": KNOWN_INVITE_ROLE_ID,
                "members_before": before,
                "members_after": after,
                "user_scim": KNOWN_USER_SCIM,
                "group_scim": KNOWN_GROUP_SCIM,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAIL: {exc}", flush=True)
        raise
