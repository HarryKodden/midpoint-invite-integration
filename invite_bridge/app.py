"""Sync MidPoint application Roles → OpenConext Invite Roles (MidPoint-leading),
and serve a simple "Invite user" form linked from the MidPoint admin menu.

On each sync cycle:
  - MidPoint Role without Invite link → POST /api/external/v1/roles
  - MidPoint Role removed → DELETE /api/external/v1/roles/{id}
  - MidPoint admin unassigns an Invite-SCIM User from an Invite-SCIM Group
    → DELETE /api/external/v1/user_roles/by_provisioned_scim/{userId}/{groupId}
    (drift prevention; local revoke always wins if Invite notify fails)

Invite role id is stored on the MidPoint Role as extension ``inviteRoleId``
(roles linked by older bridge versions used ``costCenter = invite:{id}``, still
read as a fallback).

HTTP (Flask):
  GET/POST /invite  — form that calls Invite POST /invitations
  GET /health       — liveness
"""

from __future__ import annotations

import html
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any
from urllib.parse import urlsplit

import requests
from flask import Flask, Response, redirect, request, url_for
from requests.auth import HTTPBasicAuth

logging.basicConfig(
    level=os.environ.get("LOGLEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("invite_bridge")

SYSTEM_ROLE_NAMES = {
    "Superuser",
    "End user",
    "Approver",
    "Reviewer",
    "Delegator",
    "metarole-ldap-group",
    "metarole-moodle-cohort",
}

COST_CENTER_PREFIX = "invite:"
# Extension attribute holding the Invite role id (preferred over costCenter).
INVITE_ROLE_ID_ATTR = os.environ.get("MIDPOINT_INVITE_ROLE_ID_ATTR", "inviteRoleId")
SYSTEM_CONFIG_OID = "00000000-0000-0000-0000-000000000001"
# Limit Invite role-create retries per MidPoint Role oid (in-memory; cleared on
# bridge restart). Default: one failed create, then no further attempts.
_CREATE_FAIL_MAX_ATTEMPTS = max(
    1, int(os.environ.get("INVITE_CREATE_FAIL_MAX_ATTEMPTS", "1"))
)
# Optional delay between attempts when MAX_ATTEMPTS > 1 (0 = immediate).
_CREATE_FAIL_BACKOFF_SECONDS = max(
    0, int(os.environ.get("INVITE_CREATE_FAIL_BACKOFF_SECONDS", "0"))
)
_create_fail_attempts: dict[str, int] = {}
_create_fail_until: dict[str, float] = {}
MENU_LINK_LABEL = "Invite user"

# Same extension item the SCIM MidPoint plugin writes after Invite provisions a Group.
SCIM_DETAILS_NS = os.environ.get(
    "MIDPOINT_SCIM_EXTENSION_NS",
    "https://github.com/HarryKodden/scim/xml/ns/midpoint",
)
SCIM_DETAILS_ATTR = os.environ.get("MIDPOINT_SCIM_EXTENSION_ATTR", "scimResource")


def _poly_name(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("orig") or value.get("norm") or value.get("_value")
    return str(value)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _invite_id_from_cost_center(value: Any) -> int | None:
    text = _poly_name(value) if not isinstance(value, str) else value
    if not text or not text.startswith(COST_CENTER_PREFIX):
        return None
    raw = text[len(COST_CENTER_PREFIX) :].strip()
    try:
        return int(raw)
    except ValueError:
        return None


def _invite_id_from_object(obj: dict) -> int | None:
    """Invite role id from extension ``inviteRoleId``, legacy costCenter fallback."""
    raw = _extension_item_value(obj.get("extension"), INVITE_ROLE_ID_ATTR, SCIM_DETAILS_NS)
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, int):
        return raw
    text = _poly_name(raw) if not isinstance(raw, str) else raw
    if text and str(text).strip().isdigit():
        return int(str(text).strip())
    return _invite_id_from_cost_center(obj.get("costCenter"))


def _extension_item_value(extension: Any, attr: str, ns: str) -> Any:
    """Pull ``attr`` from a MidPoint ``extension`` JSON object (any ns form)."""
    if not isinstance(extension, dict):
        return None
    if attr in extension:
        return extension[attr]
    for key, value in extension.items():
        if key.startswith("@"):
            continue
        if key.endswith(f":{attr}") or key.endswith(f"#{attr}"):
            return value
        if key == ns and isinstance(value, dict) and attr in value:
            return value[attr]
        if isinstance(value, dict) and attr in value:
            nested_ns = value.get("@ns")
            if nested_ns in (None, ns):
                return value[attr]
    return None


def _parse_scim_details(raw: Any) -> dict | None:
    """Return parsed SCIM document, or None if not a SCIM-managed object."""
    if not raw:
        return None
    text = _poly_name(raw) if not isinstance(raw, str) else raw
    if not text:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or "schemas" not in data:
        return None
    return data


def _scim_details_from_object(obj: dict) -> dict | None:
    """Parsed SCIM document from extension/scimResource (or legacy description)."""
    raw = _extension_item_value(
        obj.get("extension"), SCIM_DETAILS_ATTR, SCIM_DETAILS_NS
    )
    details = _parse_scim_details(raw)
    if details:
        return details
    return _parse_scim_details(obj.get("description"))


def _role_has_scim_resource(role: dict) -> bool:
    """True when Invite (via SCIM) has stored a SCIM Group blob on this Role."""
    return _scim_details_from_object(role) is not None


def _user_has_scim_resource(user: dict) -> bool:
    """True when Invite (via SCIM) has stored a SCIM User blob on this User."""
    return _scim_details_from_object(user) is not None


def _scim_resource_id(obj: dict) -> str | None:
    """SCIM ``id`` Invite stored for this MidPoint object, if SCIM-managed.

    Prefer the ``id`` inside ``scimResource``; fall back to MidPoint ``oid``
    (leading mode uses Role/User oid as the SCIM resource id).
    """
    details = _scim_details_from_object(obj)
    if not details:
        return None
    scim_id = details.get("id") or obj.get("oid")
    return str(scim_id) if scim_id else None


class MidPointClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)
        self.session = requests.Session()
        self.session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            auth=self.auth,
            timeout=60,
            **kwargs,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"MidPoint {method} {path} → {response.status_code}: {response.text[:500]}"
            )
        return response

    def search(self, endpoint: str, filter_text: str | None = None) -> list[dict]:
        body: dict[str, Any] = {"query": {}}
        if filter_text:
            body["query"]["filter"] = {"text": filter_text}
        data = self._request("POST", f"/{endpoint}/search", json=body).json()
        objects = data.get("object", {}).get("object", [])
        return _as_list(objects)

    def list_roles(self) -> list[dict]:
        return self.search("roles")

    def list_role_members(self, role_oid: str) -> list[dict]:
        return self.search(
            "users", f'roleMembershipRef matches (oid = "{role_oid}")'
        )

    def get_user(self, user_oid: str) -> dict | None:
        try:
            data = self._request("GET", f"/users/{user_oid}").json()
        except RuntimeError:
            return None
        return data.get("user") or data

    def set_invite_link(self, role_oid: str, invite_role_id: int) -> None:
        self._request(
            "PATCH",
            f"/roles/{role_oid}",
            json={
                "objectModification": {
                    "itemDelta": [
                        {
                            "modificationType": "replace",
                            "path": f"extension/{INVITE_ROLE_ID_ATTR}",
                            "value": str(invite_role_id),
                        }
                    ]
                }
            },
        )

    def ping(self) -> None:
        """Raise if MidPoint REST is unreachable."""
        self.get_system_configuration()

    def get_system_configuration(self) -> dict:
        body = self._request(
            "GET", f"/systemConfigurations/{SYSTEM_CONFIG_OID}"
        ).json()
        return body.get("systemConfiguration") or body

    def ensure_invite_menu_link(self, target_url: str, label: str = MENU_LINK_LABEL) -> None:
        """Add or refresh adminGuiConfiguration/additionalMenuLink for Invite user."""
        cfg = self.get_system_configuration()
        gui = cfg.get("adminGuiConfiguration") or {}
        links = [
            link
            for link in _as_list(gui.get("additionalMenuLink"))
            if isinstance(link, dict)
        ]
        desired = {
            "targetUrl": target_url,
            "label": label,
            "description": (
                "Create an OpenConext Invite invitation "
                "(does not create a MidPoint user yet)"
            ),
        }

        invite_links = [
            link for link in links if _poly_name(link.get("label")) == label
        ]
        if (
            len(invite_links) == 1
            and invite_links[0].get("targetUrl") == target_url
        ):
            logger.info(
                "MidPoint menu link already present: %s → %s", label, target_url
            )
            return

        # Preserve unrelated menu links; replace all "Invite user" entries.
        kept = []
        for link in links:
            if _poly_name(link.get("label")) == label:
                continue
            item = {
                "targetUrl": link.get("targetUrl"),
                "label": link.get("label"),
            }
            if link.get("description"):
                item["description"] = link["description"]
            kept.append(item)
        kept.append(desired)

        self._request(
            "PATCH",
            f"/systemConfigurations/{SYSTEM_CONFIG_OID}",
            json={
                "objectModification": {
                    "itemDelta": [
                        {
                            "modificationType": "replace",
                            "path": "adminGuiConfiguration/additionalMenuLink",
                            "value": kept,
                        }
                    ]
                }
            },
        )
        logger.info("Updated MidPoint menu link %s → %s", label, target_url)


class InviteClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        organization_guid: str,
        manage_id: str,
        manage_type: str,
        landing_page: str,
        default_expiry_days: int,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.organization_guid = organization_guid
        self.manage_id = manage_id
        # Invite EntityType enum is uppercase (SAML20_SP, OIDC10_RP, …).
        self.manage_type = manage_type.strip().upper()
        self.landing_page = landing_page
        self.default_expiry_days = default_expiry_days
        self.session = requests.Session()
        self.session.headers.update(
            {
                "accept": "application/json",
                "Content-Type": "application/json",
                "X-API-TOKEN": token,
            }
        )

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            timeout=90,
            **kwargs,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Invite {method} {path} → {response.status_code}: {response.text[:800]}"
            )
        return response

    def ping(self) -> None:
        """Raise if the Invite API is unreachable."""
        self._request(
            "GET",
            "/roles",
            params={"force": "false", "pageNumber": 0, "pageSize": 1},
        )

    def find_role_by_name(self, name: str) -> dict | None:
        # Search API is paginated; query filters server-side.
        response = self._request(
            "GET",
            "/roles",
            params={
                "force": "false",
                "pageNumber": 0,
                "pageSize": 20,
                "query": name,
            },
        )
        data = response.json()
        for role in data.get("content") or []:
            if role.get("name") == name:
                return role
        return None

    def list_roles(self, page_size: int = 100) -> list[dict]:
        roles: list[dict] = []
        page = 0
        while True:
            data = self._request(
                "GET",
                "/roles",
                params={
                    "force": "false",
                    "pageNumber": page,
                    "pageSize": page_size,
                    "sort": "name",
                },
            ).json()
            batch = data.get("content") or []
            roles.extend(batch)
            total = data.get("totalElements")
            if not batch:
                break
            if total is not None and len(roles) >= int(total):
                break
            if len(batch) < page_size:
                break
            page += 1
            if page > 50:
                break
        return roles

    def create_role(self, name: str, description: str | None = None) -> dict:
        payload = {
            "name": name,
            "description": description or f"Synced from MidPoint role {name}",
            "defaultExpiryDays": self.default_expiry_days,
            "enforceEmailEquality": False,
            "eduIDOnly": False,
            "overrideSettingsAllowed": False,
            "organizationGUID": self.organization_guid,
            "applicationUsages": [
                {
                    "landingPage": self.landing_page,
                    "application": {
                        "manageId": self.manage_id,
                        "manageType": self.manage_type,
                    },
                }
            ],
            "notifyInviter": False,
        }
        return self._request("POST", "/roles", json=payload).json()

    def delete_role(self, invite_role_id: int) -> None:
        response = self.session.request(
            "DELETE",
            f"{self.base_url}/roles/{invite_role_id}",
            timeout=90,
            headers=self.session.headers,
        )
        if response.status_code in (204, 404):
            return
        if response.status_code >= 400:
            raise RuntimeError(
                f"Invite DELETE /roles/{invite_role_id} → "
                f"{response.status_code}: {response.text[:800]}"
            )

    def delete_user_role_by_provisioned_scim(
        self, user_scim_id: str, group_scim_id: str
    ) -> str:
        """Revoke Invite UserRole by our SCIM User/Group ids.

        Returns ``ok`` (204), ``gone`` (404, idempotent), or ``error``.
        Does not raise on HTTP errors — caller keeps local revoke and alerts.
        """
        path = (
            f"/user_roles/by_provisioned_scim/"
            f"{user_scim_id}/{group_scim_id}"
        )
        try:
            response = self.session.request(
                "DELETE",
                f"{self.base_url}{path}",
                timeout=90,
                headers=self.session.headers,
            )
        except requests.RequestException:
            logger.exception(
                "Invite notify failed (network) for membership "
                "user_scim=%s group_scim=%s",
                user_scim_id,
                group_scim_id,
            )
            return "error"
        if response.status_code in (204, 200):
            return "ok"
        if response.status_code == 404:
            logger.info(
                "Invite membership already gone (404) user_scim=%s group_scim=%s",
                user_scim_id,
                group_scim_id,
            )
            return "gone"
        logger.error(
            "Invite DELETE %s → %s: %s "
            "(local MidPoint revoke kept; Invite may still show membership)",
            path,
            response.status_code,
            response.text[:800],
        )
        return "error"

    def create_invitation(
        self,
        *,
        emails: list[str],
        role_ids: list[int],
        message: str,
        language: str,
        intended_authority: str,
        guest_role_included: bool,
        expiry_days: int,
        suppress_sending_emails: bool = False,
    ) -> dict:
        expiry = datetime.now(timezone.utc) + timedelta(days=expiry_days)
        payload = {
            "intendedAuthority": intended_authority,
            "message": message or "",
            "language": language,
            "guestRoleIncluded": guest_role_included,
            "suppressSendingEmails": suppress_sending_emails,
            "invites": emails,
            "roleIdentifiers": role_ids,
            "organizationGUID": self.organization_guid,
            "expiryDate": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "notifyInviter": False,
        }
        return self._request("POST", "/invitations", json=payload).json()


