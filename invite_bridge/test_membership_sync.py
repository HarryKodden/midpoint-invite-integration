"""Unit tests for MidPoint → Invite membership-revoke drift prevention."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import app as bridge


def _scim_user_blob(scim_id: str, user_name: str = "guest@example.org") -> str:
    return json.dumps(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:User"],
            "id": scim_id,
            "userName": user_name,
            "externalId": user_name,
        }
    )


def _scim_group_blob(scim_id: str, display_name: str) -> str:
    return json.dumps(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:Group"],
            "id": scim_id,
            "displayName": display_name,
            "externalId": f"urn:mace:surf.nl:example:{display_name}",
        }
    )


def _role(
    oid: str,
    name: str,
    *,
    invite_id: int | None = 16,
    scim_id: str | None = None,
) -> dict:
    role: dict = {"oid": oid, "name": name}
    if invite_id is not None:
        role["costCenter"] = f"invite:{invite_id}"
    if scim_id is not None:
        role["extension"] = {"scimResource": _scim_group_blob(scim_id, name)}
    return role


def _user(oid: str, *, scim_id: str | None = None, local: bool = False) -> dict:
    user: dict = {"oid": oid, "name": f"user-{oid}"}
    if scim_id is not None and not local:
        user["extension"] = {"scimResource": _scim_user_blob(scim_id)}
    return user


class InviteIdResolutionTests(unittest.TestCase):
    def test_extension_attr_is_preferred(self) -> None:
        role = _role("role-1", "demo", invite_id=16)
        role["extension"] = {"inviteRoleId": "42"}
        self.assertEqual(bridge._invite_id_from_object(role), 42)

    def test_cost_center_is_legacy_fallback(self) -> None:
        role = _role("role-1", "demo", invite_id=16)
        self.assertEqual(bridge._invite_id_from_object(role), 16)

    def test_no_link_returns_none(self) -> None:
        role = _role("role-1", "demo", invite_id=None)
        self.assertIsNone(bridge._invite_id_from_object(role))


class ScimDetectionTests(unittest.TestCase):
    def test_role_and_user_scim_ids(self) -> None:
        role = _role("role-1", "demo", scim_id="group-scim-1")
        user = _user("user-1", scim_id="user-scim-1")
        self.assertTrue(bridge._role_has_scim_resource(role))
        self.assertTrue(bridge._user_has_scim_resource(user))
        self.assertEqual(bridge._scim_resource_id(role), "group-scim-1")
        self.assertEqual(bridge._scim_resource_id(user), "user-scim-1")

    def test_local_objects_have_no_scim_id(self) -> None:
        role = _role("role-1", "local-only", invite_id=1, scim_id=None)
        user = _user("user-1", local=True)
        self.assertFalse(bridge._role_has_scim_resource(role))
        self.assertFalse(bridge._user_has_scim_resource(user))
        self.assertIsNone(bridge._scim_resource_id(role))
        self.assertIsNone(bridge._scim_resource_id(user))


class InviteClientRevokeTests(unittest.TestCase):
    def _client(self) -> bridge.InviteClient:
        return bridge.InviteClient(
            "https://invite.example/api/external/v1",
            "token",
            "org-guid",
            "manage-id",
            "OIDC10_RP",
            "https://app.example/",
            14,
        )

    def test_404_is_gone(self) -> None:
        client = self._client()
        response = MagicMock(status_code=404, text="not found")
        with patch.object(client.session, "request", return_value=response):
            self.assertEqual(
                client.delete_user_role_by_provisioned_scim("u1", "g1"),
                "gone",
            )

    def test_204_is_ok(self) -> None:
        client = self._client()
        response = MagicMock(status_code=204, text="")
        with patch.object(client.session, "request", return_value=response) as req:
            self.assertEqual(
                client.delete_user_role_by_provisioned_scim("u-scim", "g-scim"),
                "ok",
            )
            args, kwargs = req.call_args
            self.assertEqual(args[0], "DELETE")
            self.assertIn(
                "/user_roles/by_provisioned_scim/u-scim/g-scim",
                args[1],
            )

    def test_5xx_is_error_not_raised(self) -> None:
        client = self._client()
        response = MagicMock(status_code=503, text="unavailable")
        with patch.object(client.session, "request", return_value=response):
            self.assertEqual(
                client.delete_user_role_by_provisioned_scim("u1", "g1"),
                "error",
            )


class MembershipSyncTests(unittest.TestCase):
    def test_invite_managed_remove_calls_invite_with_scim_ids(self) -> None:
        mp = MagicMock()
        invite = MagicMock()
        invite.delete_user_role_by_provisioned_scim.return_value = "ok"

        role = _role("role-oid", "e2e-demo", invite_id=16, scim_id="group-scim")
        guest = _user("user-oid", scim_id="user-scim")
        mp.list_roles.return_value = [role]
        mp.list_role_members.return_value = []  # removed
        mp.get_user.return_value = guest

        memberships = {"role-oid": {"user-oid": "user-scim"}}
        known = {"role-oid": 16}

        bridge.sync_membership_revokes(
            mp, invite, known, memberships, primed=True
        )

        invite.delete_user_role_by_provisioned_scim.assert_called_once_with(
            "user-scim", "group-scim"
        )
        self.assertEqual(memberships.get("role-oid"), {})

    def test_local_only_member_remove_does_not_call_invite(self) -> None:
        mp = MagicMock()
        invite = MagicMock()

        # Role is Invite-SCIM managed, but we never tracked the local user
        # (local users are excluded from the snapshot).
        role = _role("role-oid", "e2e-demo", invite_id=16, scim_id="group-scim")
        mp.list_roles.return_value = [role]
        mp.list_role_members.return_value = []

        memberships: dict[str, dict[str, str]] = {"role-oid": {}}
        known = {"role-oid": 16}

        bridge.sync_membership_revokes(
            mp, invite, known, memberships, primed=True
        )

        invite.delete_user_role_by_provisioned_scim.assert_not_called()

    def test_local_role_without_scim_does_not_call_invite(self) -> None:
        mp = MagicMock()
        invite = MagicMock()
        role = _role("role-oid", "local-role", invite_id=None, scim_id=None)
        mp.list_roles.return_value = [role]

        memberships = {"role-oid": {"user-oid": "user-scim"}}
        known: dict[str, int] = {}

        bridge.sync_membership_revokes(
            mp, invite, known, memberships, primed=True
        )

        invite.delete_user_role_by_provisioned_scim.assert_not_called()
        self.assertNotIn("role-oid", memberships)

    def test_cold_start_baselines_without_revoke(self) -> None:
        mp = MagicMock()
        invite = MagicMock()
        role = _role("role-oid", "e2e-demo", invite_id=16, scim_id="group-scim")
        guest = _user("user-oid", scim_id="user-scim")
        mp.list_roles.return_value = [role]
        mp.list_role_members.return_value = [guest]

        memberships: dict[str, dict[str, str]] = {}
        known = {"role-oid": 16}

        bridge.sync_membership_revokes(
            mp, invite, known, memberships, primed=False
        )

        invite.delete_user_role_by_provisioned_scim.assert_not_called()
        self.assertEqual(memberships["role-oid"], {"user-oid": "user-scim"})

    def test_invite_404_still_updates_local_snapshot(self) -> None:
        mp = MagicMock()
        invite = MagicMock()
        invite.delete_user_role_by_provisioned_scim.return_value = "gone"

        role = _role("role-oid", "e2e-demo", invite_id=16, scim_id="group-scim")
        mp.list_roles.return_value = [role]
        mp.list_role_members.return_value = []
        mp.get_user.return_value = None

        memberships = {"role-oid": {"user-oid": "user-scim"}}
        known = {"role-oid": 16}

        bridge.sync_membership_revokes(
            mp, invite, known, memberships, primed=True
        )

        invite.delete_user_role_by_provisioned_scim.assert_called_once()
        self.assertEqual(memberships["role-oid"], {})

    def test_invite_5xx_logs_but_local_snapshot_drops_member(self) -> None:
        mp = MagicMock()
        invite = MagicMock()
        invite.delete_user_role_by_provisioned_scim.return_value = "error"

        role = _role("role-oid", "e2e-demo", invite_id=16, scim_id="group-scim")
        mp.list_roles.return_value = [role]
        mp.list_role_members.return_value = []
        mp.get_user.return_value = _user("user-oid", scim_id="user-scim")

        memberships = {"role-oid": {"user-oid": "user-scim"}}
        known = {"role-oid": 16}

        bridge.sync_membership_revokes(
            mp, invite, known, memberships, primed=True
        )

        invite.delete_user_role_by_provisioned_scim.assert_called_once_with(
            "user-scim", "group-scim"
        )
        # Local MidPoint revoke already happened; snapshot follows MidPoint.
        self.assertEqual(memberships["role-oid"], {})


class InviteFormErrorTests(unittest.TestCase):
    def _client(self, mp, invite):
        import threading

        app = bridge.create_app(mp, invite, {}, threading.Lock())
        return app.test_client()

    def test_get_invite_midpoint_down_returns_503_without_traceback(self) -> None:
        mp = MagicMock()
        mp.list_roles.side_effect = RuntimeError("MidPoint GET /roles/search → 500: internal detail")
        client = self._client(mp, MagicMock())

        resp = client.get("/invite")

        self.assertEqual(resp.status_code, 503)
        self.assertNotIn(b"Traceback", resp.data)
        self.assertNotIn(b"internal detail", resp.data)
        self.assertIn(b"Could not reach MidPoint", resp.data)

    def test_post_invite_upstream_error_returns_502_generic_message(self) -> None:
        role = _role("role-oid", "e2e-demo", invite_id=16, scim_id="group-scim")
        mp = MagicMock()
        mp.list_roles.return_value = [role]
        invite = MagicMock()
        invite.create_invitation.side_effect = RuntimeError(
            "Invite POST /invitations → 500: secret upstream detail"
        )
        client = self._client(mp, invite)

        resp = client.post(
            "/invite",
            data={"email": "guest@example.org", "role_id": "16", "expiry_days": "14"},
        )

        self.assertEqual(resp.status_code, 502)
        self.assertNotIn(b"Traceback", resp.data)
        self.assertNotIn(b"secret upstream detail", resp.data)
        self.assertIn(b"details are in the invite_bridge logs", resp.data)


if __name__ == "__main__":
    unittest.main()
