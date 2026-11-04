"""Group membership lookup backends for the OIDC demo app.

Backends:
- midpoint: MidPoint REST (users + roleMembershipRef → role names/OIDs)
- scim: SCIM Groups filter ``members[value eq "<userId>"]``
- ldap: OpenLDAP groupOfNames member search
- moodle: Moodle cohorts via REST webservice (synced from MidPoint by moodle_bridge)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _poly_name(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("orig") or value.get("norm") or value.get("_value")
    return str(value)


def resolve_user_key(userinfo: dict[str, Any], claim_names: list[str]) -> str | None:
    """Pick the first non-empty claim used to find the user in MidPoint/SCIM."""
    for claim in claim_names:
        value = userinfo.get(claim)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


class GroupLookupError(Exception):
    pass


class MidPointGroupLookup:
    """Resolve groups via MidPoint REST role memberships."""

    name = "midpoint"

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        host_header: str | None = None,
        timeout: float = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth = HTTPBasicAuth(username, password)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )
        if host_header:
            self.session.headers["Host"] = host_header

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            auth=self.auth,
            timeout=self.timeout,
            **kwargs,
        )
        if response.status_code >= 400:
            raise GroupLookupError(
                f"MidPoint {method} {path} failed with HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        return response

    def _search(
        self, endpoint: str, filter_text: str | None = None
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"query": {}}
        if filter_text:
            payload["query"] = {"filter": {"text": filter_text}}
        response = self._request("POST", f"/{endpoint}/search", json=payload)
        objects = response.json().get("object", {}).get("object", [])
        return _as_list(objects)

    def _get(self, endpoint: str, oid: str) -> dict[str, Any] | None:
        try:
            response = self._request("GET", f"/{endpoint}/{oid}")
        except GroupLookupError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        body = response.json()
        for key in ("user", "role", "object"):
            if key in body:
                return body[key]
        return body

    def list_groups(self) -> list[str]:
        """Return MidPoint role names suitable for allow-list selection."""
        groups: list[str] = []
        for role in self._search("roles"):
            name = _poly_name(role.get("name")) or _poly_name(role.get("displayName"))
            if name:
                groups.append(name)
        return sorted(set(groups))

    def lookup_groups(self, user_key: str) -> list[str]:
        users = self._search("users", f'name = "{user_key}"')
        if not users:
            logger.info("MidPoint user not found for key=%s", user_key)
            return []

        user = users[0]
        groups: list[str] = []
        for ref in _as_list(user.get("roleMembershipRef")):
            if not isinstance(ref, dict):
                continue
            ref_type = str(ref.get("type", ""))
            if ref_type and not ref_type.endswith("RoleType"):
                continue
            oid = ref.get("oid")
            if not oid:
                continue
            role = self._get("roles", oid)
            name = _poly_name((role or {}).get("name")) if role else None
            groups.append(name or oid)
        return sorted(set(groups))


class ScimGroupLookup:
    """Resolve groups via SCIM filter on Group.members."""

    name = "scim"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/scim+json",
                "Content-Type": "application/scim+json",
                "Authorization": f"Bearer {api_key}",
            }
        )

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        response = self.session.get(
            f"{self.base_url}{path}", params=params, timeout=self.timeout
        )
        if response.status_code >= 400:
            raise GroupLookupError(
                f"SCIM GET {path} failed with HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        return response.json()

    def _find_user_id(self, user_key: str) -> str | None:
        # Try userName first, then id equality if the claim is already a SCIM id.
        for attr in ("userName", "id", "externalId"):
            filt = f'{attr} eq "{user_key}"'
            data = self._get("/Users", params={"filter": filt})
            resources = data.get("Resources") or []
            if resources:
                return resources[0].get("id")
        return None

    def list_groups(self) -> list[str]:
        """Return SCIM group displayNames suitable for allow-list selection."""
        data = self._get("/Groups")
        groups: list[str] = []
        for group in data.get("Resources") or []:
            name = group.get("displayName") or group.get("id")
            if name:
                groups.append(str(name))
        return sorted(set(groups))

    def lookup_groups(self, user_key: str) -> list[str]:
        user_id = self._find_user_id(user_key)
        if not user_id:
            logger.info("SCIM user not found for key=%s", user_key)
            return []

        # Nested path filter supported by this SCIM server.
        filt = f'members[value eq "{user_id}"]'
        data = self._get("/Groups", params={"filter": filt})
        groups: list[str] = []
        for group in data.get("Resources") or []:
            name = group.get("displayName") or group.get("id")
            if name:
                groups.append(str(name))
        return sorted(set(groups))


class LdapGroupLookup:
    """Resolve groups via LDAP (typical MidPoint→LDAP provisioning target)."""

    name = "ldap"

    def __init__(
        self,
        url: str,
        bind_dn: str = "",
        bind_password: str = "",
        user_base: str = "",
        user_filter: str = "(|(uid={0})(mail={0})(cn={0}))",
        group_base: str = "",
        group_filter: str = "(objectClass=groupOfNames)",
        group_name_attr: str = "cn",
        group_member_attr: str = "member",
        start_tls: bool = False,
        timeout: float = 15,
    ):
        if not url:
            raise GroupLookupError("LDAP_URL is required for LDAP lookup")
        if not user_base:
            raise GroupLookupError("LDAP_USER_BASE is required for LDAP lookup")
        if not group_base:
            raise GroupLookupError("LDAP_GROUP_BASE is required for LDAP lookup")
        self.url = url
        self.bind_dn = bind_dn
        self.bind_password = bind_password
        self.user_base = user_base
        self.user_filter = user_filter
        self.group_base = group_base
        self.group_filter = group_filter
        self.group_name_attr = group_name_attr
        self.group_member_attr = group_member_attr
        self.start_tls = start_tls
        self.timeout = timeout

    def _connection(self):
        from ldap3 import ALL, Connection, Server, Tls
        import ssl

        validate = ssl.CERT_REQUIRED
        if os.environ.get("LDAP_TLS_VALIDATE", "false").lower() not in {
            "1",
            "true",
            "yes",
        }:
            validate = ssl.CERT_NONE
        use_tls = self.url.lower().startswith("ldaps://") or self.start_tls
        tls = Tls(validate=validate) if use_tls else None
        server = Server(self.url, get_info=ALL, connect_timeout=self.timeout, tls=tls)
        kwargs: dict[str, Any] = {
            "server": server,
            "auto_bind": True,
            "receive_timeout": self.timeout,
        }
        if self.bind_dn:
            kwargs["user"] = self.bind_dn
            kwargs["password"] = self.bind_password
        try:
            conn = Connection(**kwargs)
            if self.start_tls and not self.url.lower().startswith("ldaps://"):
                if not conn.start_tls():
                    raise GroupLookupError("LDAP StartTLS failed")
            return conn
        except Exception as exc:  # noqa: BLE001
            raise GroupLookupError(f"LDAP bind failed: {exc}") from exc

    def _escape(self, value: str) -> str:
        # RFC 4515 filter escaping
        replacements = (
            ("\\", r"\5c"),
            ("*", r"\2a"),
            ("(", r"\28"),
            (")", r"\29"),
            ("\x00", r"\00"),
        )
        out = value
        for src, dst in replacements:
            out = out.replace(src, dst)
        return out

    def list_groups(self) -> list[str]:
        from ldap3 import SUBTREE

        conn = self._connection()
        try:
            ok = conn.search(
                search_base=self.group_base,
                search_filter=self.group_filter,
                search_scope=SUBTREE,
                attributes=[self.group_name_attr],
            )
            if not ok:
                raise GroupLookupError(
                    f"LDAP group search failed: {conn.result}"
                )
            groups: list[str] = []
            for entry in conn.entries:
                values = entry[self.group_name_attr].values
                if values:
                    groups.append(str(values[0]))
            return sorted(set(groups))
        finally:
            conn.unbind()

    def _find_user(self, conn, user_key: str) -> tuple[str, str] | None:
        """Return (dn, member_value) for group membership searches."""
        from ldap3 import SUBTREE

        filt = self.user_filter.format(self._escape(user_key))
        ok = conn.search(
            search_base=self.user_base,
            search_filter=filt,
            search_scope=SUBTREE,
            attributes=["uid", "cn"],
        )
        if not ok or not conn.entries:
            return None
        entry = conn.entries[0]
        dn = str(entry.entry_dn)
        if self.group_member_attr.lower() == "memberuid":
            uid_values = entry.uid.values if "uid" in entry else []
            member_value = str(uid_values[0]) if uid_values else user_key
            return dn, member_value
        return dn, dn

    def lookup_groups(self, user_key: str) -> list[str]:
        from ldap3 import SUBTREE

        conn = self._connection()
        try:
            found = self._find_user(conn, user_key)
            if not found:
                logger.info("LDAP user not found for key=%s", user_key)
                return []
            _user_dn, member_value = found

            member_filt = (
                f"({self.group_member_attr}={self._escape(member_value)})"
            )
            if self.group_filter.startswith("(") and self.group_filter.endswith(")"):
                filt = f"(&{self.group_filter}{member_filt})"
            else:
                filt = f"(&({self.group_filter}){member_filt})"

            ok = conn.search(
                search_base=self.group_base,
                search_filter=filt,
                search_scope=SUBTREE,
                attributes=[self.group_name_attr],
            )
            if not ok:
                raise GroupLookupError(
                    f"LDAP membership search failed: {conn.result}"
                )
            groups: list[str] = []
            for entry in conn.entries:
                values = entry[self.group_name_attr].values
                if values:
                    groups.append(str(values[0]))
            return sorted(set(groups))
        finally:
            conn.unbind()


class MoodleGroupLookup:
    """Resolve groups via Moodle cohorts (REST webservice)."""

    name = "moodle"

    def __init__(
        self,
        ws_url: str,
        token: str,
        public_host: str | None = None,
        timeout: float = 30,
    ):
        if not ws_url:
            raise GroupLookupError("MOODLE_WS_URL is required for Moodle lookup")
        if not token:
            raise GroupLookupError("MOODLE_WS_TOKEN is required for Moodle lookup")
        self.ws_url = ws_url
        self.token = token
        self.public_host = public_host
        self.timeout = timeout
        self.session = requests.Session()

    def _call(self, function: str, **params) -> Any:
        data = {
            "wstoken": self.token,
            "wsfunction": function,
            "moodlewsrestformat": "json",
        }
        data.update(params)
        headers = {}
        if self.public_host:
            headers["Host"] = self.public_host
            headers["X-Forwarded-Proto"] = "https"
            headers["X-Forwarded-Host"] = self.public_host
        response = self.session.post(
            self.ws_url, data=data, headers=headers, timeout=self.timeout
        )
        if response.status_code >= 400:
            raise GroupLookupError(
                f"Moodle {function} → HTTP {response.status_code}: {response.text[:300]}"
            )
        payload = response.json()
        if isinstance(payload, dict) and payload.get("exception"):
            raise GroupLookupError(
                f"Moodle {function}: {payload.get('message') or payload}"
            )
        return payload

    def list_groups(self) -> list[str]:
        result = self._call(
            "core_cohort_search_cohorts",
            query="",
            **{
                "context[contextlevel]": "10",
                "context[instanceid]": "0",
                "limitfrom": "0",
                "limitnum": "500",
            },
        )
        cohorts = result.get("cohorts") if isinstance(result, dict) else result
        names: list[str] = []
        for cohort in cohorts or []:
            name = cohort.get("idnumber") or cohort.get("name")
            if name:
                names.append(str(name))
        return sorted(set(names))

    def lookup_groups(self, user_key: str) -> list[str]:
        users = self._call(
            "core_user_get_users_by_field",
            field="username",
            **{"values[0]": user_key},
        )
        if not users:
            logger.info("Moodle user not found for key=%s", user_key)
            return []
        user_id = int(users[0]["id"])

        result = self._call(
            "core_cohort_search_cohorts",
            query="",
            **{
                "context[contextlevel]": "10",
                "context[instanceid]": "0",
                "limitfrom": "0",
                "limitnum": "500",
            },
        )
        cohorts = result.get("cohorts") if isinstance(result, dict) else result
        if not cohorts:
            return []

        # Batch member lookup (API accepts multiple cohort ids).
        params: dict[str, Any] = {}
        for idx, cohort in enumerate(cohorts):
            params[f"cohortids[{idx}]"] = int(cohort["id"])
        members_payload = self._call("core_cohort_get_cohort_members", **params)
        members_by_cohort = {
            int(entry["cohortid"]): set(int(u) for u in entry.get("userids") or [])
            for entry in (members_payload or [])
            if entry.get("cohortid") is not None
        }

        groups: list[str] = []
        for cohort in cohorts:
            cid = int(cohort["id"])
            if user_id in members_by_cohort.get(cid, set()):
                name = cohort.get("idnumber") or cohort.get("name")
                if name:
                    groups.append(str(name))
        return sorted(set(groups))


LOOKUP_BACKENDS = ("midpoint", "scim", "ldap", "moodle")

LOOKUP_BACKEND_META = {
    "midpoint": {
        "label": "MidPoint",
        "description": (
            "List roles and resolve memberships via MidPoint REST "
            "(roleMembershipRef)."
        ),
    },
    "scim": {
        "label": "SCIM",
        "description": (
            "List groups and resolve memberships via SCIM "
            '(Groups?filter=members[value eq "..."]).'
        ),
    },
    "ldap": {
        "label": "LDAP",
        "description": (
            "List groups and resolve memberships via LDAP — typical target when "
            "MidPoint provisions accounts/groups to a directory."
        ),
    },
    "moodle": {
        "label": "Moodle",
        "description": (
            "List Moodle cohorts and resolve memberships via Moodle REST "
            "(synced from MidPoint by moodle_bridge)."
        ),
    },
}


def build_midpoint_client_from_env() -> MidPointGroupLookup:
    password = os.environ.get("MIDPOINT_PASSWORD", "")
    if not password:
        raise GroupLookupError("MIDPOINT_PASSWORD is required to list MidPoint groups")
    return MidPointGroupLookup(
        base_url=os.environ.get(
            "MIDPOINT_URL", "http://midpoint:8080/midpoint/ws/rest"
        ),
        username=os.environ.get("MIDPOINT_USERNAME", "administrator"),
        password=password,
        host_header=os.environ.get("MIDPOINT_HOST_HEADER") or None,
    )


def build_scim_client_from_env() -> ScimGroupLookup:
    return ScimGroupLookup(
        base_url=os.environ.get("SCIM_URL", "http://scim:8000/api/v2"),
        api_key=os.environ.get("SCIM_API_KEY", "secret"),
    )


def build_ldap_client_from_env() -> LdapGroupLookup:
    url = os.environ.get("LDAP_URL", "").strip()
    if not url:
        raise GroupLookupError(
            "LDAP_URL is not set (optional backend; configure when MidPoint "
            "provisions to LDAP)"
        )
    return LdapGroupLookup(
        url=url,
        bind_dn=os.environ.get("LDAP_BIND_DN", ""),
        bind_password=os.environ.get("LDAP_BIND_PASSWORD", ""),
        user_base=os.environ.get("LDAP_USER_BASE", ""),
        user_filter=os.environ.get(
            "LDAP_USER_FILTER", "(|(uid={0})(mail={0})(cn={0}))"
        ),
        group_base=os.environ.get("LDAP_GROUP_BASE", ""),
        group_filter=os.environ.get(
            "LDAP_GROUP_FILTER", "(objectClass=groupOfNames)"
        ),
        group_name_attr=os.environ.get("LDAP_GROUP_NAME_ATTR", "cn"),
        group_member_attr=os.environ.get("LDAP_GROUP_MEMBER_ATTR", "member"),
        start_tls=os.environ.get("LDAP_START_TLS", "false").lower()
        in {"1", "true", "yes"},
    )


def _moodle_token_from_env() -> str:
    token = os.environ.get("MOODLE_WS_TOKEN", "").strip()
    if token:
        return token
    path = os.environ.get("MOODLE_WS_TOKEN_FILE", "").strip()
    if path and os.path.isfile(path):
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    return ""


def build_moodle_client_from_env() -> MoodleGroupLookup:
    return MoodleGroupLookup(
        ws_url=os.environ.get(
            "MOODLE_WS_URL", "http://moodle:8080/webservice/rest/server.php"
        ),
        token=_moodle_token_from_env(),
        public_host=os.environ.get("MOODLE_PUBLIC_HOST") or None,
    )


def build_group_lookup(backend: str):
    name = (backend or "").strip().lower()
    if name == "midpoint":
        return build_midpoint_client_from_env()
    if name == "scim":
        return build_scim_client_from_env()
    if name == "ldap":
        return build_ldap_client_from_env()
    if name == "moodle":
        return build_moodle_client_from_env()
    raise GroupLookupError(
        f"Unsupported lookup backend={backend!r}; "
        "use 'midpoint', 'scim', 'ldap', or 'moodle'"
    )


def build_group_lookup_from_env():
    return build_group_lookup(os.environ.get("GROUP_LOOKUP_BACKEND", "midpoint"))