def sync_once(
    mp: MidPointClient,
    invite: InviteClient,
    known: dict[str, int],
) -> None:
    """Sync MidPoint roles to Invite; ``known`` maps MidPoint oid → Invite id."""
    roles = mp.list_roles()
    seen_oids: set[str] = set()

    for role in roles:
        name = _poly_name(role.get("name"))
        oid = role.get("oid")
        if not name or not oid or name in SYSTEM_ROLE_NAMES:
            continue
        if name.startswith("metarole-"):
            continue

        seen_oids.add(oid)
        invite_id = _invite_id_from_object(role)
        if invite_id is None:
            invite_id = known.get(oid)

        if invite_id is not None:
            known[oid] = invite_id
            continue

        existing = invite.find_role_by_name(name)
        if existing and existing.get("id") is not None:
            invite_id = int(existing["id"])
            _create_fail_attempts.pop(oid, None)
            _create_fail_until.pop(oid, None)
            logger.info(
                "Link existing Invite role %s ← MidPoint %s (%s)",
                invite_id,
                name,
                oid,
            )
        else:
            attempts = _create_fail_attempts.get(oid, 0)
            if attempts >= _CREATE_FAIL_MAX_ATTEMPTS:
                logger.warning(
                    "Skip Invite create for MidPoint %s (%s); "
                    "already failed %s/%s attempt(s) "
                    "(restart invite_bridge to retry)",
                    name,
                    oid,
                    attempts,
                    _CREATE_FAIL_MAX_ATTEMPTS,
                )
                continue
            now = time.time()
            blocked_until = _create_fail_until.get(oid, 0.0)
            if blocked_until > now:
                logger.warning(
                    "Skip Invite create for MidPoint %s (%s); "
                    "backoff %.0fs remaining after prior failure",
                    name,
                    oid,
                    blocked_until - now,
                )
                continue
            description = _poly_name(role.get("description"))
            # Avoid stuffing SCIM JSON into Invite description.
            if description and description.strip().startswith("{"):
                description = f"Synced from MidPoint role {name}"
            logger.info(
                "Create Invite role for MidPoint %s (%s) [attempt %s/%s]",
                name,
                oid,
                attempts + 1,
                _CREATE_FAIL_MAX_ATTEMPTS,
            )
            try:
                created = invite.create_role(name, description)
            except Exception:
                _create_fail_attempts[oid] = attempts + 1
                if (
                    _CREATE_FAIL_BACKOFF_SECONDS > 0
                    and _create_fail_attempts[oid] < _CREATE_FAIL_MAX_ATTEMPTS
                ):
                    _create_fail_until[oid] = now + _CREATE_FAIL_BACKOFF_SECONDS
                logger.exception(
                    "Invite create failed for MidPoint %s (%s); "
                    "attempts %s/%s%s",
                    name,
                    oid,
                    _create_fail_attempts[oid],
                    _CREATE_FAIL_MAX_ATTEMPTS,
                    (
                        f"; backoff {_CREATE_FAIL_BACKOFF_SECONDS}s"
                        if _create_fail_until.get(oid, 0) > now
                        else "; no further auto-retries until restart"
                        if _create_fail_attempts[oid] >= _CREATE_FAIL_MAX_ATTEMPTS
                        else ""
                    ),
                )
                continue
            invite_id = int(created["id"])
            _create_fail_attempts.pop(oid, None)
            _create_fail_until.pop(oid, None)
            logger.info(
                "  Invite role id=%s identifier=%s",
                invite_id,
                created.get("identifier"),
            )

        known[oid] = invite_id
        try:
            mp.set_invite_link(oid, invite_id)
        except Exception:
            logger.exception(
                "Created Invite role %s but failed to set MidPoint extension %s on %s",
                INVITE_ROLE_ID_ATTR,
                invite_id,
                oid,
            )

    # MidPoint Role gone → delete Invite Role (only for oids we previously linked).
    for oid, invite_id in list(known.items()):
        if oid in seen_oids:
            continue
        logger.info(
            "MidPoint role %s gone → DELETE Invite role %s", oid, invite_id
        )
        try:
            invite.delete_role(invite_id)
            del known[oid]
        except Exception:
            logger.exception("Failed to delete Invite role %s", invite_id)


