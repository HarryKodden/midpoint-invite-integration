"""Sync MidPoint application roles → Moodle cohorts (and members).

Interim Phase-1 path until a ConnId Moodle resource exists.
Role name → cohort idnumber/name; MidPoint user name → Moodle username.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

logging.basicConfig(
    level=os.environ.get("LOGLEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("moodle_bridge")

SYSTEM_ROLE_NAMES = {
    "Superuser",
    "End user",
    "Approver",
    "Reviewer",
    "Delegator",
    "metarole-ldap-group",
    "metarole-moodle-cohort",
}


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


class MoodleClient:
    def __init__(self, ws_url: str, token: str, public_host: str | None = None):
        self.ws_url = ws_url
        self.token = token
        self.public_host = public_host
        self.session = requests.Session()

    def call(self, function: str, **params) -> Any:
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
            self.ws_url, data=data, headers=headers, timeout=60
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("exception"):
            raise RuntimeError(
                f"Moodle {function}: {payload.get('message') or payload}"
            )
        return payload

    def search_cohorts(self) -> list[dict]:
        result = self.call(
            "core_cohort_search_cohorts",
            query="",
            **{
                "context[contextlevel]": "10",
                "context[instanceid]": "0",
                "limitfrom": "0",
                "limitnum": "500",
            },
        )
        if isinstance(result, dict):
            return result.get("cohorts") or []
        return []

    def ensure_cohort(self, name: str, idnumber: str) -> dict:
        for cohort in self.search_cohorts():
            if cohort.get("idnumber") == idnumber or cohort.get("name") == name:
                return cohort
        created = self.call(
            "core_cohort_create_cohorts",
            **{
                "cohorts[0][categorytype][type]": "system",
                "cohorts[0][categorytype][value]": "",
                "cohorts[0][name]": name,
                "cohorts[0][idnumber]": idnumber,
                "cohorts[0][description]": f"Synced from MidPoint role {name}",
            },
        )
        return created[0] if isinstance(created, list) else created

    def get_users_by_field(self, field: str, value: str) -> list[dict]:
        result = self.call(
            "core_user_get_users_by_field",
            field=field,
            **{"values[0]": value},
        )
        return result if isinstance(result, list) else []

    def ensure_user(self, username: str, fullname: str | None, email: str | None) -> dict:
        existing = self.get_users_by_field("username", username)
        if existing:
            return existing[0]
        first = (fullname or username).split(" ", 1)[0]
        last = (fullname or username).split(" ", 1)[1] if fullname and " " in fullname else username
        mail = email or f"{username.replace('@', '_at_')}@users.invalid"
        # Moodle requires a password on create; mark as force-change via auth manual.
        created = self.call(
            "core_user_create_users",
            **{
                "users[0][username]": username,
                "users[0][firstname]": first[:100],
                "users[0][lastname]": last[:100],
                "users[0][email]": mail,
                "users[0][auth]": "manual",
                "users[0][createpassword]": 1,
                "users[0][idnumber]": username,
            },
        )
        return created[0] if isinstance(created, list) else created

    def get_cohort_members(self, cohort_id: int) -> set[int]:
        result = self.call(
            "core_cohort_get_cohort_members",
            **{"cohortids[0]": cohort_id},
        )
        members: set[int] = set()
        for entry in result or []:
            for uid in entry.get("userids") or []:
                members.add(int(uid))
        return members

    def add_cohort_member(self, cohort_id: int, user_id: int) -> None:
        self.call(
            "core_cohort_add_cohort_members",
            **{
                "members[0][cohorttype][type]": "id",
                "members[0][cohorttype][value]": str(cohort_id),
                "members[0][usertype][type]": "id",
                "members[0][usertype][value]": str(user_id),
            },
        )

    def remove_cohort_member(self, cohort_id: int, user_id: int) -> None:
        self.call(
            "core_cohort_delete_cohort_members",
            **{
                "members[0][cohortid]": cohort_id,
                "members[0][userid]": user_id,
            },
        )


def sync_once(mp: MidPointClient, moodle: MoodleClient) -> None:
    roles = mp.list_roles()
    synced = 0
    for role in roles:
        name = _poly_name(role.get("name"))
        oid = role.get("oid")
        if not name or not oid or name in SYSTEM_ROLE_NAMES:
            continue
        logger.info("Sync role %s (%s)", name, oid)
        cohort = moodle.ensure_cohort(name, idnumber=name)
        cohort_id = int(cohort["id"])

        desired_user_ids: set[int] = set()
        for user in mp.list_role_members(oid):
            username = _poly_name(user.get("name"))
            if not username:
                continue
            fullname = _poly_name(user.get("fullName"))
            email = user.get("emailAddress")
            if isinstance(email, dict):
                email = _poly_name(email)
            mu = moodle.ensure_user(username, fullname, email if isinstance(email, str) else None)
            desired_user_ids.add(int(mu["id"]))

        current = moodle.get_cohort_members(cohort_id)
        for uid in sorted(desired_user_ids - current):
            logger.info("  add user %s → cohort %s", uid, name)
            moodle.add_cohort_member(cohort_id, uid)
        for uid in sorted(current - desired_user_ids):
            logger.info("  remove user %s ← cohort %s", uid, name)
            moodle.remove_cohort_member(cohort_id, uid)
        synced += 1
    logger.info("Sync complete (%s roles)", synced)


def _load_token() -> str:
    token = os.environ.get("MOODLE_WS_TOKEN", "").strip()
    if token:
        return token
    path = os.environ.get("MOODLE_WS_TOKEN_FILE", "/shared/moodle_ws_token")
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def main() -> None:
    interval = int(os.environ.get("SYNC_INTERVAL_SECONDS", "60"))
    mp = MidPointClient(
        os.environ.get("MIDPOINT_URL", "http://midpoint:8080/midpoint/ws/rest"),
        os.environ.get("MIDPOINT_USERNAME", "administrator"),
        os.environ["MIDPOINT_PASSWORD"],
    )
    moodle = MoodleClient(
        os.environ.get(
            "MOODLE_WS_URL",
            "http://moodle:8080/webservice/rest/server.php",
        ),
        _load_token(),
        public_host=os.environ.get("MOODLE_PUBLIC_HOST") or None,
    )
    # Prefer public HTTPS URL if set (sslproxy Host); allow override for in-cluster.
    once = os.environ.get("SYNC_ONCE", "").lower() in {"1", "true", "yes"}
    while True:
        try:
            sync_once(mp, moodle)
        except Exception:
            logger.exception("Sync failed")
            if once:
                raise
        if once:
            break
        time.sleep(interval)


if __name__ == "__main__":
    main()