def _memberships_state_path(role_state_path: str) -> str:
    explicit = os.environ.get("INVITE_BRIDGE_MEMBERSHIPS_STATE", "").strip()
    if explicit:
        return explicit
    if role_state_path.endswith(".json"):
        return f"{role_state_path[:-5]}_memberships.json"
    return f"{role_state_path}.memberships"


def _load_state(path: str) -> dict[str, int]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return {str(k): int(v) for k, v in (data or {}).items()}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("Failed to load state %s; starting empty", path)
        return {}


def _save_state(path: str, known: dict[str, int]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(known, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _load_memberships_state(path: str) -> dict[str, dict[str, str]]:
    """role_oid → {user_oid → user_scim_id} for Invite-SCIM members last seen."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for role_oid, members in data.items():
            if not isinstance(members, dict):
                continue
            out[str(role_oid)] = {
                str(uid): str(scim_id) for uid, scim_id in members.items() if scim_id
            }
        return out
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("Failed to load memberships state %s; starting empty", path)
        return {}


def _save_memberships_state(path: str, memberships: dict[str, dict[str, str]]) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(memberships, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _invite_managed_roles(
    roles: list[dict], known: dict[str, int]
) -> list[dict]:
    """Roles that Invite SCIM-provisioned and we linked (Invite is SoT for guests)."""
    managed: list[dict] = []
    for role in roles:
        name = _poly_name(role.get("name"))
        oid = role.get("oid")
        if not name or not oid or name in SYSTEM_ROLE_NAMES or name.startswith("metarole-"):
            continue
        if not _role_has_scim_resource(role):
            continue
        invite_id = _invite_id_from_object(role) or known.get(oid)
        if invite_id is None:
            continue
        managed.append(role)
    return managed


def _current_invite_scim_members(
    mp: MidPointClient, role_oid: str
) -> dict[str, str]:
    """Map MidPoint user oid → SCIM User id for Invite-provisioned members only."""
    current: dict[str, str] = {}
    for user in mp.list_role_members(role_oid):
        user_oid = user.get("oid")
        if not user_oid:
            continue
        if not _user_has_scim_resource(user):
            continue
        scim_id = _scim_resource_id(user)
        if not scim_id:
            continue
        current[str(user_oid)] = scim_id
    return current


def sync_membership_revokes(
    mp: MidPointClient,
    invite: InviteClient,
    known: dict[str, int],
    memberships: dict[str, dict[str, str]],
    *,
    primed: bool,
) -> None:
    """Notify Invite when MidPoint removes Invite-SCIM users from Invite-SCIM roles.

    ``memberships`` is mutated in place. When ``primed`` is False (first cycle
    after empty state / restart with no file), only establish the baseline —
    never DELETE — so we do not mass-revoke on cold start.
    """
    roles = mp.list_roles()
    managed = _invite_managed_roles(roles, known)
    managed_oids = {str(r["oid"]) for r in managed if r.get("oid")}

    for role in managed:
        role_oid = str(role["oid"])
        group_scim_id = _scim_resource_id(role)
        if not group_scim_id:
            logger.warning(
                "Invite-managed MidPoint role %s has no SCIM Group id; skip membership sync",
                role_oid,
            )
            continue

        previous = memberships.get(role_oid, {})
        try:
            current = _current_invite_scim_members(mp, role_oid)
        except Exception:
            logger.exception(
                "Failed to list members for MidPoint role %s; keeping prior snapshot",
                role_oid,
            )
            continue

        if primed:
            for user_oid, user_scim_id in previous.items():
                if user_oid in current:
                    continue
                # Prefer cached SCIM id; refresh from MidPoint if user still exists.
                resolved_user_scim = user_scim_id
                still = mp.get_user(user_oid)
                if still is not None:
                    if not _user_has_scim_resource(still):
                        logger.info(
                            "Skip Invite revoke for MidPoint user %s on role %s "
                            "(not Invite-SCIM provisioned)",
                            user_oid,
                            role_oid,
                        )
                        continue
                    resolved_user_scim = _scim_resource_id(still) or user_scim_id
                logger.info(
                    "MidPoint removed Invite-SCIM user %s from role %s → "
                    "DELETE Invite by_provisioned_scim/%s/%s",
                    user_oid,
                    role_oid,
                    resolved_user_scim,
                    group_scim_id,
                )
                invite.delete_user_role_by_provisioned_scim(
                    resolved_user_scim, group_scim_id
                )

        memberships[role_oid] = current

    # Drop snapshots for roles that are gone or no longer Invite-SCIM managed.
    # Role delete already DELETEs the Invite Role (cascades memberships there).
    for role_oid in list(memberships.keys()):
        if role_oid not in managed_oids:
            del memberships[role_oid]

    if not primed and managed:
        logger.info(
            "Membership baseline established for %s Invite-SCIM role(s); "
            "subsequent removals will notify Invite",
            len(managed),
        )


def _linked_invite_roles(mp: MidPointClient, known: dict[str, int]) -> list[dict]:
    """Roles selectable for invitations.

    Only MidPoint Roles that Invite has already SCIM-provisioned (stored
    ``extension/scimResource``, or legacy SCIM JSON in ``description``) and that
    have an Invite role id (``extension/inviteRoleId`` or bridge state).
    """
    by_id: dict[int, str] = {}
    for role in mp.list_roles():
        name = _poly_name(role.get("name"))
        oid = role.get("oid")
        if not name or not oid or name in SYSTEM_ROLE_NAMES or name.startswith("metarole-"):
            continue
        if not _role_has_scim_resource(role):
            continue
        invite_id = _invite_id_from_object(role) or known.get(oid)
        if invite_id is None:
            logger.debug(
                "Role %s has SCIM resource but no Invite id yet — omitted from form",
                name,
            )
            continue
        by_id[int(invite_id)] = name

    return [
        {"id": rid, "name": name}
        for rid, name in sorted(by_id.items(), key=lambda x: x[1].lower())
    ]


def _render_page(
    *,
    roles: list[dict],
    error: str | None = None,
    success: str | None = None,
    values: dict[str, str] | None = None,
) -> str:
    values = values or {}
    options = "\n".join(
        f'<option value="{html.escape(str(r["id"]))}"'
        f'{" selected" if str(r["id"]) == values.get("role_id", "") else ""}>'
        f'{html.escape(r["name"])} (id={r["id"]})</option>'
        for r in roles
    )
    err_html = (
        f'<p class="error">{html.escape(error)}</p>' if error else ""
    )
    ok_html = (
        f'<div class="ok"><pre>{html.escape(success)}</pre></div>' if success else ""
    )
    lang = values.get("language", "en")
    authority = values.get("intended_authority", "INVITER")
    guest_checked = "checked" if values.get("guest_role_included", "1") == "1" else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Invite user</title>
  <style>
    :root {{
      --bg: #f3f5f7;
      --card: #ffffff;
      --ink: #1a2330;
      --muted: #5b6b7c;
      --accent: #0b6e4f;
      --border: #d5dde5;
      --error: #9b1c1c;
      --ok: #0b6e4f;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Segoe UI", system-ui, sans-serif;
      background: linear-gradient(160deg, #e8eef3 0%, #f7f4ef 100%);
      color: var(--ink);
      min-height: 100vh;
    }}
    main {{
      max-width: 40rem;
      margin: 2.5rem auto;
      padding: 0 1.25rem 3rem;
    }}
    h1 {{
      font-size: 1.75rem;
      margin: 0 0 0.35rem;
      letter-spacing: -0.02em;
    }}
    .lead {{
      color: var(--muted);
      margin: 0 0 1.5rem;
      line-height: 1.45;
    }}
    form {{
      background: var(--card);
      border: 1px solid var(--border);
      padding: 1.25rem 1.35rem 1.5rem;
    }}
    label {{
      display: block;
      font-weight: 600;
      margin: 0.9rem 0 0.35rem;
      font-size: 0.92rem;
    }}
    input[type=text], input[type=email], input[type=number], select, textarea {{
      width: 100%;
      padding: 0.55rem 0.65rem;
      border: 1px solid var(--border);
      background: #fcfdfe;
      font: inherit;
    }}
    textarea {{ min-height: 5rem; resize: vertical; }}
    .row {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
    .row > * {{ flex: 1 1 10rem; }}
    .check {{
      display: flex; align-items: center; gap: 0.5rem;
      margin-top: 1rem; font-weight: 500;
    }}
    button {{
      margin-top: 1.25rem;
      background: var(--accent);
      color: #fff;
      border: 0;
      padding: 0.65rem 1.1rem;
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }}
    button:hover {{ filter: brightness(1.05); }}
    .error {{ color: var(--error); font-weight: 600; }}
    .ok {{
      margin-bottom: 1rem;
      padding: 0.85rem 1rem;
      border: 1px solid #b7d7c8;
      background: #eef8f2;
      color: var(--ok);
    }}
    .ok pre {{ margin: 0; white-space: pre-wrap; font-family: ui-monospace, monospace; font-size: 0.85rem; }}
    .hint {{ color: var(--muted); font-size: 0.85rem; margin: 0.25rem 0 0; }}
  </style>
</head>
<body>
  <main>
    <h1>Invite user</h1>
    <p class="lead">
      Sends an OpenConext Invite invitation by email. No MidPoint user is created until
      the invitee accepts and Invite provisions via SCIM.
    </p>
    {err_html}
    {ok_html}
    <form method="post" action="{html.escape(url_for('invite_form'))}">
      <label for="email">Email</label>
      <input id="email" name="email" type="email" required
             value="{html.escape(values.get('email', ''))}"
             placeholder="user@example.org"/>
      <p class="hint">One address per invitation. Use Invite UI for bulk.</p>

      <label for="role_id">Role</label>
      <select id="role_id" name="role_id" required>
        <option value="">Select a role…</option>
        {options}
      </select>
      <p class="hint">Only roles Invite has already provisioned via SCIM (scimResource on the MidPoint Role).</p>

      <label for="message">Message (optional)</label>
      <textarea id="message" name="message"
        placeholder="Personal message included in the invitation email">{html.escape(values.get('message', ''))}</textarea>

      <div class="row">
        <div>
          <label for="language">Language</label>
          <select id="language" name="language">
            <option value="en"{" selected" if lang == "en" else ""}>English</option>
            <option value="nl"{" selected" if lang == "nl" else ""}>Nederlands</option>
          </select>
        </div>
        <div>
          <label for="intended_authority">Authority</label>
          <select id="intended_authority" name="intended_authority">
            {"".join(
                f'<option value="{a}"{" selected" if authority == a else ""}>{a}</option>'
                for a in ("INVITER", "GUEST", "MANAGER", "APPLICATION_MANAGER", "INSTITUTION_ADMIN")
            )}
          </select>
        </div>
        <div>
          <label for="expiry_days">Invitation expiry (days)</label>
          <input id="expiry_days" name="expiry_days" type="number" min="1" max="3650"
                 value="{html.escape(values.get('expiry_days', '14'))}"/>
        </div>
      </div>

      <label class="check">
        <input type="checkbox" name="guest_role_included" value="1" {guest_checked}/>
        Include guest role (recommended by Invite API)
      </label>

      <button type="submit">Send invitation</button>
    </form>
  </main>
</body>
</html>
"""


def create_app(
    mp: MidPointClient,
    invite: InviteClient,
    known: dict[str, int],
    known_lock: threading.Lock,
) -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("INVITE_BRIDGE_SECRET") or secrets.token_hex(16)
    ui_user = os.environ.get("INVITE_BRIDGE_UI_USER", "").strip()
    ui_password = os.environ.get("INVITE_BRIDGE_UI_PASSWORD", "").strip()

    def _check_auth(username: str, password: str) -> bool:
        if not ui_password:
            return True
        expected_user = ui_user or "administrator"
        return secrets.compare_digest(username, expected_user) and secrets.compare_digest(
            password, ui_password
        )

    def require_ui_auth(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not ui_password:
                return view(*args, **kwargs)
            auth = request.authorization
            if not auth or not _check_auth(auth.username or "", auth.password or ""):
                return Response(
                    "Authentication required",
                    401,
                    {"WWW-Authenticate": 'Basic realm="Invite user"'},
                )
            return view(*args, **kwargs)

        return wrapped

    @app.get("/health")
    def health():
        checks: dict[str, str] = {}
        try:
            mp.ping()
            checks["midpoint"] = "ok"
        except Exception as exc:
            checks["midpoint"] = f"unreachable: {exc}"
        try:
            invite.ping()
            checks["invite"] = "ok"
        except Exception as exc:
            checks["invite"] = f"unreachable: {exc}"
        ok = all(value == "ok" for value in checks.values())
        return {"status": "ok" if ok else "degraded", "checks": checks}, (
            200 if ok else 503
        )

    @app.get("/")
    def root():
        return redirect(url_for("invite_form"))

    @app.route("/invite", methods=["GET", "POST"])
    @require_ui_auth
    def invite_form():
        with known_lock:
            known_snapshot = dict(known)
        try:
            roles = _linked_invite_roles(mp, known_snapshot)
        except Exception:
            logger.exception("Failed to list Invite-linked roles from MidPoint")
            return _render_page(
                roles=[],
                error="Could not reach MidPoint to list roles; try again later.",
            ), 503

        values = {
            "email": "",
            "role_id": "",
            "message": "",
            "language": "en",
            "intended_authority": "INVITER",
            "expiry_days": os.environ.get(
                "INVITE_INVITATION_EXPIRY_DAYS",
                os.environ.get("INVITE_FORM_DEFAULT_EXPIRY_DAYS", "14"),
            ),
            "guest_role_included": "1",
        }

        if request.method == "GET":
            return _render_page(roles=roles, values=values)

        values.update(
            {
                "email": (request.form.get("email") or "").strip(),
                "role_id": (request.form.get("role_id") or "").strip(),
                "message": (request.form.get("message") or "").strip(),
                "language": (request.form.get("language") or "en").strip(),
                "intended_authority": (
                    request.form.get("intended_authority") or "INVITER"
                ).strip(),
                "expiry_days": (request.form.get("expiry_days") or "14").strip(),
                "guest_role_included": (
                    "1" if request.form.get("guest_role_included") else "0"
                ),
            }
        )

        email = values["email"]
        if not email or "@" not in email:
            return _render_page(
                roles=roles, error="A valid email address is required.", values=values
            ), 400
        try:
            role_id = int(values["role_id"])
        except ValueError:
            return _render_page(
                roles=roles, error="Select a role.", values=values
            ), 400
        allowed_ids = {int(r["id"]) for r in roles}
        if role_id not in allowed_ids:
            return _render_page(
                roles=roles,
                error=(
                    "That role is not available for invitation. "
                    "Only MidPoint roles with an Invite SCIM resource can be used."
                ),
                values=values,
            ), 400
        try:
            expiry_days = max(1, int(values["expiry_days"]))
        except ValueError:
            return _render_page(
                roles=roles, error="Expiry days must be a number.", values=values
            ), 400

        try:
            result = invite.create_invitation(
                emails=[email],
                role_ids=[role_id],
                message=values["message"],
                language=values["language"] if values["language"] in {"en", "nl"} else "en",
                intended_authority=values["intended_authority"],
                guest_role_included=values["guest_role_included"] == "1",
                expiry_days=expiry_days,
            )
        except Exception:
            logger.exception("Invitation failed for %s", email)
            return _render_page(
                roles=roles,
                error="Invitation failed; details are in the invite_bridge logs.",
                values=values,
            ), 502

        urls = result.get("recipientInvitationURLs") or []
        lines = [f"Invitation created for {email} (role id {role_id})."]
        for item in urls:
            if isinstance(item, dict):
                lines.append(
                    f"{item.get('recipient')}: {item.get('invitationURL')}"
                )
        if result.get("invitationIdentifiers"):
            lines.append(f"ids: {result['invitationIdentifiers']}")
        logger.info("Created Invite invitation for %s role=%s", email, role_id)
        return _render_page(
            roles=roles,
            success="\n".join(lines),
            values={**values, "email": "", "message": ""},
        )

    return app


def _public_invite_url() -> str | None:
    explicit = os.environ.get("INVITE_BRIDGE_PUBLIC_URL", "").strip()
    if explicit:
        return explicit.rstrip("/") + (
            "" if explicit.rstrip("/").endswith("/invite") else "/invite"
        )
    domain = os.environ.get("DOMAIN", "").strip()
    if domain:
        return f"https://invite-bridge.{domain}/invite"
    return None


def _sync_memberships_enabled() -> bool:
    return os.environ.get("INVITE_BRIDGE_SYNC_MEMBERSHIPS", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _sync_loop(
    mp: MidPointClient,
    invite: InviteClient,
    known: dict[str, int],
    known_lock: threading.Lock,
    state_path: str,
    memberships: dict[str, dict[str, str]],
    memberships_path: str,
    memberships_primed: list[bool],
    interval: int,
    once: bool,
) -> None:
    # memberships_primed is a one-element list so the loop can flip it after
    # the first successful baseline (mutable flag shared with main).
    # Network I/O runs on working copies; the lock only guards the swap and
    # the state-file writes so the HTTP form never blocks on slow calls.
    while True:
        try:
            with known_lock:
                working_known = dict(known)
                working_memberships = {
                    role_oid: dict(members) for role_oid, members in memberships.items()
                }
            sync_once(mp, invite, working_known)
            memberships_enabled = _sync_memberships_enabled()
            if memberships_enabled:
                sync_membership_revokes(
                    mp,
                    invite,
                    working_known,
                    working_memberships,
                    primed=memberships_primed[0],
                )
            with known_lock:
                known.clear()
                known.update(working_known)
                memberships.clear()
                memberships.update(working_memberships)
                if memberships_enabled:
                    memberships_primed[0] = True
                _save_state(state_path, known)
                if memberships_enabled:
                    _save_memberships_state(memberships_path, memberships)
        except Exception:
            logger.exception("Sync failed")
            if once:
                raise
        if once:
            break
        time.sleep(interval)


def main() -> None:
    interval = int(os.environ.get("SYNC_INTERVAL_SECONDS", "30"))
    state_path = os.environ.get("INVITE_BRIDGE_STATE", "/data/invite_links.json")
    memberships_path = _memberships_state_path(state_path)
    http_port = int(os.environ.get("INVITE_BRIDGE_HTTP_PORT", "8080"))

    required = [
        "INVITE_API_TOKEN",
        "INVITE_ORGANIZATION_GUID",
        "INVITE_APPLICATION_MANAGE_ID",
    ]
    missing = [k for k in required if not os.environ.get(k, "").strip()]
    if missing:
        raise SystemExit(f"Missing required env: {', '.join(missing)}")

    mp = MidPointClient(
        os.environ.get("MIDPOINT_URL", "http://midpoint:8080/midpoint/ws/rest"),
        os.environ.get("MIDPOINT_USERNAME", "administrator"),
        os.environ["MIDPOINT_PASSWORD"],
    )
    invite = InviteClient(
        os.environ.get(
            "INVITE_API_BASE_URL",
            "https://invite.test.surfconext.nl/api/external/v1",
        ),
        os.environ["INVITE_API_TOKEN"].strip(),
        os.environ["INVITE_ORGANIZATION_GUID"].strip(),
        os.environ["INVITE_APPLICATION_MANAGE_ID"].strip(),
        os.environ.get("INVITE_APPLICATION_MANAGE_TYPE", "OIDC10_RP").strip(),
        os.environ.get(
            "INVITE_APPLICATION_LANDING_PAGE",
            "https://app.pilot1.sram.surf.nl/",
        ).strip(),
        int(os.environ.get("INVITE_DEFAULT_EXPIRY_DAYS", "365")),
    )

    known = _load_state(state_path)
    memberships = _load_memberships_state(memberships_path)
    # If we already have a snapshot on disk, treat as primed (safe to revoke).
    memberships_primed = [bool(memberships)]
    known_lock = threading.Lock()
    once = os.environ.get("SYNC_ONCE", "").lower() in {"1", "true", "yes"}
    logger.info(
        "invite_bridge started (interval=%ss, state=%s, memberships=%s, "
        "sync_memberships=%s, primed=%s, once=%s, http=:%s)",
        interval,
        state_path,
        memberships_path,
        _sync_memberships_enabled(),
        memberships_primed[0],
        once,
        http_port,
    )

    menu_url = _public_invite_url()
    if menu_url and os.environ.get("INVITE_BRIDGE_REGISTER_MENU", "true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        try:
            # Basic sanity: absolute http(s) URL
            parts = urlsplit(menu_url)
            if parts.scheme in {"http", "https"} and parts.netloc:
                mp.ensure_invite_menu_link(menu_url)
            else:
                logger.warning("Skipping menu link; invalid URL %s", menu_url)
        except Exception:
            logger.exception("Failed to register MidPoint additionalMenuLink")
    else:
        logger.info(
            "MidPoint menu link not registered "
            "(set INVITE_BRIDGE_PUBLIC_URL or DOMAIN, "
            "INVITE_BRIDGE_REGISTER_MENU=true)"
        )

    if not once:
        thread = threading.Thread(
            target=_sync_loop,
            args=(
                mp,
                invite,
                known,
                known_lock,
                state_path,
                memberships,
                memberships_path,
                memberships_primed,
                interval,
                False,
            ),
            name="invite-sync",
            daemon=True,
        )
        thread.start()
    else:
        _sync_loop(
            mp,
            invite,
            known,
            known_lock,
            state_path,
            memberships,
            memberships_path,
            memberships_primed,
            interval,
            True,
        )
        return

    app = create_app(mp, invite, known, known_lock)
    app.run(host="0.0.0.0", port=http_port, threaded=True)


if __name__ == "__main__":
    main()
